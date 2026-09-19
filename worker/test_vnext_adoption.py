"""Isolated acceptance tests for the Phase-8.5 adoption boundary."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vnext_runtime.adoption import (
    AdoptionAttemptEvidence,
    AdoptionDisabledError,
    AdoptionEvidenceStore,
    AdoptionGate,
    AdoptionMode,
    AdoptionPolicy,
    AdmissionBarrier,
    BarrierState,
    CandidateFreeze,
    CandidateValidationError,
    ConflictCategory,
    EvidenceIntegrityError,
    ForwardRollbackPlan,
    GitInspector,
    HealthGateContract,
    HealthGateStatus,
    HealthObservation,
    MainHeadStaleError,
    MainRevalidation,
    PromotionGuard,
    PromotionPlan,
    PromotionRejectedError,
    ReconciliationConflictError,
    RollbackIdentity,
    RollbackRejectedError,
    build_promotion_plan,
    freeze_candidate,
    plan_forward_rollback,
    plan_reconciliation,
    revalidate_latest_main,
)


SHA_BASE = "a" * 40
SHA_STABLE = "b" * 40
SHA_CANDIDATE = "c" * 40
SHA_MAIN = "d" * 40
SHA_ADOPTED = "e" * 40


def _run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _git_repo_with_candidate(root: Path) -> tuple[str, str]:
    root.mkdir()
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "core.autocrlf", "false")
    _run_git(root, "config", "user.email", "test@example.invalid")
    _run_git(root, "config", "user.name", "Phase 8.5 Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(root, "commit", "-m", "base")
    main_sha = _run_git(root, "rev-parse", "HEAD")
    _run_git(root, "switch", "-c", "feature/synthetic-candidate")
    (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _run_git(root, "add", "candidate.txt")
    _run_git(root, "commit", "-m", "candidate")
    return main_sha, _run_git(root, "rev-parse", "HEAD")


def _commit_file(root: Path, relative: str, content: bytes, message: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _run_git(root, "add", relative)
    _run_git(root, "commit", "-m", message)
    return _run_git(root, "rev-parse", "HEAD")


class Phase85IdentityTests(unittest.TestCase):
    def test_candidate_freeze_and_latest_main_revalidation_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-git-") as temp:
            root = Path(temp) / "candidate"
            main_sha, candidate_sha = _git_repo_with_candidate(root)
            frozen = freeze_candidate(
                root,
                bootstrap_base_sha=main_sha,
                candidate_branch="feature/synthetic-candidate",
                accepted_candidate_sha=candidate_sha,
            )
            self.assertEqual(frozen.accepted_candidate_sha, candidate_sha)
            self.assertNotEqual(frozen.accepted_candidate_sha, frozen.bootstrap_base_sha)
            latest = revalidate_latest_main(root, main_sha, ref="main")
            self.assertEqual(latest.latest_main_sha, main_sha)
            self.assertNotEqual(latest.latest_main_sha, frozen.accepted_candidate_sha)
            with self.assertRaises(MainHeadStaleError):
                revalidate_latest_main(root, candidate_sha, ref="main")

    def test_candidate_head_mismatch_is_rejected_before_any_adoption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-git-") as temp:
            root = Path(temp) / "candidate"
            main_sha, candidate_sha = _git_repo_with_candidate(root)
            with self.assertRaises(CandidateValidationError):
                freeze_candidate(
                    root,
                    bootstrap_base_sha=main_sha,
                    candidate_branch="feature/synthetic-candidate",
                    accepted_candidate_sha="f" * 40,
                )


class Phase85ReconciliationTests(unittest.TestCase):
    def _identities(self) -> tuple[CandidateFreeze, MainRevalidation]:
        now = "2001-01-15T12:00:00+00:00"
        return (
            CandidateFreeze(
                candidate_root=Path(tempfile.gettempdir()) / "candidate",
                candidate_branch="feature/synthetic-candidate",
                bootstrap_base_sha=SHA_BASE,
                accepted_candidate_sha=SHA_CANDIDATE,
                frozen_at=now,
            ),
            MainRevalidation(
                ref="main",
                expected_main_sha=SHA_MAIN,
                observed_main_sha=SHA_MAIN,
                revalidated_at=now,
            ),
        )

    def test_non_overlapping_source_changes_produce_history_preserving_plan(self) -> None:
        candidate, main = self._identities()
        plan = plan_reconciliation(
            candidate,
            main,
            candidate_changed_paths=("worker/vnext_runtime/adoption.py",),
            main_changed_paths=("projects/engine-maintenance/reports/report-017.md",),
            previous_stable_sha=SHA_STABLE,
        )
        self.assertTrue(plan.preserve_live_history)
        self.assertFalse(plan.force_operations_allowed)
        self.assertEqual(plan.previous_stable_sha, SHA_STABLE)
        self.assertEqual(plan.latest_main_sha, SHA_MAIN)

    def test_source_overlap_and_protected_boundaries_fail_closed(self) -> None:
        candidate, main = self._identities()
        cases = (
            ("worker/vnext_runtime/adoption.py", "source_overlap"),
            ("PROTOCOL.md", ConflictCategory.PROTOCOL_AUTHORITY.value),
            ("projects/engine-maintenance/state.json", ConflictCategory.PROJECT_CONTROL_PLANE.value),
            ("worker/runtime/worker-health.json", ConflictCategory.PROTECTED_PATH.value),
            ("worker/windows_worker_launcher.py", ConflictCategory.LAUNCHER_SAFETY.value),
            ("worker/recovery_journal.py", ConflictCategory.RECOVERY_IDENTITY.value),
        )
        for path, expected_category in cases:
            with self.subTest(path=path):
                main_paths = (path,) if expected_category == "source_overlap" else ()
                with self.assertRaises(ReconciliationConflictError) as raised:
                    plan_reconciliation(
                        candidate,
                        main,
                        candidate_changed_paths=(path,),
                        main_changed_paths=main_paths,
                    )
                self.assertEqual(raised.exception.conflicts[0].category.value, expected_category)


class Phase85DrainTests(unittest.TestCase):
    def test_drain_blocks_new_admission_and_waits_for_existing_run(self) -> None:
        barrier = AdmissionBarrier(max_active=2)
        first = barrier.try_admit("run-a")
        self.assertIsNotNone(first)
        self.assertEqual(barrier.active_count, 1)
        result: list[bool] = []
        waiter = threading.Thread(target=lambda: result.append(barrier.begin_draining()))
        waiter.start()
        waiter.join(timeout=0.05)
        self.assertTrue(waiter.is_alive())
        self.assertIsNone(barrier.try_admit("run-b"))
        self.assertEqual(barrier.state, BarrierState.DRAINING)
        self.assertTrue(first.close())
        waiter.join(timeout=2)
        self.assertEqual(result, [True])
        self.assertTrue(barrier.restart_ready)
        self.assertEqual(barrier.state, BarrierState.DRAINED)
        self.assertIsNone(barrier.try_admit("run-c"))

    def test_timeout_keeps_drain_closed_without_killing_the_lease(self) -> None:
        barrier = AdmissionBarrier()
        lease = barrier.try_admit("run-a")
        self.assertIsNotNone(lease)
        self.assertFalse(barrier.begin_draining(timeout_seconds=0.01))
        self.assertEqual(barrier.active_ids(), ("run-a",))
        self.assertEqual(barrier.state, BarrierState.DRAINING)
        lease.close()
        self.assertTrue(barrier.begin_draining(timeout_seconds=1))


class Phase85PromotionRollbackTests(unittest.TestCase):
    def _promotion(self) -> PromotionPlan:
        candidate = CandidateFreeze(
            candidate_root=Path(tempfile.gettempdir()) / "candidate",
            candidate_branch="feature/synthetic-candidate",
            bootstrap_base_sha=SHA_BASE,
            accepted_candidate_sha=SHA_CANDIDATE,
            frozen_at="2001-01-15T12:00:00+00:00",
        )
        main = MainRevalidation(
            ref="main",
            expected_main_sha=SHA_MAIN,
            observed_main_sha=SHA_MAIN,
            revalidated_at="2001-01-15T12:00:00+00:00",
        )
        reconciliation = plan_reconciliation(
            candidate,
            main,
            candidate_changed_paths=("worker/vnext_runtime/adoption.py",),
            main_changed_paths=(),
            previous_stable_sha=SHA_STABLE,
        )
        return build_promotion_plan(
            reconciliation,
            SHA_ADOPTED,
            revalidated_at="2001-01-15T12:00:00+00:00",
        )

    def test_promotion_requires_fresh_head_and_non_force_descendant(self) -> None:
        plan = self._promotion()
        guard = PromotionGuard()
        guard.validate(
            plan,
            current_integration_head_sha=SHA_MAIN,
            adoption_is_descendant=True,
        )
        with self.assertRaises(PromotionRejectedError):
            guard.validate(
                plan,
                current_integration_head_sha=SHA_STABLE,
                adoption_is_descendant=True,
            )
        with self.assertRaises(PromotionRejectedError):
            guard.validate(
                plan,
                current_integration_head_sha=SHA_MAIN,
                adoption_is_descendant=True,
                force=True,
            )

    def test_forward_rollback_preserves_history_and_outer_authority(self) -> None:
        plan = plan_forward_rollback(self._promotion())
        self.assertIsInstance(plan, ForwardRollbackPlan)
        self.assertEqual(plan.operation, "create_forward_revert_commit")
        self.assertTrue(plan.preserves_control_plane_history)
        self.assertFalse(plan.depends_on_adopted_worker)
        self.assertFalse(plan.uses_reset)
        self.assertFalse(plan.uses_force_push)
        self.assertTrue(plan.no_blind_rerun)


class Phase85SyntheticGitIntegrationTests(unittest.TestCase):
    def test_reconcile_promote_and_forward_rollback_preserve_project_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-integration-") as temp:
            root = Path(temp) / "candidate"
            bootstrap_sha, candidate_sha = _git_repo_with_candidate(root)
            report_bytes = b"append-only report from latest main\n"
            _run_git(root, "switch", "main")
            latest_main_sha = _commit_file(
                root,
                "projects/engine-maintenance/reports/report-017.md",
                report_bytes,
                "control-plane report",
            )
            _run_git(root, "switch", "feature/synthetic-candidate")

            inspector = GitInspector(root)
            frozen = freeze_candidate(
                root,
                bootstrap_base_sha=bootstrap_sha,
                candidate_branch="feature/synthetic-candidate",
                accepted_candidate_sha=candidate_sha,
            )
            latest = revalidate_latest_main(root, latest_main_sha, ref="main")
            reconciliation = plan_reconciliation(
                frozen,
                latest,
                candidate_changed_paths=inspector.changed_paths(bootstrap_sha, candidate_sha),
                main_changed_paths=inspector.changed_paths(bootstrap_sha, latest_main_sha),
                previous_stable_sha=bootstrap_sha,
            )

            # Build the reconciled tree in a disposable repository.  The
            # production boundary only plans/guards this operation; the test
            # performs the non-force Git operation in its own fixture.
            _run_git(root, "switch", "main")
            _run_git(root, "switch", "-c", "synthetic-adoption", "main")
            _run_git(
                root,
                "merge",
                "--no-ff",
                "feature/synthetic-candidate",
                "-m",
                "synthetic reconciliation",
            )
            reconciled_sha = _run_git(root, "rev-parse", "HEAD")
            _run_git(root, "switch", "main")
            promotion = build_promotion_plan(reconciliation, reconciled_sha)
            PromotionGuard().validate_with_git(
                promotion,
                repository=inspector,
                current_integration_ref="main",
            )
            _run_git(root, "merge", "--ff-only", reconciled_sha)
            self.assertEqual(_run_git(root, "rev-parse", "main"), reconciled_sha)
            self.assertEqual(
                (root / "projects/engine-maintenance/reports/report-017.md").read_bytes(),
                report_bytes,
            )

            # A new control-plane event lands after adoption.  The outer
            # controller must preserve it while forward-reverting the adopted
            # source change, without depending on the adopted Worker.
            control_bytes = b"new control-plane history during probation\n"
            control_sha = _commit_file(
                root,
                "projects/engine-maintenance/reports/report-018.md",
                control_bytes,
                "control-plane event during probation",
            )
            rollback = plan_forward_rollback(
                promotion,
                current_adoption_sha=control_sha,
                adopted_head_is_ancestor=inspector.is_ancestor(reconciled_sha, control_sha),
            )
            self.assertFalse(rollback.depends_on_adopted_worker)
            _run_git(root, "revert", "-m", "1", reconciled_sha, "--no-edit")
            rollback_commit = _run_git(root, "rev-parse", "HEAD")
            self.assertEqual(rollback.current_adoption_sha, control_sha)
            self.assertTrue(inspector.is_ancestor(control_sha, rollback_commit))
            self.assertFalse((root / "candidate.txt").exists())
            self.assertEqual(
                (root / "projects/engine-maintenance/reports/report-017.md").read_bytes(),
                report_bytes,
            )
            self.assertEqual(
                (root / "projects/engine-maintenance/reports/report-018.md").read_bytes(),
                control_bytes,
            )

    def test_stale_main_race_aborts_before_forward_only_ref_move(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-stale-") as temp:
            root = Path(temp) / "candidate"
            bootstrap_sha, candidate_sha = _git_repo_with_candidate(root)
            _run_git(root, "switch", "main")
            latest_main_sha = _commit_file(root, "main.txt", b"latest\n", "latest main")
            _run_git(root, "switch", "feature/synthetic-candidate")
            inspector = GitInspector(root)
            frozen = freeze_candidate(
                root,
                bootstrap_base_sha=bootstrap_sha,
                candidate_branch="feature/synthetic-candidate",
                accepted_candidate_sha=candidate_sha,
            )
            latest = revalidate_latest_main(root, latest_main_sha, ref="main")
            reconciliation = plan_reconciliation(
                frozen,
                latest,
                candidate_changed_paths=inspector.changed_paths(bootstrap_sha, candidate_sha),
                main_changed_paths=inspector.changed_paths(bootstrap_sha, latest_main_sha),
                previous_stable_sha=bootstrap_sha,
            )
            promotion = build_promotion_plan(reconciliation, candidate_sha)
            _run_git(root, "switch", "main")
            moved_main_sha = _commit_file(root, "main-race.txt", b"race\n", "main race")
            _run_git(root, "switch", "feature/synthetic-candidate")
            with self.assertRaises(PromotionRejectedError):
                PromotionGuard().validate_with_git(
                    promotion,
                    repository=inspector,
                    current_integration_ref="main",
                )
            self.assertEqual(_run_git(root, "rev-parse", "main"), moved_main_sha)

    def test_rollback_construction_failure_is_durable_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-rollback-failure-") as temp:
            evidence_path = Path(temp) / "adoption-attempt.json"
            evidence = AdoptionAttemptEvidence(
                attempt_id="attempt-rollback-failure",
                recorded_at="2001-01-15T12:00:00+00:00",
                bootstrap_base_sha=SHA_BASE,
                previous_stable_sha=SHA_STABLE,
                accepted_candidate_sha=SHA_CANDIDATE,
                latest_main_sha=SHA_MAIN,
                reconciled_adoption_sha=SHA_ADOPTED,
                health_deadline="2001-01-15T12:10:00+00:00",
                health_criteria=("launcher_alive", "worker_healthy", "protocol_ready", "recovery_clear"),
                rollback_identity=RollbackIdentity(known_good_sha=SHA_STABLE),
                mode=AdoptionMode.MANUAL,
                status="ROLLBACK_FAILED",
                failure_reason="known_good_history_not_verified",
            )
            with self.assertRaises(RollbackRejectedError):
                plan_forward_rollback(
                    self._promotion_for_failure(),
                    stable_is_ancestor_of_integration=False,
                )
            store = AdoptionEvidenceStore(evidence_path)
            store.write(evidence)
            loaded = store.read()
            self.assertEqual(loaded.status, "ROLLBACK_FAILED")
            self.assertEqual(loaded.failure_reason, "known_good_history_not_verified")

    @staticmethod
    def _promotion_for_failure() -> PromotionPlan:
        return PromotionPlan(
            target_branch="main",
            expected_integration_head_sha=SHA_MAIN,
            previous_stable_sha=SHA_STABLE,
            accepted_candidate_sha=SHA_CANDIDATE,
            reconciled_adoption_sha=SHA_ADOPTED,
            revalidated_at="2001-01-15T12:00:00+00:00",
        )


class Phase85EvidenceAndHealthTests(unittest.TestCase):
    def _evidence(self) -> AdoptionAttemptEvidence:
        recorded = datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc)
        return AdoptionAttemptEvidence(
            attempt_id="attempt-017",
            recorded_at=recorded.isoformat(),
            bootstrap_base_sha=SHA_BASE,
            previous_stable_sha=SHA_STABLE,
            accepted_candidate_sha=SHA_CANDIDATE,
            latest_main_sha=SHA_MAIN,
            reconciled_adoption_sha=SHA_ADOPTED,
            health_deadline=(recorded + timedelta(minutes=10)).isoformat(),
            health_criteria=("launcher_alive", "worker_healthy", "protocol_ready"),
            rollback_identity=RollbackIdentity(known_good_sha=SHA_STABLE),
            mode=AdoptionMode.MANUAL,
        )

    def test_evidence_store_is_atomic_bounded_and_integrity_checked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-evidence-") as temp:
            path = Path(temp) / "adoption-attempt.json"
            store = AdoptionEvidenceStore(path)
            evidence = self._evidence()
            store.write(evidence)
            self.assertEqual(store.read(), evidence)
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["payload"]["accepted_candidate_sha"] = "f" * 40
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaises(EvidenceIntegrityError):
                store.read()

    def test_evidence_rejects_secret_like_health_criteria(self) -> None:
        with self.assertRaises(EvidenceIntegrityError):
            AdoptionAttemptEvidence.from_payload(
                {**self._evidence().to_payload(), "health_criteria": ["token=secret"]}
            )

    def test_health_gate_holds_claims_during_restart_and_probation(self) -> None:
        requested = datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc)
        contract = HealthGateContract(
            restart_requested_at=requested.isoformat(),
            health_deadline=(requested + timedelta(minutes=5)).isoformat(),
            probation_seconds=30,
            health_criteria=("launcher_alive", "worker_healthy"),
        )
        waiting = contract.evaluate(
            HealthObservation(
                observed_at=(requested + timedelta(seconds=1)).isoformat(),
                exit_code=75,
                worker_started_at=None,
                heartbeat_at=None,
                launcher_alive=True,
                worker_healthy=False,
                protocol_ready=False,
                recovery_clear=False,
            )
        )
        self.assertEqual(waiting.status, HealthGateStatus.WAITING_FOR_RESTART)
        self.assertFalse(waiting.claims_allowed)
        probation = contract.evaluate(
            HealthObservation(
                observed_at=(requested + timedelta(seconds=10)).isoformat(),
                exit_code=75,
                worker_started_at=(requested + timedelta(seconds=2)).isoformat(),
                heartbeat_at=(requested + timedelta(seconds=9)).isoformat(),
                launcher_alive=True,
                worker_healthy=True,
                protocol_ready=True,
                recovery_clear=True,
            )
        )
        self.assertEqual(probation.status, HealthGateStatus.PROBATION)
        self.assertFalse(probation.claims_allowed)
        ready = contract.evaluate(
            HealthObservation(
                observed_at=(requested + timedelta(seconds=40)).isoformat(),
                exit_code=75,
                worker_started_at=(requested + timedelta(seconds=2)).isoformat(),
                heartbeat_at=(requested + timedelta(seconds=39)).isoformat(),
                launcher_alive=True,
                worker_healthy=True,
                protocol_ready=True,
                recovery_clear=True,
            )
        )
        self.assertEqual(ready.status, HealthGateStatus.READY)
        self.assertTrue(ready.claims_allowed)

    def test_unattended_and_manual_adoption_are_disabled_by_default(self) -> None:
        gate = AdoptionGate()
        self.assertFalse(gate.can_authorize(AdoptionMode.UNATTENDED, explicit_controlled_mode=True))
        with self.assertRaises(AdoptionDisabledError):
            gate.authorize(AdoptionMode.MANUAL, explicit_controlled_mode=True)
        enabled = AdoptionGate(
            AdoptionPolicy(
                controlled_adoption_enabled=True,
                unattended_adoption_enabled=False,
            )
        )
        self.assertTrue(enabled.can_authorize(AdoptionMode.MANUAL, explicit_controlled_mode=True))
        self.assertFalse(enabled.can_authorize(AdoptionMode.UNATTENDED, explicit_controlled_mode=True))


if __name__ == "__main__":
    unittest.main()
