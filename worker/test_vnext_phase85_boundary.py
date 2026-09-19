"""Deterministic Phase-8.5 reconciliation and outer-controller tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from vnext_runtime.adoption import (
    AdoptionMode,
    CandidateFreeze,
    ConflictCategory,
    GitInspector,
    MainRevalidation,
    ReconciliationConflictError,
    ReconciliationPolicy,
    ReconciliationResolution,
    plan_reconciliation,
)
from vnext_runtime.handoff import (
    HandoffConflictError,
    HandoffConsumeStatus,
    HandoffEvidenceStore,
    HandoffHealthError,
    HandoffIdentity,
    HandoffIntegrityError,
    HandoffNotQuiescedError,
    HandoffPhase,
    HandoffStaleError,
    HandoffTransitionError,
    ForwardRollbackObservation,
    LauncherHandoffActions,
    LauncherHandoffConsumer,
    OuterControllerHandoff,
    WorkerRestartObservation,
)
from vnext_runtime.adoption import HealthObservation, RollbackIdentity
import windows_worker_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_BASE = "4444444444444444444444444444444444444444"
SHA_STABLE = "b" * 40
SHA_CANDIDATE = "c" * 40
SHA_MAIN = "d" * 40
SHA_ADOPTED = "e" * 40
SHA_ROLLBACK = "f" * 40


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def _synthetic_repo(
    root: Path,
    *,
    candidate_path: str,
    main_path: str | None = None,
    candidate_text: str = "candidate\n",
    main_text: str = "stable\n",
) -> tuple[str, str, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Phase 8.5 boundary test")
    _git(root, "config", "user.email", "phase85@example.invalid")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "feature/synthetic-candidate")
    candidate = root / candidate_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(candidate_text, encoding="utf-8")
    _git(root, "add", candidate_path)
    _git(root, "commit", "-m", "candidate")
    candidate_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    if main_path is not None:
        main = root / main_path
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_text(main_text, encoding="utf-8")
        _git(root, "add", main_path)
        _git(root, "commit", "-m", "stable change")
    main_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "feature/synthetic-candidate")
    return base, candidate_sha, main_sha


def _synthetic_protocol_repo(
    root: Path,
    *,
    base_text: str = "base\n",
    candidate_text: str,
    main_text: str,
) -> tuple[str, str, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Phase 8.5 protocol test")
    _git(root, "config", "user.email", "phase85-protocol@example.invalid")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / "PROTOCOL.md").write_text(base_text, encoding="utf-8")
    _git(root, "add", "README.md", "PROTOCOL.md")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "feature/synthetic-candidate")
    (root / "PROTOCOL.md").write_text(candidate_text, encoding="utf-8")
    _git(root, "add", "PROTOCOL.md")
    _git(root, "commit", "-m", "candidate")
    candidate_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    (root / "PROTOCOL.md").write_text(main_text, encoding="utf-8")
    _git(root, "add", "PROTOCOL.md")
    _git(root, "commit", "-m", "stable change")
    main_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "feature/synthetic-candidate")
    return base, candidate_sha, main_sha


def _synthetic_protocol_reverted_main_repo(root: Path) -> tuple[str, str, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Phase 8.5 protocol history test")
    _git(root, "config", "user.email", "phase85-protocol-history@example.invalid")
    base_text = "base\n"
    (root / "README.md").write_text(base_text, encoding="utf-8")
    (root / "PROTOCOL.md").write_text(base_text, encoding="utf-8")
    _git(root, "add", "README.md", "PROTOCOL.md")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "feature/synthetic-candidate")
    (root / "PROTOCOL.md").write_text("candidate\n", encoding="utf-8")
    _git(root, "add", "PROTOCOL.md")
    _git(root, "commit", "-m", "candidate")
    candidate_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    (root / "PROTOCOL.md").write_text("stable-intermediate\n", encoding="utf-8")
    _git(root, "add", "PROTOCOL.md")
    _git(root, "commit", "-m", "stable intermediate")
    (root / "PROTOCOL.md").write_text(base_text, encoding="utf-8")
    _git(root, "add", "PROTOCOL.md")
    _git(root, "commit", "-m", "stable revert")
    main_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "feature/synthetic-candidate")
    return base, candidate_sha, main_sha


class CurrentMainReconciliationTests(unittest.TestCase):
    """Qualify current-main overlaps without widening protected authority."""

    def test_current_latest_main_accepts_safe_overlaps_and_rejects_protected_conflicts(self) -> None:
        # A public regression must qualify divergent refs using synthetic Git,
        # not assume a private historical bootstrap commit exists in this repo.
        temporary = tempfile.TemporaryDirectory(prefix="bridge-current-main-fixture-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        paths = (
            "protocol/v2/check_conformance.py", "protocol/v2/transitions.json",
            "worker/bridge_worker_hardened.py", "worker/protocol_core.py",
            "worker/vnext_runtime/__init__.py", "worker/vnext_runtime/adoption.py",
            "worker/windows_worker_launcher.py",
        )
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.name", "Synthetic boundary test")
        _git(root, "config", "user.email", "boundary@example.test")
        for name in (*paths, "worker/safe_extension.py"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("base\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "synthetic base")
        base = _git(root, "rev-parse", "HEAD")
        _git(root, "switch", "-c", "feature/synthetic-candidate")
        for name in paths:
            (root / name).write_text("candidate\n", encoding="utf-8")
        (root / "worker/safe_extension.py").write_text("agreed\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "synthetic candidate")
        candidate_sha = _git(root, "rev-parse", "HEAD")
        _git(root, "switch", "main")
        for name in paths:
            (root / name).write_text("main\n", encoding="utf-8")
        (root / "worker/safe_extension.py").write_text("agreed\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "synthetic latest main")
        latest_main_sha = _git(root, "rev-parse", "HEAD")
        _git(root, "switch", "feature/synthetic-candidate")
        candidate, main, inspector = self._identities_for_repo(
            root, base, candidate_sha, latest_main_sha, GitInspector(root)
        )
        with self.assertRaises(ReconciliationConflictError) as raised:
            plan_reconciliation(
                candidate,
                main,
                candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                main_changed_paths=inspector.changed_paths(base, latest_main_sha),
                previous_stable_sha=latest_main_sha,
                repository=inspector,
                policy=ReconciliationPolicy.phase_85(),
            )
        conflicts = raised.exception.conflicts
        self.assertEqual(
            tuple(item.path for item in conflicts),
            (
                "protocol/v2/check_conformance.py",
                "protocol/v2/transitions.json",
                "worker/bridge_worker_hardened.py",
                "worker/protocol_core.py",
                "worker/vnext_runtime/__init__.py",
                "worker/vnext_runtime/adoption.py",
                "worker/windows_worker_launcher.py",
            ),
        )
        self.assertEqual(
            tuple(item.category for item in conflicts),
            (
                ConflictCategory.PROTOCOL_AUTHORITY,
                ConflictCategory.PROTOCOL_AUTHORITY,
                ConflictCategory.LAUNCHER_SAFETY,
                ConflictCategory.PROTOCOL_AUTHORITY,
                ConflictCategory.SOURCE_OVERLAP,
                ConflictCategory.SOURCE_OVERLAP,
                ConflictCategory.LAUNCHER_SAFETY,
            ),
        )

    def test_protocol_clean_three_way_is_allowed_but_content_conflict_is_not(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-protocol-") as temporary:
            root = Path(temporary)
            base, candidate_sha, main_sha = _synthetic_protocol_repo(
                root,
                base_text="section-a\nbase-a\nsection-b\nbase-b\n",
                candidate_text="section-a\ncandidate-a\nsection-b\nbase-b\n",
                main_text="section-a\nbase-a\nsection-b\nstable-b\n",
            )
            inspector = GitInspector(root)
            candidate, main, inspector = self._identities_for_repo(
                root,
                base,
                candidate_sha,
                main_sha,
                inspector,
            )
            plan = plan_reconciliation(
                candidate,
                main,
                candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                main_changed_paths=inspector.changed_paths(base, main_sha),
                repository=inspector,
                policy=ReconciliationPolicy.phase_85(),
            )
            decision = plan.decisions[0]
            self.assertEqual(decision.path, "PROTOCOL.md")
            self.assertEqual(decision.resolution, ReconciliationResolution.CLEAN_THREE_WAY)
            self.assertEqual(decision.category, ConflictCategory.PROTOCOL_AUTHORITY)

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-protocol-conflict-") as temporary:
            root = Path(temporary)
            base, candidate_sha, main_sha = _synthetic_protocol_repo(
                root,
                candidate_text="candidate\n",
                main_text="stable\n",
            )
            inspector = GitInspector(root)
            candidate, main, inspector = self._identities_for_repo(
                root,
                base,
                candidate_sha,
                main_sha,
                inspector,
            )
            with self.assertRaises(ReconciliationConflictError) as raised:
                plan_reconciliation(
                    candidate,
                    main,
                    candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                    main_changed_paths=inspector.changed_paths(base, main_sha),
                    repository=inspector,
                    policy=ReconciliationPolicy.phase_85(),
                )
            self.assertEqual(raised.exception.conflicts[0].category, ConflictCategory.PROTOCOL_AUTHORITY)
            self.assertEqual(raised.exception.conflicts[0].reason, "protected_protocol_three_way_conflict")

    def test_reverted_main_protocol_history_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-protocol-revert-") as temporary:
            root = Path(temporary)
            base, candidate_sha, main_sha = _synthetic_protocol_reverted_main_repo(root)
            inspector = GitInspector(root)
            candidate, main, inspector = self._identities_for_repo(
                root,
                base,
                candidate_sha,
                main_sha,
                inspector,
            )
            self.assertNotIn("PROTOCOL.md", inspector.changed_paths(base, main_sha))
            with self.assertRaises(ReconciliationConflictError) as raised:
                plan_reconciliation(
                    candidate,
                    main,
                    candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                    main_changed_paths=inspector.changed_paths(base, main_sha),
                    repository=inspector,
                    policy=ReconciliationPolicy.phase_85(),
                )
            self.assertEqual(raised.exception.conflicts[0].category, ConflictCategory.PROTOCOL_AUTHORITY)

    def _identities_for_repo(
        self,
        root: Path,
        base: str,
        candidate_sha: str,
        main_sha: str,
        inspector: GitInspector,
    ) -> tuple[CandidateFreeze, MainRevalidation, GitInspector]:
        return (
            CandidateFreeze(
                candidate_root=root,
                candidate_branch="feature/synthetic-candidate",
                bootstrap_base_sha=base,
                accepted_candidate_sha=candidate_sha,
                frozen_at="2001-01-15T00:00:00+00:00",
            ),
            MainRevalidation(
                ref="main",
                expected_main_sha=main_sha,
                observed_main_sha=main_sha,
                revalidated_at="2001-01-15T00:00:00+00:00",
            ),
            inspector,
        )


class ReconciliationFailClosedTests(unittest.TestCase):
    def _identities(self, root: Path, base: str, candidate_sha: str, main_sha: str) -> tuple[CandidateFreeze, MainRevalidation, GitInspector]:
        inspector = GitInspector(root)
        return (
            CandidateFreeze(
                candidate_root=root,
                candidate_branch="feature/synthetic-candidate",
                bootstrap_base_sha=base,
                accepted_candidate_sha=candidate_sha,
                frozen_at="2001-01-15T00:00:00+00:00",
            ),
            MainRevalidation(
                ref="main",
                expected_main_sha=main_sha,
                observed_main_sha=main_sha,
                revalidated_at="2001-01-15T00:00:00+00:00",
            ),
            inspector,
        )

    def test_unknown_source_overlap_is_precisely_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-unknown-") as temporary:
            root = Path(temporary)
            base, candidate_sha, main_sha = _synthetic_repo(
                root,
                candidate_path="worker/unknown.py",
                main_path="worker/unknown.py",
            )
            candidate, main, inspector = self._identities(root, base, candidate_sha, main_sha)
            with self.assertRaises(ReconciliationConflictError) as raised:
                plan_reconciliation(
                    candidate,
                    main,
                    candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                    main_changed_paths=inspector.changed_paths(base, main_sha),
                    repository=inspector,
                    policy=ReconciliationPolicy.phase_85(),
                )
            self.assertEqual(raised.exception.conflicts[0].path, "worker/unknown.py")
            self.assertEqual(raised.exception.conflicts[0].reason, "unapproved_source_overlap")

    def test_project_control_plane_and_boundary_two_sided_changes_stay_protected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-protected-") as temporary:
            root = Path(temporary)
            base, candidate_sha, main_sha = _synthetic_repo(
                root,
                candidate_path="projects/p/state.json",
                main_path="projects/p/state.json",
            )
            candidate, main, inspector = self._identities(root, base, candidate_sha, main_sha)
            with self.assertRaises(ReconciliationConflictError) as raised:
                plan_reconciliation(
                    candidate,
                    main,
                    candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                    main_changed_paths=inspector.changed_paths(base, main_sha),
                    repository=inspector,
                    policy=ReconciliationPolicy.phase_85(),
                )
            self.assertEqual(raised.exception.conflicts[0].category, ConflictCategory.PROJECT_CONTROL_PLANE)

    def test_identical_project_tip_does_not_authorize_control_plane_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-identical-project-") as temporary:
            root = Path(temporary)
            base, candidate_sha, main_sha = _synthetic_repo(
                Path(temporary),
                candidate_path="projects/p/state.json",
                main_path="projects/p/state.json",
                candidate_text="same\n",
                main_text="same\n",
            )
            candidate, main, inspector = self._identities(root, base, candidate_sha, main_sha)
            with self.assertRaises(ReconciliationConflictError) as raised:
                plan_reconciliation(
                    candidate,
                    main,
                    candidate_changed_paths=inspector.changed_paths(base, candidate_sha),
                    main_changed_paths=inspector.changed_paths(base, main_sha),
                    repository=inspector,
                    policy=ReconciliationPolicy.phase_85(),
                )
            self.assertEqual(raised.exception.conflicts[0].category, ConflictCategory.PROJECT_CONTROL_PLANE)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _ready_health() -> HealthObservation:
    return HealthObservation(
        observed_at="2001-01-15T12:00:03+00:00",
        exit_code=75,
        worker_started_at="2001-01-15T12:00:00.500000+00:00",
        heartbeat_at="2001-01-15T12:00:02.500000+00:00",
        launcher_alive=True,
        worker_healthy=True,
        protocol_ready=True,
        recovery_clear=True,
    )


class _HandoffActionHarness:
    """Idempotent infrastructure double; it never starts an Agent or Git."""

    def __init__(
        self,
        *,
        stopped: bool = True,
        active: tuple[str, ...] = (),
        health: HealthObservation | None = None,
        adopted_sha: str = SHA_ADOPTED,
        rollback_sha: str = SHA_ROLLBACK,
    ) -> None:
        self.stopped = stopped
        self.active = active
        self.health = health or _ready_health()
        self.adopted_sha = adopted_sha
        self.rollback_sha = rollback_sha
        self.calls: Counter[str] = Counter()
        self.events: list[str] = []
        self.fail_once: str | None = None
        self.fail_after_once: str | None = None
        self.invalid_reason: str | None = None
        self.adoption_effects: set[str] = set()
        self.restart_effects: set[str] = set()
        self.rollback_effects: set[str] = set()
        self.rollback_reasons: list[str] = []

    def _call(self, name: str) -> None:
        self.calls[name] += 1
        self.events.append(name)
        if self.fail_once == name:
            self.fail_once = None
            raise RuntimeError(f"simulated {name} crash")

    def _after_effect(self, name: str) -> None:
        if self.fail_after_once == name:
            self.fail_after_once = None
            raise RuntimeError(f"simulated {name} crash after effect")

    def validate_identity(self, identity: HandoffIdentity) -> None:
        self._call("validate_identity")
        if self.invalid_reason is not None:
            raise HandoffConflictError(self.invalid_reason)

    def worker_stopped(self, identity: HandoffIdentity) -> bool:
        self._call("worker_stopped")
        return self.stopped

    def active_run_ids(self, identity: HandoffIdentity) -> tuple[str, ...]:
        self._call("active_run_ids")
        return self.active

    def ensure_adopted(self, identity: HandoffIdentity) -> str:
        self._call("ensure_adopted")
        self.adoption_effects.add(identity.attempt_id)
        self._after_effect("ensure_adopted")
        return self.adopted_sha

    def ensure_worker_restarted(self, identity: HandoffIdentity) -> WorkerRestartObservation:
        self._call("ensure_worker_restarted")
        self.restart_effects.add(identity.attempt_id)
        self._after_effect("ensure_worker_restarted")
        return WorkerRestartObservation(
            launcher_identity=identity.expected_launcher_identity,
            worker_identity=identity.expected_worker_identity,
            worker_sha=identity.reconciled_adoption_sha,
        )

    def observe_health(self, identity: HandoffIdentity) -> HealthObservation:
        self._call("observe_health")
        return self.health

    def ensure_forward_rollback(
        self,
        identity: HandoffIdentity,
        reason: str,
    ) -> ForwardRollbackObservation:
        self._call("ensure_forward_rollback")
        self.rollback_effects.add(identity.attempt_id)
        self.rollback_reasons.append(reason)
        self._after_effect("ensure_forward_rollback")
        return ForwardRollbackObservation(
            rollback_commit_sha=self.rollback_sha,
            forward_only_verified=True,
        )

    def actions(self) -> LauncherHandoffActions:
        return LauncherHandoffActions(
            validate_identity=self.validate_identity,
            worker_stopped=self.worker_stopped,
            active_run_ids=self.active_run_ids,
            ensure_adopted=self.ensure_adopted,
            ensure_worker_restarted=self.ensure_worker_restarted,
            observe_health=self.observe_health,
            ensure_forward_rollback=self.ensure_forward_rollback,
        )


class _FailOnceCompletionStore(HandoffEvidenceStore):
    """Simulate a Launcher crash/failure while recording COMPLETED."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.failed = False

    def write(self, evidence):
        if evidence.phase is HandoffPhase.COMPLETED and not self.failed:
            self.failed = True
            raise HandoffIntegrityError("simulated_completion_write_failure")
        return super().write(evidence)


class OuterControllerHandoffTests(unittest.TestCase):
    def identity(self, *, deadline: datetime | None = None, replay: bool = False) -> HandoffIdentity:
        started = datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc)
        return HandoffIdentity(
            attempt_id="attempt-019",
            initiated_at=started.isoformat(),
            previous_stable_sha=SHA_STABLE,
            latest_main_sha=SHA_MAIN,
            accepted_candidate_sha=SHA_CANDIDATE,
            reconciled_adoption_sha=SHA_ADOPTED,
            initiating_project_id="engine-maintenance",
            initiating_run_id="run-019",
            initiating_command_id=19,
            initiating_claim_generation=50,
            expected_launcher_identity="outer-launcher-v1",
            expected_worker_identity="worker-vnext-v1",
            startup_contract=("outer_launcher_job", "exit_75_restart", "health_gate"),
            health_deadline=(deadline or started + timedelta(minutes=10)).isoformat(),
            health_criteria=("launcher_alive", "worker_healthy", "protocol_ready", "recovery_clear"),
            probation_seconds=2,
            rollback_identity=RollbackIdentity(known_good_sha=SHA_STABLE),
            mode=AdoptionMode.MANUAL,
            replay_initiating_command=replay,
        )

    def live_identity(self) -> HandoffIdentity:
        """Build evidence for tests whose consumer intentionally uses the real clock."""
        return self.identity(deadline=datetime.now(timezone.utc) + timedelta(hours=1))

    def _prepared_consumer(
        self,
        temporary: str,
        *,
        identity: HandoffIdentity | None = None,
        harness: _HandoffActionHarness | None = None,
        store: HandoffEvidenceStore | None = None,
    ) -> tuple[
        HandoffIdentity,
        _Clock,
        HandoffEvidenceStore,
        OuterControllerHandoff,
        _HandoffActionHarness,
        LauncherHandoffConsumer,
    ]:
        clock = _Clock(datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc))
        evidence_store = store or HandoffEvidenceStore(Path(temporary) / "adoption-handoff.json")
        handoff_identity = identity or self.identity()
        controller = OuterControllerHandoff(evidence_store, clock=clock)
        controller.prepare(handoff_identity)
        action_harness = harness or _HandoffActionHarness()
        consumer = LauncherHandoffConsumer(controller, actions=action_harness.actions())
        return handoff_identity, clock, evidence_store, controller, action_harness, consumer

    def test_handoff_survives_worker_replacement_and_never_waits_for_initiator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-handoff-") as temporary:
            clock = _Clock(datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc))
            store = HandoffEvidenceStore(Path(temporary) / "adoption-handoff.json")
            identity = self.identity()
            controller = OuterControllerHandoff(store, clock=clock)
            prepared = controller.prepare(identity)
            self.assertEqual(prepared.phase, HandoffPhase.PREPARED)
            self.assertEqual(controller.prepare(identity), prepared)
            draining = controller.begin_draining(identity)
            self.assertEqual(draining.phase, HandoffPhase.DRAINING)
            started = time.monotonic()
            with self.assertRaises(HandoffNotQuiescedError) as raised:
                controller.record_worker_quiesced(
                    identity,
                    active_run_ids=(identity.initiating_run_id,),
                )
            self.assertEqual(raised.exception.args[0], "initiating_run_still_active")
            self.assertLess(time.monotonic() - started, 1.0)
            with self.assertRaises(HandoffNotQuiescedError):
                controller.record_worker_quiesced(identity, active_run_ids=("other-run",))
            with self.assertRaises(HandoffTransitionError):
                controller.mark_adopted(identity, observed_adoption_sha=SHA_ADOPTED)
            quiesced = controller.record_worker_quiesced(identity, active_run_ids=())
            self.assertTrue(quiesced.worker_boundary_quiesced)
            adopted = controller.mark_adopted(identity, observed_adoption_sha=SHA_ADOPTED)
            self.assertEqual(adopted.phase, HandoffPhase.ADOPTED)

            # Simulate old Worker death and a fresh Launcher-side controller
            # instance reading the same durable identity.
            replacement_controller = OuterControllerHandoff(store, clock=clock)
            self.assertEqual(replacement_controller.recover(identity).identity, identity)
            probation = replacement_controller.record_worker_restart(
                identity,
                launcher_identity=identity.expected_launcher_identity,
                worker_identity=identity.expected_worker_identity,
                worker_sha=SHA_ADOPTED,
            )
            self.assertEqual(probation.phase, HandoffPhase.PROBATION)
            early = HealthObservation(
                observed_at="2001-01-15T12:00:01+00:00",
                exit_code=75,
                worker_started_at="2001-01-15T12:00:00.500000+00:00",
                heartbeat_at="2001-01-15T12:00:01+00:00",
                launcher_alive=True,
                worker_healthy=True,
                protocol_ready=True,
                recovery_clear=True,
            )
            with self.assertRaises(HandoffHealthError):
                replacement_controller.mark_health_ready(
                    identity,
                    observation=early,
                    launcher_identity=identity.expected_launcher_identity,
                    worker_identity=identity.expected_worker_identity,
                    worker_sha=SHA_ADOPTED,
                )
            ready = replacement_controller.mark_health_ready(
                identity,
                observation=HealthObservation(
                    observed_at="2001-01-15T12:00:03+00:00",
                    exit_code=75,
                    worker_started_at="2001-01-15T12:00:00.500000+00:00",
                    heartbeat_at="2001-01-15T12:00:02.500000+00:00",
                    launcher_alive=True,
                    worker_healthy=True,
                    protocol_ready=True,
                    recovery_clear=True,
                ),
                launcher_identity=identity.expected_launcher_identity,
                worker_identity=identity.expected_worker_identity,
                worker_sha=SHA_ADOPTED,
            )
            self.assertEqual(ready.phase, HandoffPhase.COMPLETED)
            self.assertTrue(replacement_controller.claims_allowed(identity))
            payload = json.loads(store.path.read_text(encoding="utf-8"))["payload"]
            self.assertNotIn("command", payload)
            self.assertFalse(payload["identity"]["replay_initiating_command"])

    def test_launcher_factory_places_evidence_at_outer_runtime_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-launcher-boundary-") as temporary:
            controller = launcher.create_handoff_controller(Path(temporary))
            self.assertIsInstance(controller, OuterControllerHandoff)
            self.assertEqual(
                controller.store.path,
                (Path(temporary) / "adoption-handoff.json").resolve(),
            )

    def test_duplicate_corrupt_stale_and_forward_rollback_evidence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-handoff-integrity-") as temporary:
            path = Path(temporary) / "adoption-handoff.json"
            store = HandoffEvidenceStore(path)
            clock = _Clock(datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc))
            controller = OuterControllerHandoff(store, clock=clock)
            identity = self.identity()
            controller.prepare(identity)
            with self.assertRaises(HandoffConflictError):
                controller.prepare(self.identity(deadline=datetime(2001, 1, 15, 12, 5, tzinfo=timezone.utc)))
            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(HandoffIntegrityError):
                controller.recover(identity)

            stale_path = Path(temporary) / "stale.json"
            stale_store = HandoffEvidenceStore(stale_path)
            stale_controller = OuterControllerHandoff(stale_store, clock=clock)
            stale_identity = self.identity(deadline=datetime(2001, 1, 15, 12, 1, tzinfo=timezone.utc))
            stale_controller.prepare(stale_identity)
            clock.value = datetime(2001, 1, 15, 12, 2, tzinfo=timezone.utc)
            with self.assertRaises(HandoffStaleError):
                stale_controller.recover(stale_identity)

            with self.assertRaises(HandoffIntegrityError):
                self.identity(replay=True)

            rollback_clock = _Clock(datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc))
            rollback_store = HandoffEvidenceStore(Path(temporary) / "rollback.json")
            rollback_controller = OuterControllerHandoff(rollback_store, clock=rollback_clock)
            rollback_identity = self.identity()
            rollback_controller.prepare(rollback_identity)
            rollback_controller.begin_draining(rollback_identity)
            rollback_controller.record_worker_quiesced(rollback_identity, active_run_ids=())
            rollback_controller.mark_adopted(rollback_identity, observed_adoption_sha=SHA_ADOPTED)
            rollback_controller.record_worker_restart(
                rollback_identity,
                launcher_identity=rollback_identity.expected_launcher_identity,
                worker_identity=rollback_identity.expected_worker_identity,
                worker_sha=SHA_ADOPTED,
            )
            rollback_controller.request_rollback(rollback_identity, reason="health_gate_failed")
            with self.assertRaises(HandoffTransitionError):
                rollback_controller.mark_forward_rollback(
                    rollback_identity,
                    rollback_commit_sha=SHA_STABLE,
                    forward_only_verified=True,
                )
            rolled_back = rollback_controller.mark_forward_rollback(
                rollback_identity,
                rollback_commit_sha=SHA_ROLLBACK,
                forward_only_verified=True,
            )
            self.assertEqual(rolled_back.phase, HandoffPhase.ROLLED_BACK)
            self.assertFalse(rollback_controller.claims_allowed(rollback_identity))

    def test_consumer_requires_exit_and_quiescence_before_consuming_prepared(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-gate-") as temporary:
            identity, _, store, controller, harness, consumer = self._prepared_consumer(temporary)

            no_exit = consumer.try_consume_after_worker_exit()
            self.assertEqual(no_exit.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(no_exit.phase, HandoffPhase.PREPARED)
            self.assertEqual(harness.calls, Counter())
            self.assertEqual(store.read().phase, HandoffPhase.PREPARED)

            harness.stopped = False
            waiting_for_exit = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(waiting_for_exit.status, HandoffConsumeStatus.WAITING_FOR_WORKER_EXIT)
            self.assertEqual(store.read().phase, HandoffPhase.DRAINING)
            self.assertEqual(harness.events[:2], ["validate_identity", "worker_stopped"])

            harness.stopped = True
            harness.active = ("other-run",)
            waiting_for_quiescence = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(waiting_for_quiescence.status, HandoffConsumeStatus.WAITING_FOR_QUIESCENCE)
            self.assertEqual(store.read().phase, HandoffPhase.DRAINING)
            self.assertEqual(harness.calls["ensure_adopted"], 0)

            harness.active = ()
            completed = consumer.try_consume_after_worker_exit(
                worker_exit_code=75,
                expected_identity=identity,
            )
            self.assertEqual(completed.status, HandoffConsumeStatus.COMPLETED)
            self.assertTrue(completed.terminal)
            self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)
            self.assertTrue(controller.claims_allowed(identity))

    def test_stale_or_mismatched_identity_fails_before_outer_actions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-stale-") as temporary:
            stale_identity = self.identity(
                deadline=datetime(2001, 1, 15, 12, 1, tzinfo=timezone.utc),
            )
            _, clock, _, _, stale_harness, stale_consumer = self._prepared_consumer(
                temporary,
                identity=stale_identity,
            )
            clock.value = datetime(2001, 1, 15, 12, 2, tzinfo=timezone.utc)
            stale = stale_consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(stale.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(stale.reason, HandoffStaleError.__name__)
            self.assertEqual(stale_harness.calls, Counter())

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-identity-") as temporary:
            _, _, _, _, harness, consumer = self._prepared_consumer(temporary)
            harness.invalid_reason = "candidate_sha_stale"
            mismatched = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(mismatched.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(mismatched.reason, HandoffConflictError.__name__)
            self.assertEqual(harness.calls["validate_identity"], 1)
            self.assertEqual(harness.calls["worker_stopped"], 0)
            self.assertEqual(harness.calls["ensure_adopted"], 0)

    def test_corrupt_truncated_and_ambiguous_evidence_never_start_actions(self) -> None:
        identity = self.identity()
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-corrupt-") as temporary:
            root = Path(temporary)
            truncated_path = root / "truncated.json"
            truncated_path.write_text('{"schema_version":1', encoding="utf-8")
            truncated_store = HandoffEvidenceStore(truncated_path)
            truncated_controller = OuterControllerHandoff(truncated_store)
            truncated_harness = _HandoffActionHarness()
            truncated_consumer = LauncherHandoffConsumer(
                truncated_controller,
                actions=truncated_harness.actions(),
            )
            truncated = truncated_consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(truncated.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(truncated.reason, HandoffIntegrityError.__name__)
            self.assertEqual(truncated_harness.calls, Counter())

            mismatch_path = root / "mismatch.json"
            mismatch_store = HandoffEvidenceStore(mismatch_path)
            mismatch_controller = OuterControllerHandoff(mismatch_store)
            mismatch_controller.prepare(identity)
            envelope = json.loads(mismatch_path.read_text(encoding="utf-8"))
            envelope["payload_sha256"] = "0" * 64
            mismatch_path.write_text(json.dumps(envelope), encoding="utf-8")
            mismatch_harness = _HandoffActionHarness()
            mismatch_consumer = LauncherHandoffConsumer(
                mismatch_controller,
                actions=mismatch_harness.actions(),
            )
            mismatch = mismatch_consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(mismatch.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(mismatch.reason, HandoffIntegrityError.__name__)
            self.assertEqual(mismatch_harness.calls, Counter())

            ambiguous_path = root / "ambiguous.json"
            ambiguous_store = HandoffEvidenceStore(ambiguous_path)
            ambiguous_controller = OuterControllerHandoff(ambiguous_store)
            ambiguous_controller.prepare(identity)
            ambiguous = json.loads(ambiguous_path.read_text(encoding="utf-8"))
            ambiguous_payload = ambiguous["payload"]
            ambiguous_payload["phase"] = "AMBIGUOUS"
            ambiguous["payload_sha256"] = sha256(
                json.dumps(
                    ambiguous_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            ambiguous_path.write_text(json.dumps(ambiguous), encoding="utf-8")
            ambiguous_harness = _HandoffActionHarness()
            ambiguous_consumer = LauncherHandoffConsumer(
                ambiguous_controller,
                actions=ambiguous_harness.actions(),
            )
            ambiguous_result = ambiguous_consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(ambiguous_result.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(ambiguous_result.reason, HandoffIntegrityError.__name__)
            self.assertEqual(ambiguous_harness.calls, Counter())

    def test_crash_before_switch_is_recovered_without_duplicate_adoption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-switch-crash-") as temporary:
            _, _, store, controller, harness, consumer = self._prepared_consumer(temporary)
            harness.fail_after_once = "ensure_adopted"
            crashed = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(crashed.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(crashed.phase, HandoffPhase.QUIESCED)
            self.assertEqual(len(harness.adoption_effects), 1)

            resumed = LauncherHandoffConsumer(controller, actions=harness.actions())
            completed = resumed.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(completed.status, HandoffConsumeStatus.COMPLETED)
            self.assertEqual(len(harness.adoption_effects), 1)
            self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)

    def test_subprocess_crash_before_switch_survives_launcher_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-subprocess-") as temporary:
            identity, _, store, controller, harness, _ = self._prepared_consumer(
                temporary,
                identity=self.live_identity(),
            )
            effect_path = Path(temporary) / "adoption-effect.txt"
            script = r'''
import sys
from pathlib import Path

from vnext_runtime.adoption import HealthObservation
from vnext_runtime.handoff import (
    ForwardRollbackObservation,
    HandoffConsumeStatus,
    HandoffEvidenceStore,
    HandoffPhase,
    LauncherHandoffActions,
    LauncherHandoffConsumer,
    OuterControllerHandoff,
    WorkerRestartObservation,
)

store = HandoffEvidenceStore(Path(sys.argv[1]))
controller = OuterControllerHandoff(store)
identity = store.read().identity
effect = Path(sys.argv[2])

def validate(current):
    return None

def stopped(current):
    return True

def active(current):
    return ()

def adopted(current):
    effect.write_text("adoption-started", encoding="utf-8")
    raise RuntimeError("simulated launcher crash before handoff marker")

def restarted(current):
    return WorkerRestartObservation(
        launcher_identity=current.expected_launcher_identity,
        worker_identity=current.expected_worker_identity,
        worker_sha=current.reconciled_adoption_sha,
    )

def health(current):
    return HealthObservation(
        observed_at="2001-01-15T12:00:03+00:00",
        exit_code=75,
        worker_started_at="2001-01-15T12:00:00.500000+00:00",
        heartbeat_at="2001-01-15T12:00:02.500000+00:00",
        launcher_alive=True,
        worker_healthy=True,
        protocol_ready=True,
        recovery_clear=True,
    )

def rollback(current, reason):
    return ForwardRollbackObservation(
        rollback_commit_sha="f" * 40,
        forward_only_verified=True,
    )

actions = LauncherHandoffActions(
    validate_identity=validate,
    worker_stopped=stopped,
    active_run_ids=active,
    ensure_adopted=adopted,
    ensure_worker_restarted=restarted,
    observe_health=health,
    ensure_forward_rollback=rollback,
)
result = LauncherHandoffConsumer(controller, actions=actions).try_consume_after_worker_exit(
    worker_exit_code=75,
)
print(f"{result.status.value}:{result.phase.value if result.phase else None}")
if result.status is not HandoffConsumeStatus.BLOCKED or result.phase is not HandoffPhase.QUIESCED:
    raise SystemExit(2)
'''
            completed = subprocess.run(
                [sys.executable, "-c", script, str(store.path), str(effect_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env={**os.environ, "PYTHONPATH": str(ROOT / "worker")},
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}",
            )
            self.assertIn("BLOCKED:QUIESCED", completed.stdout)
            self.assertEqual(effect_path.read_text(encoding="utf-8"), "adoption-started")
            self.assertEqual(store.read().phase, HandoffPhase.QUIESCED)

            resumed = LauncherHandoffConsumer(controller, actions=harness.actions())
            self.assertEqual(
                resumed.try_consume_after_worker_exit(worker_exit_code=75).status,
                HandoffConsumeStatus.COMPLETED,
            )
            self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)

    def test_crash_during_restart_and_probation_bookkeeping_is_restart_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-restart-crash-") as temporary:
            _, _, store, controller, harness, consumer = self._prepared_consumer(temporary)
            harness.fail_after_once = "ensure_worker_restarted"
            crashed = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(crashed.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(crashed.phase, HandoffPhase.ADOPTED)
            self.assertEqual(len(harness.restart_effects), 1)

            resumed = LauncherHandoffConsumer(controller, actions=harness.actions())
            completed = resumed.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(completed.status, HandoffConsumeStatus.COMPLETED)
            self.assertEqual(len(harness.restart_effects), 1)
            self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-probation-crash-") as temporary:
            store = _FailOnceCompletionStore(Path(temporary) / "adoption-handoff.json")
            _, _, _, controller, harness, consumer = self._prepared_consumer(
                temporary,
                store=store,
            )
            crashed = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(crashed.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(crashed.phase, HandoffPhase.PROBATION)
            self.assertEqual(store.read().phase, HandoffPhase.PROBATION)

            resumed = LauncherHandoffConsumer(controller, actions=harness.actions())
            completed = resumed.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(completed.status, HandoffConsumeStatus.COMPLETED)
            self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)

    def test_terminal_completed_and_superseded_handoffs_never_replay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-terminal-") as temporary:
            _, _, store, controller, harness, consumer = self._prepared_consumer(temporary)
            first = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(first.status, HandoffConsumeStatus.COMPLETED)
            calls_after_first = harness.calls.copy()
            replay = LauncherHandoffConsumer(controller, actions=harness.actions()).try_consume_after_worker_exit(
                worker_exit_code=75,
            )
            self.assertEqual(replay.status, HandoffConsumeStatus.COMPLETED)
            self.assertEqual(harness.calls, calls_after_first)
            self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-superseded-") as temporary:
            identity, _, store, controller, harness, consumer = self._prepared_consumer(temporary)
            controller.mark_superseded(identity, reason="operator_closed")
            superseded = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(superseded.status, HandoffConsumeStatus.SUPERSEDED)
            self.assertTrue(superseded.terminal)
            self.assertEqual(harness.calls, Counter())
            self.assertFalse(controller.claims_allowed(identity))
            self.assertEqual(
                LauncherHandoffConsumer(controller, actions=harness.actions())
                .try_consume_after_worker_exit(worker_exit_code=75)
                .status,
                HandoffConsumeStatus.SUPERSEDED,
            )
            self.assertEqual(store.read().phase, HandoffPhase.SUPERSEDED)

    def test_probation_failure_uses_proven_forward_only_rollback(self) -> None:
        failed_health = HealthObservation(
            observed_at="2001-01-15T12:00:03+00:00",
            exit_code=75,
            worker_started_at="2001-01-15T12:00:00.500000+00:00",
            heartbeat_at="2001-01-15T12:00:02.500000+00:00",
            launcher_alive=True,
            worker_healthy=False,
            protocol_ready=True,
            recovery_clear=True,
        )
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-rollback-") as temporary:
            identity, _, store, controller, harness, consumer = self._prepared_consumer(
                temporary,
                harness=_HandoffActionHarness(health=failed_health),
            )
            result = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(result.status, HandoffConsumeStatus.ROLLED_BACK)
            self.assertTrue(result.terminal)
            self.assertIn("request_rollback", result.actions)
            self.assertIn("record_forward_rollback", result.actions)
            self.assertEqual(len(harness.rollback_effects), 1)
            self.assertEqual(harness.rollback_reasons, ["worker_healthy"])
            self.assertEqual(store.read().phase, HandoffPhase.ROLLED_BACK)
            self.assertFalse(controller.claims_allowed(identity))

    def test_unavailable_real_outer_actions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-consumer-unavailable-") as temporary:
            consumer = launcher.create_handoff_consumer(Path(temporary))
            identity = self.live_identity()
            consumer.controller.prepare(identity)
            result = consumer.try_consume_after_worker_exit(worker_exit_code=75)
            self.assertEqual(result.status, HandoffConsumeStatus.BLOCKED)
            self.assertEqual(result.phase, HandoffPhase.PREPARED)
            self.assertEqual(result.reason, "HandoffConsumerBlockedError")
            self.assertEqual(consumer.controller.store.read().phase, HandoffPhase.PREPARED)


if __name__ == "__main__":
    unittest.main()
