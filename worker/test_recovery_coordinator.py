import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import bridge_common
import pending_report
import recovery_journal
from vnext_runtime.services import recovery as rc


def running_state(*, project_id="p", run_id="run-004-test", generation=8):
    return {
        "protocol_version": 2,
        "project_id": project_id,
        "status": "CODEX_RUNNING",
        "generation": generation,
        "latest_command": 4,
        "latest_report": 3,
        "active_run": {
            "run_id": run_id,
            "command_id": 4,
            "claimed_generation": generation,
            "source": "manual_chatgpt",
        },
        "worker_pid": 42,
    }


def journal_data(*, project_id="p", run_id="run-004-test", status="pending", remote=True):
    return {
        "schema_version": 1,
        "project_id": project_id,
        "command_id": 4,
        "run_id": run_id,
        "claim_generation": 8,
        "interrupted_at": "2001-01-15T00:00:00+08:00",
        "interruption_kind": "network_guard",
        "interruption_reason_safe": "probe failed",
        "remote_publish_pending": remote,
        "journal_status": status,
        "external_side_effects_unknown": True,
    }


class RecoveryCoordinatorTests(unittest.TestCase):
    def test_typed_decision_rejects_mismatched_identity_without_payload(self):
        coordinator = rc.RecoveryCoordinator(Path("."))
        identity = rc.RecoveryIdentity("p", 4, "run-004-test", 8)
        with self.assertRaises(rc.RecoveryServiceConflict):
            coordinator.prepare_recovery_decision(
                running_state(run_id="run-other"), identity, reason="interrupted"
            )

        decision = coordinator.prepare_recovery_decision(
            running_state(), identity, reason="interrupted"
        )
        self.assertEqual(decision.next_generation, 9)
        self.assertEqual(decision.identity, identity)
        payload = coordinator.recovery_payload(running_state(), decision)
        self.assertEqual(payload["status"], "RECOVERY_REQUIRED")
        self.assertIsNone(payload["active_run"])
        self.assertEqual(payload["project_id"], "p")

    def test_service_has_no_executor_or_operator_resolution_boundary(self):
        coordinator = rc.RecoveryCoordinator(Path("."))
        self.assertFalse(hasattr(coordinator, "state"))
        self.assertFalse(hasattr(coordinator, "lease"))
        self.assertFalse(hasattr(rc, "executor"))
        self.assertFalse(hasattr(rc, "codex_lifecycle"))
        self.assertFalse(hasattr(rc, "recovery_resolution"))

    def test_network_recovery_records_evidence_before_one_exact_cas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_path = root / "projects" / "p" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(bridge_common.json_text(running_state()), encoding="utf-8")
            publisher = Mock()

            def publish(**kwargs):
                current = running_state()
                self.assertTrue(kwargs["expected"](current))
                payload = kwargs["payload_builder"](current)[state_path]
                updated = json.loads(payload)
                state_path.write_text(payload, encoding="utf-8")
                return updated

            publisher.side_effect = publish
            coordinator = rc.RecoveryCoordinator(root, publish_cas=publisher)
            coordinator.publish_network_recovery(
                state_path=state_path,
                project_id="p",
                command_id=4,
                run_id="run-004-test",
                claim_generation=8,
                reason="probe failed",
                report_text="interrupted evidence",
                runtime_dir=root / "worker" / "runtime" / "p",
                workdir=root,
            )
            journal = recovery_journal.read_journal(
                recovery_journal.journal_path(root, "p", "run-004-test")
            )
            self.assertEqual(journal["journal_status"], "reconciled")
            self.assertEqual(json.loads(state_path.read_text())["status"], "RECOVERY_REQUIRED")
            publisher.assert_called_once()

    def test_deferred_recovery_cas_conflict_fails_closed_and_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_path = root / "projects" / "p" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(bridge_common.json_text(running_state()), encoding="utf-8")
            journal_path = recovery_journal.journal_path(root, "p", "run-004-test")
            recovery_journal.write_journal(journal_path, journal_data())
            publisher = Mock(side_effect=bridge_common.CASConflict("race"))
            coordinator = rc.RecoveryCoordinator(root, publish_cas=publisher)

            self.assertEqual(coordinator.reconcile_pending_recoveries(), 1)
            journal = recovery_journal.read_journal(journal_path)
            self.assertEqual(journal["journal_status"], "conflict")
            self.assertFalse(json.loads(state_path.read_text())["status"] == "RECOVERY_REQUIRED")

    def test_malformed_or_ambiguous_evidence_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bad = root / "worker" / "runtime" / "p" / "recovery" / "run-004-bad.json"
            bad.parent.mkdir(parents=True)
            bad.write_text("not-json", encoding="utf-8")
            publisher = Mock()
            coordinator = rc.RecoveryCoordinator(root, publish_cas=publisher)
            self.assertEqual(coordinator.reconcile_pending_recoveries(), 0)
            publisher.assert_not_called()

    def test_pending_report_reconciliation_is_publication_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_path = root / "projects" / "p" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(bridge_common.json_text(running_state()), encoding="utf-8")
            pending_report.save_worker_pending_report(
                bridge_root=root,
                project_id="p",
                command_id=4,
                run_id="run-004-test",
                claim_generation=8,
                source="manual_chatgpt",
                kind="EXECUTE",
                previous_status="CODEX_RUNNING",
                outcome="FAILED",
                target_status="REPORT_READY",
                report_text="# evidence\n",
                reason="failed",
                last_execution_error="failed",
            )
            calls = 0

            def publish(**kwargs):
                nonlocal calls
                calls += 1
                current = json.loads(state_path.read_text())
                payloads = kwargs["payload_builder"](current)
                for path, text in payloads.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                return json.loads(state_path.read_text())

            provider = Mock(side_effect=AssertionError("provider execution"))
            coordinator = rc.RecoveryCoordinator(root, publish_cas=publish)
            self.assertEqual(coordinator.reconcile_pending_reports(), 1)
            self.assertEqual(calls, 1)
            provider.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text())["status"], "REPORT_READY")


if __name__ == "__main__":
    unittest.main()
