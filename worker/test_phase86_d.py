"""Deterministic Phase-8.6-D Git and publication-recovery tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Callable

import bridge_common
import pending_report
from vnext_runtime.git_effects import (
    GitPushIntent,
    GitPushReconciler,
    WalPushEffectRecorder,
)
from vnext_runtime.models import RunIdentity
from vnext_runtime.recovery_evidence import (
    PushReconciliation,
    PushResolutionState,
    WalEventKind,
)
from vnext_runtime.recovery_wal import DurableRunWal
from vnext_runtime.services.recovery import RecoveryCoordinator


class GitEffectReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = RunIdentity("p", 4, "run-004-phase86d", 8)
        self.intent = GitPushIntent(
            identity=self.identity,
            effect_id="push-1",
            remote="origin",
            target_ref="refs/heads/main",
            expected_object="a" * 40,
            precondition_object="b" * 40,
        )

    def test_exact_expected_object_is_applied_without_a_write_path(self) -> None:
        writes: list[str] = []
        result = GitPushReconciler(
            lambda _remote, _ref: (writes.append("read") or "a" * 40)
        ).reconcile(self.intent)
        self.assertEqual(result.state, PushResolutionState.EXACT_APPLIED)
        self.assertEqual(result.observed_object, "a" * 40)
        self.assertEqual(writes, ["read"])

    def test_exact_precondition_is_not_applied_without_repeating_the_push(self) -> None:
        result = GitPushReconciler(
            lambda _remote, _ref: "b" * 40
        ).reconcile(self.intent)
        self.assertEqual(result.state, PushResolutionState.EXACT_NOT_APPLIED)
        self.assertEqual(result.observed_object, "b" * 40)

    def test_unexpected_remote_identity_is_ambiguous(self) -> None:
        result = GitPushReconciler(
            lambda _remote, _ref: "c" * 40
        ).reconcile(self.intent)
        self.assertEqual(result.state, PushResolutionState.AMBIGUOUS)

    def test_missing_remote_ref_is_ambiguous_unless_zero_precondition_is_recorded(self) -> None:
        ambiguous = GitPushReconciler(
            lambda _remote, _ref: None
        ).reconcile(self.intent)
        self.assertEqual(ambiguous.state, PushResolutionState.AMBIGUOUS)
        absent_intent = GitPushIntent(
            identity=self.identity,
            effect_id="push-absent",
            remote="origin",
            target_ref="refs/heads/main",
            expected_object="a" * 40,
            precondition_object="0" * 40,
        )
        not_applied = GitPushReconciler(
            lambda _remote, _ref: None
        ).reconcile(absent_intent)
        self.assertEqual(not_applied.state, PushResolutionState.EXACT_NOT_APPLIED)
        self.assertEqual(not_applied.observed_object, "0" * 40)

    def test_wal_recorder_binds_intent_precondition_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wal = DurableRunWal(
                Path(directory) / "recovery.wal",
                self.identity,
            )
            wal.append(WalEventKind.RUN_CLAIMED)
            recorder = WalPushEffectRecorder(wal)
            intent = recorder.record_push_intent(
                remote="origin",
                target_ref="refs/heads/main",
                expected_object="a" * 40,
                precondition_object="b" * 40,
            )
            observed = GitPushReconciler(
                lambda _remote, _ref: "a" * 40
            ).reconcile(intent)
            recorder.record_push_confirmed(observed)
            effect = wal.snapshot.wal.push_effects()[0]
            self.assertTrue(effect.confirmed)
            self.assertEqual(effect.precondition_object, "b" * 40)


class TypedPublicationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = RunIdentity("p", 4, "run-004-phase86d", 8)

    def _state(self, status: str = "CODEX_RUNNING") -> dict[str, object]:
        return {
            "protocol_version": 2,
            "project_id": "p",
            "status": status,
            "generation": 8,
            "latest_command": 4,
            "latest_report": 3,
            "active_run": (
                {
                    "run_id": self.identity.run_id,
                    "command_id": 4,
                    "claimed_generation": 8,
                    "source": "manual_chatgpt",
                }
                if status == "CODEX_RUNNING"
                else None
            ),
            "worker_pid": 42 if status == "CODEX_RUNNING" else None,
        }

    def _prepare(
        self,
        root: Path,
        *,
        text: str,
        kind: str = "EXECUTE",
        outcome: str = "SUCCESS",
        target_status: str = "REPORT_READY",
        run_id: str | None = None,
        identity: RunIdentity | None = None,
        make_wal: Callable[[DurableRunWal, str], None] | None = None,
    ) -> Path:
        actual_identity = identity or self.identity
        actual_run_id = run_id or actual_identity.run_id
        state_path = root / "projects" / "p" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(self._state()) + "\n", encoding="utf-8")
        metadata_path = pending_report.save_worker_pending_report(
            bridge_root=root,
            project_id="p",
            command_id=4,
            run_id=actual_run_id,
            claim_generation=8,
            source="manual_chatgpt",
            kind=kind,
            previous_status="CODEX_RUNNING",
            outcome=outcome,
            target_status=target_status,
            report_text=text,
            reason="publication interrupted",
            last_execution_error=None if outcome == "SUCCESS" else "interrupted",
            evidence_mode="typed",
        )
        if make_wal is not None:
            wal_path = pending_report.canonical_wal_path(root, "p", actual_run_id)
            wal = DurableRunWal(wal_path, actual_identity)
            make_wal(wal, hashlib.sha256(text.encode("utf-8")).hexdigest())
        return metadata_path

    @staticmethod
    def _completed_wal(wal: DurableRunWal, digest: str) -> None:
        wal.append(WalEventKind.RUN_CLAIMED)
        wal.append(WalEventKind.PROCESS_CREATE_INTENT)
        wal.append(WalEventKind.PROCESS_CREATED)
        wal.append(WalEventKind.PROCESS_EXITED)
        wal.append(WalEventKind.REPORT_MATERIALIZED, artifact_digest=digest)
        wal.append(WalEventKind.REPORT_PUBLISH_INTENT, artifact_digest=digest)

    @staticmethod
    def _safe_interrupted_wal(wal: DurableRunWal, digest: str) -> None:
        wal.append(WalEventKind.RUN_CLAIMED)
        wal.append(WalEventKind.BROKER_STARTED)
        wal.append(WalEventKind.CONTAINMENT_PREPARED)
        wal.append(WalEventKind.PROCESS_CREATE_INTENT)
        wal.append(WalEventKind.PROCESS_CREATED)
        wal.append(WalEventKind.PROCESS_EXITED)
        wal.append(WalEventKind.BROKER_STOPPED)
        wal.record_lifecycle(
            "containment_reconciled",
            {
                "artifact_digest": "a" * 64,
                "project_egress_observed": "NO",
                "project_egress_artifact_digest": "b" * 64,
            },
        )
        wal.record_lifecycle(
            "containment_released",
            {"artifact_digest": "a" * 64},
        )
        wal.append(WalEventKind.REPORT_MATERIALIZED, artifact_digest=digest)
        wal.append(WalEventKind.REPORT_PUBLISH_INTENT, artifact_digest=digest)

    @staticmethod
    def _publisher(root: Path, state_path: Path, calls: list[dict[str, object]]) -> Callable[..., dict[str, object]]:
        def publish(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            current = bridge_common.load_json(state_path)
            expected = kwargs["expected"]
            payload_builder = kwargs["payload_builder"]
            assert callable(expected)
            assert callable(payload_builder)
            if not expected(current):
                raise bridge_common.CASConflict("state changed")
            payloads = payload_builder(current)
            assert isinstance(payloads, dict)
            for path, text in payloads.items():
                assert isinstance(path, Path)
                assert isinstance(text, str)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return bridge_common.load_json(state_path)

        return publish

    def test_completed_report_recovery_publishes_once_without_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: VALID\n- outcome: SUCCESS\n"
            self._prepare(root, text=text, make_wal=self._completed_wal)
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "REPORT_READY")

    def test_temporary_publication_failure_retries_exact_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: VALID\n- outcome: SUCCESS\n"
            self._prepare(root, text=text, make_wal=self._completed_wal)
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            normal = self._publisher(root, state_path, calls)
            attempts = 0

            def flaky(**kwargs: object) -> dict[str, object]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise bridge_common.WorkerError("temporary Durable Store failure")
                return normal(**kwargs)

            coordinator = RecoveryCoordinator(root, publish_cas=flaky)
            self.assertEqual(coordinator.reconcile_pending_reports(), 0)
            self.assertEqual(coordinator.reconcile_pending_reports(), 1)
            self.assertEqual(attempts, 2)
            self.assertEqual(bridge_common.load_json(state_path)["latest_report"], 4)

    def test_ambiguous_git_effect_enters_recovery_without_a_second_push(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: MISSING\n- outcome: FAILED\n"

            def with_push(wal: DurableRunWal, digest: str) -> None:
                wal.append(WalEventKind.RUN_CLAIMED)
                wal.append(
                    WalEventKind.PUSH_INTENT,
                    effect_id="push-1",
                    target_id="origin:refs/heads/main",
                    expected_object="a" * 40,
                    precondition_object="b" * 40,
                )
                wal.append(WalEventKind.REPORT_MATERIALIZED, artifact_digest=digest)
                wal.append(WalEventKind.REPORT_PUBLISH_INTENT, artifact_digest=digest)

            self._prepare(root, text=text, outcome="FAILED", make_wal=with_push)
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            push_writes: list[str] = []

            def read_ambiguous(_intent: GitPushIntent) -> PushReconciliation:
                return PushReconciliation(
                    effect_id="push-1",
                    target_id="origin:refs/heads/main",
                    expected_object="a" * 40,
                    precondition_object="b" * 40,
                    state=PushResolutionState.AMBIGUOUS,
                )

            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
                reconcile_push=read_ambiguous,
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 0)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "RECOVERY_REQUIRED")
            self.assertEqual(push_writes, [])
            self.assertEqual(len(calls), 1)

    def test_positive_containment_and_no_egress_allow_interrupted_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: MISSING\n- outcome: FAILED\n"

            def with_safe_wal(wal: DurableRunWal, digest: str) -> None:
                self._safe_interrupted_wal(wal, digest)

            self._prepare(
                root,
                text=text,
                outcome="FAILED",
                make_wal=with_safe_wal,
            )
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 1)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "REPORT_READY")

    def test_finalize_without_valid_marker_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: MISSING\n- outcome: FAILED\n"
            self._prepare(
                root,
                text=text,
                kind="FINALIZE",
                outcome="FAILED",
                make_wal=self._completed_wal,
            )
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 0)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "RECOVERY_REQUIRED")
            self.assertEqual(len(calls), 1)

    def test_already_canonical_recovery_required_is_never_auto_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: VALID\n- outcome: SUCCESS\n"
            self._prepare(root, text=text, make_wal=self._completed_wal)
            state_path = root / "projects" / "p" / "state.json"
            state = self._state("RECOVERY_REQUIRED")
            state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
            calls: list[dict[str, object]] = []
            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 0)
            self.assertEqual(len(calls), 0)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "RECOVERY_REQUIRED")

    def test_missing_wal_cannot_publish_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: VALID\n- outcome: SUCCESS\n"
            self._prepare(root, text=text, make_wal=None)
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 0)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "RECOVERY_REQUIRED")
            self.assertEqual(len(calls), 1)

    def test_run_a_cannot_consume_run_b_wal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "- marker_result: VALID\n- outcome: SUCCESS\n"
            self._prepare(root, text=text, make_wal=None)
            foreign = RunIdentity("p", 4, "run-004-foreign", 8)
            foreign_wal = DurableRunWal(
                pending_report.canonical_wal_path(root, "p", self.identity.run_id),
                foreign,
            )
            foreign_wal.append(WalEventKind.RUN_CLAIMED)
            state_path = root / "projects" / "p" / "state.json"
            calls: list[dict[str, object]] = []
            coordinator = RecoveryCoordinator(
                root,
                publish_cas=self._publisher(root, state_path, calls),
            )
            self.assertEqual(coordinator.reconcile_pending_reports(), 0)
            self.assertEqual(bridge_common.load_json(state_path)["status"], "RECOVERY_REQUIRED")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
