"""Isolated end-to-end acceptance for synthetic Phase-8.5 Launcher actions."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bridge_worker_hardened import _handoff_claims_allowed
import worker_health
import windows_worker_launcher as launcher
from vnext_runtime.adoption import AdoptionMode, AdoptionPolicy, RollbackIdentity
from vnext_runtime.handoff import (
    HandoffConflictError,
    HandoffConsumeStatus,
    HandoffEvidenceStore,
    HandoffIdentity,
    HandoffPhase,
    LauncherHandoffConsumer,
    OuterControllerHandoff,
)
from vnext_runtime.launcher_actions import (
    FaultInjection,
    LauncherActionConfig,
    LauncherActionError,
    LauncherOwnedHandoffActions,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def _commit(root: Path, relative: str, content: str, message: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", relative)
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _fixture(root: Path) -> dict[str, Path | str]:
    """Build main/latest/candidate/integration refs and an isolated bare origin."""

    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "user.name", "Phase 8.5 Launcher Test")
    _git(root, "config", "user.email", "phase85-launcher@example.invalid")
    (root / ".gitignore").write_text(
        "worker/runtime/\nworker/logs/\n", encoding="utf-8"
    )
    (root / "worker" / "worker_stub.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "worker" / "worker_stub.py").write_text(
        '''from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

runtime = Path(os.environ.get(
    "BRIDGE_WORKER_RUNTIME_ROOT",
    str(Path(__file__).resolve().parent / "runtime"),
)).resolve()
runtime.mkdir(parents=True, exist_ok=True)
health = runtime / "worker-health.json"
started = datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")
temporary = runtime / f".worker-health.{os.getpid()}.tmp"
while True:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")
    payload = {
        "schema_version": 1,
        "updated_at": now,
        "worker_pid": os.getpid(),
        "worker_started_at": started,
        "last_worker_start": started,
        "last_poll_at": now,
        "last_successful_poll_at": now,
        "last_successful_fetch_at": now,
        "last_successful_fetch": now,
        "poll_count": 1,
        "active_run_count": 0,
        "active_runs": [],
        "last_failure_kind": None,
    }
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, health)
    time.sleep(0.02)
''',
        encoding="utf-8",
    )
    (root / "worker" / "fixture-config.json").write_text(
        json.dumps(
            {
                "runtime": {"max_parallel_runs": 1},
                "adoption": {
                    "controlled_adoption_enabled": True,
                    "unattended_adoption_enabled": False,
                },
                "projects": {},
            }
        ),
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "stable launcher fixture")
    base = _git(root, "rev-parse", "HEAD")

    _git(root, "switch", "-c", "feature/synthetic-candidate")
    candidate = _commit(
        root,
        "worker/candidate-runtime.txt",
        "candidate-v1\n",
        "candidate runtime",
    )

    _git(root, "switch", "main")
    latest = _commit(
        root,
        "projects/example-service/reports/report-002.md",
        "append-only project history\n",
        "append project history",
    )

    _git(root, "switch", "-c", "adoption-target", "main")
    _git(root, "merge", "--no-ff", "feature/synthetic-candidate", "-m", "integrate candidate")
    target = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")

    remote = root.parent / "origin.git"
    _git(remote.parent, "init", "--bare", str(remote))
    _git(root, "remote", "add", "origin", remote.as_uri())
    _git(root, "push", "origin", "main")
    return {
        "root": root,
        "remote": remote,
        "base": base,
        "candidate": candidate,
        "latest": latest,
        "target": target,
        "runtime": root / "worker" / "runtime",
        "worker_script": root / "worker" / "worker_stub.py",
        "config": root / "worker" / "fixture-config.json",
        "log": root / "worker" / "logs" / "launcher.log",
    }


def _identity(
    fixture: dict[str, Path | str],
    *,
    attempt_id: str,
    command_id: int,
    claim_generation: int,
    previous: str | None = None,
    latest: str | None = None,
    candidate: str | None = None,
    target: str | None = None,
) -> HandoffIdentity:
    now = datetime.now(timezone.utc)
    previous = previous or str(fixture["base"])
    latest = latest or str(fixture["latest"])
    candidate = candidate or str(fixture["candidate"])
    target = target or str(fixture["target"])
    return HandoffIdentity(
        attempt_id=attempt_id,
        initiated_at=now.isoformat(),
        previous_stable_sha=previous,
        latest_main_sha=latest,
        accepted_candidate_sha=candidate,
        reconciled_adoption_sha=target,
        initiating_project_id="example-service",
        initiating_run_id=f"run-{command_id:03d}",
        initiating_command_id=command_id,
        initiating_claim_generation=claim_generation,
        expected_launcher_identity="outer-launcher-v1",
        expected_worker_identity="worker-vnext-v1",
        startup_contract=(
            "outer_launcher_job",
            "exit_75_restart",
            "health_gate",
            "worker_claim_gate",
        ),
        health_deadline=(now + timedelta(seconds=45)).isoformat(),
        health_criteria=(
            "launcher_alive",
            "worker_healthy",
            "protocol_ready",
            "recovery_clear",
        ),
        probation_seconds=0,
        rollback_identity=RollbackIdentity(known_good_sha=previous),
        mode=AdoptionMode.MANUAL,
    )


def _actions(
    fixture: dict[str, Path | str],
    *,
    fault: FaultInjection = FaultInjection.NONE,
    fault_injection_command_id: int | None = None,
) -> LauncherOwnedHandoffActions:
    return LauncherOwnedHandoffActions(
        LauncherActionConfig(
            repository_root=Path(fixture["root"]),
            runtime_root=Path(fixture["runtime"]),
            worker_script=Path(fixture["worker_script"]),
            config_path=Path(fixture["config"]),
            log_file=Path(fixture["log"]),
            adoption_policy=AdoptionPolicy(
                controlled_adoption_enabled=True,
                unattended_adoption_enabled=False,
            ),
            expected_remote=Path(fixture["remote"]).as_uri(),
            worker_start_timeout_seconds=5,
            worker_health_poll_seconds=0.02,
            rollback_ready_timeout_seconds=5,
            fault_injection=fault,
            fault_injection_command_id=fault_injection_command_id,
        )
    )


def _drive(
    consumer: LauncherHandoffConsumer,
    *,
    timeout_seconds: float = 12,
) -> object:
    deadline = time.monotonic() + timeout_seconds
    result = None
    while time.monotonic() < deadline:
        result = consumer.try_consume_after_worker_exit(worker_exit_code=75)
        if result.terminal or result.status is HandoffConsumeStatus.BLOCKED:
            return result
        time.sleep(0.02)
    raise AssertionError(f"handoff did not reach a terminal result: {result!r}")


def _safe_git_sha(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "<unavailable>"
    value = completed.stdout.strip().split()
    if (
        completed.returncode != 0
        or len(value) != 1
        or len(value[0]) != 40
        or any(char not in "0123456789abcdefABCDEF" for char in value[0])
    ):
        return "<unavailable>"
    return value[0]


def _diagnostic_summary(store: HandoffEvidenceStore, fixture: dict[str, Path | str]) -> str:
    evidence = store.read_optional()
    phase = evidence.phase.value if evidence is not None else "<none>"
    transition_seq = evidence.transition_seq if evidence is not None else "<none>"
    operations: set[str] = set()
    for receipt_path in Path(fixture["runtime"]).glob("adoption-action-receipt*.json"):
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("operation") in {"adopt", "rollback"}:
            operations.add(payload["operation"])
    return (
        f"handoff_phase={phase} transition_seq={transition_seq} "
        f"local_head={_safe_git_sha(Path(fixture['root']), 'rev-parse', 'HEAD')} "
        f"remote_main_sha={_safe_git_sha(Path(fixture['root']), 'ls-remote', 'origin', 'refs/heads/main')} "
        f"adopt_receipt={'yes' if 'adopt' in operations else 'no'} "
        f"rollback_receipt={'yes' if 'rollback' in operations else 'no'}"
    )


def _drive_strict(
    consumer: LauncherHandoffConsumer,
    *,
    store: HandoffEvidenceStore,
    fixture: dict[str, Path | str],
    timeout_seconds: float = 12,
) -> object:
    deadline = time.monotonic() + timeout_seconds
    result = None
    while time.monotonic() < deadline:
        # Deliberately use the strict consumer so the CI traceback preserves
        # the exact HandoffError class and message.  Waiting statuses retain
        # the same bounded polling behavior as _drive().
        result = consumer.consume_after_worker_exit(worker_exit_code=75)
        if result.terminal or result.status is HandoffConsumeStatus.BLOCKED:
            return result
        time.sleep(0.02)
    raise AssertionError(
        "handoff did not reach a terminal result: "
        f"{result!r}; {_diagnostic_summary(store, fixture)}"
    )


def _stop(process: object | None) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    except Exception:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        except Exception:
            pass


class SyntheticLauncherActionAcceptanceTests(unittest.TestCase):
    def test_production_launcher_factory_arms_only_explicit_controlled_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-phase85-wiring-") as temporary:
            fixture = _fixture(Path(temporary) / "checkout")
            armed = launcher.create_launcher_action_controller(
                repository_root=Path(fixture["root"]),
                runtime_root=Path(fixture["runtime"]),
                worker_script=Path(fixture["worker_script"]),
                config_path=Path(fixture["config"]),
                log_file=Path(fixture["log"]),
                policy=AdoptionPolicy(
                    controlled_adoption_enabled=True,
                    unattended_adoption_enabled=False,
                ),
            )
            self.assertIsInstance(armed, LauncherOwnedHandoffActions)
            self.assertIsNotNone(armed.as_actions())
            armed.close()
            self.assertIsNone(
                launcher.create_launcher_action_controller(
                    repository_root=Path(fixture["root"]),
                    runtime_root=Path(fixture["runtime"]),
                    worker_script=Path(fixture["worker_script"]),
                    config_path=Path(fixture["config"]),
                    log_file=Path(fixture["log"]),
                    policy=AdoptionPolicy(),
                )
            )

    def _prepare(
        self,
        fixture: dict[str, Path | str],
        identity: HandoffIdentity,
    ) -> tuple[HandoffEvidenceStore, OuterControllerHandoff, LauncherHandoffConsumer, LauncherOwnedHandoffActions]:
        runtime = Path(fixture["runtime"])
        worker_health.update_worker_health(
            Path(fixture["root"]),
            runtime_root=runtime,
            worker_pid=None,
            active_run_count=0,
            active_runs=[],
            max_parallel_runs=1,
        )
        store = HandoffEvidenceStore(runtime / "adoption-handoff.json")
        controller = OuterControllerHandoff(store)
        controller.prepare(identity)
        action = _actions(fixture)
        action.bind_worker_exit(None, 75)
        consumer = LauncherHandoffConsumer(controller, actions=action.as_actions())
        return store, controller, consumer, action

    def test_synthetic_prepared_ready_duplicate_and_restart_resume(self) -> None:
        """The concrete callbacks survive a crash at durable PROBATION."""

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-synthetic-") as temporary:
            fixture = _fixture(Path(temporary) / "checkout")
            identity = _identity(
                fixture,
                attempt_id="attempt-synthetic-restart",
                command_id=2,
                claim_generation=10,
            )
            store, controller, consumer, first_action = self._prepare(fixture, identity)
            first_process = None
            second_process = None
            resumed_action = None
            try:
                # Drive the action boundary explicitly to PROBATION, then
                # terminate that action-owned process to model Launcher crash.
                controller.begin_draining(identity)
                self.assertTrue(first_action.worker_stopped(identity))
                self.assertEqual(first_action.active_run_ids(identity), ())
                controller.record_worker_quiesced(identity, active_run_ids=())
                self.assertEqual(first_action.ensure_adopted(identity), str(fixture["target"]))
                controller.mark_adopted(
                    identity,
                    observed_adoption_sha=str(fixture["target"]),
                )
                restart = first_action.ensure_worker_restarted(identity)
                controller.record_worker_restart(
                    identity,
                    launcher_identity=restart.launcher_identity,
                    worker_identity=restart.worker_identity,
                    worker_sha=restart.worker_sha,
                )
                self.assertEqual(store.read().phase, HandoffPhase.PROBATION)
                with patch.dict(
                    os.environ,
                    {
                        "BRIDGE_HANDOFF_EVIDENCE_PATH": str(
                            Path(fixture["runtime"]) / "adoption-handoff.json"
                        ),
                        "BRIDGE_HANDOFF_ATTEMPT_ID": identity.attempt_id,
                        "BRIDGE_HANDOFF_ALLOWED_PHASES": "COMPLETED",
                    },
                    clear=False,
                ):
                    self.assertFalse(
                        _handoff_claims_allowed(
                            Path(fixture["root"]),
                            runtime_root=Path(fixture["runtime"]),
                        )
                    )
                first_process = first_action._replacement_process
                first_action.close()
                first_process = None

                # A fresh outer action object binds the same attempt-specific
                # receipt and resumes the durable PROBATION phase.  No merge
                # or initiating command is replayed.
                resumed_action = _actions(fixture)
                resumed_action.bind_worker_exit(None, 75)
                resumed_consumer = LauncherHandoffConsumer(
                    controller,
                    actions=resumed_action.as_actions(),
                )
                completed = _drive(resumed_consumer)
                self.assertEqual(completed.status, HandoffConsumeStatus.COMPLETED)
                self.assertEqual(store.read().phase, HandoffPhase.COMPLETED)
                with patch.dict(
                    os.environ,
                    {
                        "BRIDGE_HANDOFF_EVIDENCE_PATH": str(
                            Path(fixture["runtime"]) / "adoption-handoff.json"
                        ),
                        "BRIDGE_HANDOFF_ATTEMPT_ID": identity.attempt_id,
                        "BRIDGE_HANDOFF_ALLOWED_PHASES": "COMPLETED",
                    },
                    clear=False,
                ):
                    self.assertTrue(
                        _handoff_claims_allowed(
                            Path(fixture["root"]),
                            runtime_root=Path(fixture["runtime"]),
                        )
                    )
                second_process = resumed_action.take_replacement_process()
                self.assertIsNotNone(second_process)
                self.assertEqual(_git(Path(fixture["root"]), "rev-parse", "HEAD"), str(fixture["target"]))
                self.assertEqual(
                    _git(Path(fixture["root"]), "ls-remote", "origin", "refs/heads/main").split()[0],
                    str(fixture["target"]),
                )
                self.assertTrue(
                    (Path(fixture["root"]) / "projects/example-service/reports/report-002.md").is_file()
                )
                self.assertEqual(
                    resumed_consumer.try_consume_after_worker_exit(worker_exit_code=75).status,
                    HandoffConsumeStatus.COMPLETED,
                )
                self.assertIsNone(resumed_action.take_replacement_process())
                self.assertEqual(len(list(Path(fixture["runtime"]).glob("adoption-action-receipt-*.json"))), 1)
            finally:
                _stop(first_process)
                _stop(second_process)
                if resumed_action is not None:
                    resumed_action.close()
                first_action.close()

    def test_synthetic_probation_failure_forward_rollback_and_clean_readoption(self) -> None:
        """A synthetic health fault reverts forward, preserves projects, then re-adopts current main."""

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-synthetic-rollback-") as temporary:
            fixture = _fixture(Path(temporary) / "checkout")
            identity = _identity(
                fixture,
                attempt_id="attempt-synthetic-rollback",
                command_id=2,
                claim_generation=10,
            )
            store, controller, consumer, action = self._prepare(fixture, identity)
            action.close()
            action = _actions(
                fixture,
                fault=FaultInjection.PROBATION_FAILURE,
                fault_injection_command_id=2,
            )
            action.bind_worker_exit(None, 75)
            consumer = LauncherHandoffConsumer(controller, actions=action.as_actions())
            rollback_process = None
            readopt_process = None
            try:
                rolled = _drive_strict(
                    consumer,
                    store=store,
                    fixture=fixture,
                )
                self.assertEqual(
                    rolled.status,
                    HandoffConsumeStatus.ROLLED_BACK,
                    _diagnostic_summary(store, fixture),
                )
                evidence = store.read()
                self.assertEqual(evidence.phase, HandoffPhase.ROLLED_BACK)
                rollback_sha = evidence.rollback_commit_sha
                self.assertIsNotNone(rollback_sha)
                assert rollback_sha is not None
                self.assertTrue(
                    _git(Path(fixture["root"]), "merge-base", "--is-ancestor", str(fixture["target"]), rollback_sha)
                    == ""
                )
                self.assertEqual(_git(Path(fixture["root"]), "rev-parse", "HEAD"), rollback_sha)
                self.assertEqual(
                    _git(Path(fixture["root"]), "ls-remote", "origin", "refs/heads/main").split()[0],
                    rollback_sha,
                )
                self.assertFalse((Path(fixture["root"]) / "worker/candidate-runtime.txt").exists())
                self.assertTrue(
                    (Path(fixture["root"]) / "projects/example-service/reports/report-002.md").is_file()
                )
                rollback_process = action.take_replacement_process()
                _stop(rollback_process)
                rollback_process = None

                # Build a new candidate from the then-current rollback main.
                root = Path(fixture["root"])
                _git(root, "switch", "-c", "feature/re-adopt")
                candidate2 = _commit(
                    root,
                    "worker/candidate-runtime.txt",
                    "candidate-v2\n",
                    "candidate runtime v2",
                )
                _git(root, "switch", "main")
                latest2 = str(rollback_sha)
                _git(root, "switch", "-c", "adoption-target-2", "main")
                _git(root, "merge", "--no-ff", "feature/re-adopt", "-m", "integrate candidate v2")
                target2 = _git(root, "rev-parse", "HEAD")
                _git(root, "switch", "main")
                identity2 = _identity(
                    fixture,
                    attempt_id="attempt-synthetic-readopt",
                    command_id=3,
                    claim_generation=11,
                    previous=str(rollback_sha),
                    latest=latest2,
                    candidate=candidate2,
                    target=target2,
                )
                # The terminal prior attempt is replaced only by a strictly
                # newer successor whose stable/latest base is the rollback
                # tree.  A stale base is rejected before any Git action.
                with self.assertRaises(HandoffConflictError):
                    controller.prepare(
                        _identity(
                            fixture,
                            attempt_id="attempt-stale-readopt",
                            command_id=3,
                            claim_generation=11,
                            previous=str(fixture["base"]),
                            latest=str(fixture["base"]),
                            candidate=str(fixture["candidate"]),
                            target=str(fixture["target"]),
                        )
                    )
                controller.prepare(identity2)
                readopt_action = _actions(fixture)
                readopt_action.bind_worker_exit(None, 75)
                readopt_consumer = LauncherHandoffConsumer(
                    controller,
                    actions=readopt_action.as_actions(),
                )
                readopted = _drive(readopt_consumer)
                self.assertEqual(readopted.status, HandoffConsumeStatus.COMPLETED, f"{_diagnostic_summary(store, fixture)} {store.read()!r}")
                self.assertEqual(store.read().identity, identity2)
                self.assertEqual(_git(root, "rev-parse", "HEAD"), target2)
                self.assertEqual(
                    _git(root, "ls-remote", "origin", "refs/heads/main").split()[0],
                    target2,
                )
                self.assertTrue((root / "worker/candidate-runtime.txt").is_file())
                self.assertTrue((root / "projects/example-service/reports/report-002.md").is_file())
                readopt_process = readopt_action.take_replacement_process()
                self.assertIsNotNone(readopt_process)
                readopt_action.close()
            finally:
                _stop(rollback_process)
                _stop(readopt_process)
                action.close()

    def test_synthetic_rollback_receipt_crash_is_discovered_and_resumed(self) -> None:
        """A crash after Git revert but before its receipt never causes a rewind."""

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-synthetic-revert-crash-") as temporary:
            fixture = _fixture(Path(temporary) / "checkout")
            identity = _identity(
                fixture,
                attempt_id="attempt-synthetic-revert-crash",
                command_id=2,
                claim_generation=10,
            )
            store, controller, consumer, action = self._prepare(fixture, identity)
            try:
                controller.begin_draining(identity)
                self.assertEqual(action.active_run_ids(identity), ())
                controller.record_worker_quiesced(identity, active_run_ids=())
                action.ensure_adopted(identity)
                controller.mark_adopted(identity, observed_adoption_sha=str(fixture["target"]))
                restart = action.ensure_worker_restarted(identity)
                controller.record_worker_restart(
                    identity,
                    launcher_identity=restart.launcher_identity,
                    worker_identity=restart.worker_identity,
                    worker_sha=restart.worker_sha,
                )
                controller.request_rollback(identity, reason="probation_failure")
                original_write = action._receipt.write

                def fail_rollback(receipt: object) -> None:
                    if getattr(receipt, "operation", None) == "rollback":
                        raise LauncherActionError("simulated_receipt_crash")
                    original_write(receipt)

                with patch.object(action._receipt, "write", side_effect=fail_rollback):
                    blocked = consumer.try_consume_after_worker_exit(worker_exit_code=75)
                self.assertEqual(blocked.status, HandoffConsumeStatus.BLOCKED)
                self.assertEqual(store.read().phase, HandoffPhase.ROLLBACK_REQUIRED)
                rollback_head = _git(Path(fixture["root"]), "rev-parse", "HEAD")
                self.assertNotEqual(rollback_head, str(fixture["target"]))
                action.close()

                resumed_action = _actions(fixture)
                resumed_action.bind_worker_exit(None, 75)
                resumed_consumer = LauncherHandoffConsumer(
                    controller,
                    actions=resumed_action.as_actions(),
                )
                resumed = _drive(resumed_consumer)
                self.assertEqual(resumed.status, HandoffConsumeStatus.ROLLED_BACK)
                self.assertEqual(store.read().rollback_commit_sha, rollback_head)
                self.assertEqual(
                    _git(Path(fixture["root"]), "ls-remote", "origin", "refs/heads/main").split()[0],
                    rollback_head,
                )
                resumed_process = resumed_action.take_replacement_process()
                _stop(resumed_process)
                resumed_action.close()
            finally:
                action.close()

    def test_synthetic_rollback_failure_and_dirty_repository_fail_closed(self) -> None:
        """The concrete rollback boundary leaves durable ROLLBACK_REQUIRED on failure."""

        with tempfile.TemporaryDirectory(prefix="bridge-phase85-synthetic-failclosed-") as temporary:
            fixture = _fixture(Path(temporary) / "checkout")
            identity = _identity(
                fixture,
                attempt_id="attempt-synthetic-failclosed",
                command_id=2,
                claim_generation=10,
            )
            store, controller, _, action = self._prepare(fixture, identity)
            try:
                controller.begin_draining(identity)
                self.assertTrue(action.worker_stopped(identity))
                self.assertEqual(action.active_run_ids(identity), ())
                controller.record_worker_quiesced(identity, active_run_ids=())
                self.assertEqual(action.ensure_adopted(identity), str(fixture["target"]))
                controller.mark_adopted(identity, observed_adoption_sha=str(fixture["target"]))
                restart = action.ensure_worker_restarted(identity)
                controller.record_worker_restart(
                    identity,
                    launcher_identity=restart.launcher_identity,
                    worker_identity=restart.worker_identity,
                    worker_sha=restart.worker_sha,
                )
                controller.request_rollback(identity, reason="probation_failure")
                action.close()

                failing = _actions(fixture, fault=FaultInjection.ROLLBACK_FAILURE)
                failing.bind_worker_exit(None, 75)
                failed = LauncherHandoffConsumer(controller, actions=failing.as_actions())
                result = failed.try_consume_after_worker_exit(worker_exit_code=75)
                self.assertEqual(result.status, HandoffConsumeStatus.BLOCKED)
                self.assertEqual(store.read().phase, HandoffPhase.ROLLBACK_REQUIRED)
                self.assertEqual(_git(Path(fixture["root"]), "rev-parse", "HEAD"), str(fixture["target"]))
                failing.close()

                # A dirty/ambiguous repository is rejected before the action
                # can mutate the stable branch.
                (Path(fixture["root"]) / "ambiguous-source.txt").write_text(
                    "untracked\n", encoding="utf-8"
                )
                dirty_store = HandoffEvidenceStore(
                    Path(fixture["runtime"]) / "dirty-handoff.json"
                )
                dirty_controller = OuterControllerHandoff(dirty_store)
                dirty_identity = _identity(
                    fixture,
                    attempt_id="attempt-synthetic-dirty",
                    command_id=4,
                    claim_generation=12,
                )
                dirty_controller.prepare(dirty_identity)
                dirty_action = _actions(fixture)
                dirty_action.bind_worker_exit(None, 75)
                dirty_consumer = LauncherHandoffConsumer(
                    dirty_controller,
                    actions=dirty_action.as_actions(),
                )
                dirty_result = dirty_consumer.try_consume_after_worker_exit(worker_exit_code=75)
                self.assertEqual(dirty_result.status, HandoffConsumeStatus.BLOCKED)
                self.assertEqual(_git(Path(fixture["root"]), "rev-parse", "HEAD"), str(fixture["target"]))
                dirty_action.close()
            finally:
                action.close()


class SplitLauncherActionConfigTests(unittest.TestCase):
    def test_split_state_runtime_is_accepted_and_pinned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-split-action-config-") as temp:
            base = Path(temp)
            engine = base / "engine"
            state = base / "state"
            worker = engine / "worker" / "bridge_worker_hardened.py"
            config = state / "worker" / "config.local.json"
            log = state / "worker" / "logs" / "launcher.log"
            worker.parent.mkdir(parents=True)
            config.parent.mkdir(parents=True)
            (state / "worker" / "runtime").mkdir(parents=True)
            worker.write_text("# fixture\n", encoding="utf-8")
            config.write_text("{}\n", encoding="utf-8")
            value = LauncherActionConfig(
                repository_root=engine,
                state_root=state,
                runtime_root=state / "worker" / "runtime",
                worker_script=worker,
                config_path=config,
                log_file=log,
                adoption_policy=AdoptionPolicy(
                    controlled_adoption_enabled=True,
                    unattended_adoption_enabled=False,
                ),
                require_remote=False,
            )
            self.assertEqual(value.repository_root, engine.resolve())
            self.assertEqual(value.state_root, state.resolve())
            self.assertEqual(value.runtime_root, (state / "worker" / "runtime").resolve())


if __name__ == "__main__":
    unittest.main()
