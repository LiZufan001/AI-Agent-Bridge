import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
import bridge_common
from codex_lifecycle import CodexRunResult
import pending_report
import recovery_journal


def run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def running_state(
    *,
    project_id: str = "p",
    command_id: int = 8,
    generation: int = 23,
    run_id: str = "run-008-test",
    source: str = "scheduled_chatgpt",
    status: str = "CODEX_RUNNING",
    latest_report: int | None = None,
) -> dict:
    return {
        "protocol_version": 2,
        "project_id": project_id,
        "status": status,
        "generation": generation,
        "latest_command": command_id,
        "latest_report": command_id - 1 if latest_report is None else latest_report,
        "last_reviewed_report": command_id - 1,
        "active_run": (
            {
                "run_id": run_id,
                "command_id": command_id,
                "source": source,
                "based_on_report": command_id - 1,
                "base_generation": generation - 1,
                "claimed_generation": generation,
                "claimed_at": "2001-01-15T07:00:00+08:00",
                "lease_expires_at": "2099-01-01T00:00:00+00:00",
                "worker_pid": 1234,
            }
            if status == "CODEX_RUNNING"
            else None
        ),
        "worker_pid": 1234 if status == "CODEX_RUNNING" else None,
    }


class PendingReportIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.worker = self.root / "worker"
        run_git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        run_git("init", "--initial-branch=main", str(self.seed), cwd=self.root)
        run_git("config", "user.name", "pending-report-test", cwd=self.seed)
        run_git("config", "user.email", "pending-report-test@localhost", cwd=self.seed)
        run_git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.write_state(self.seed, running_state())
        mission_path = self.seed / "projects" / "p" / "MISSION.md"
        mission_path.write_text("mission\n", encoding="utf-8")
        (self.seed / "tracked.txt").write_text("clean\n", encoding="utf-8")
        run_git("add", "--all", cwd=self.seed)
        run_git("commit", "-m", "initial canonical fixture", cwd=self.seed)
        run_git("push", "-u", "origin", "main", cwd=self.seed)
        run_git("clone", str(self.remote), str(self.worker), cwd=self.root)
        run_git("config", "user.name", "pending-report-test", cwd=self.worker)
        run_git("config", "user.email", "pending-report-test@localhost", cwd=self.worker)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def write_state(root: Path, state: dict) -> None:
        state_path = root / "projects" / "p" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        (state_path.parent / "reports").mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def save_pending(
        self,
        root: Path,
        *,
        report_text: str = "# Report 008\n\ncompleted\n",
        run_id: str = "run-008-test",
        claim_generation: int = 23,
        source: str = "scheduled_chatgpt",
        kind: str = "EXECUTE",
        previous_status: str = "COMMAND_READY",
        outcome: str = "SUCCESS",
        target_status: str = "REPORT_READY",
        last_execution_error: str | None = None,
    ) -> Path:
        return pending_report.save_worker_pending_report(
            bridge_root=root,
            project_id="p",
            command_id=8,
            run_id=run_id,
            claim_generation=claim_generation,
            source=source,
            kind=kind,
            previous_status=previous_status,
            outcome=outcome,
            target_status=target_status,
            report_text=report_text,
            reason="publication temporarily unavailable",
            last_execution_error=last_execution_error,
        )

    def test_dirty_clone_keeps_pending_then_clean_clone_reconciles(self):
        report_text = "# Report 008\n\ncompleted once\n"
        self.save_pending(self.worker, report_text=report_text)
        (self.worker / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        with patch.object(bw, "codex_run", side_effect=AssertionError("Codex rerun")):
            self.assertEqual(pending_report.reconcile_pending_reports(self.worker), 0)
        metadata_path = self.worker / "worker" / "runtime" / "p" / "pending-report-008.meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["journal_status"], "pending")
        self.assertTrue(metadata["remote_publish_pending"])
        self.assertFalse((self.worker / "projects" / "p" / "reports" / "report-008.md").exists())

        run_git("restore", "--", "tracked.txt", cwd=self.worker)
        with patch.object(bw, "codex_run", side_effect=AssertionError("Codex rerun")):
            self.assertEqual(pending_report.reconcile_pending_reports(self.worker), 1)
        state = json.loads((self.worker / "projects" / "p" / "state.json").read_text())
        self.assertEqual(state["generation"], 24)
        self.assertEqual(state["latest_report"], 8)
        self.assertEqual(state["status"], "REPORT_READY")
        self.assertIsNone(state["active_run"])
        self.assertIsNone(state["worker_pid"])
        self.assertNotIn("last_execution_error", state)
        self.assertEqual(
            (self.worker / "projects" / "p" / "reports" / "report-008.md").read_text(
                encoding="utf-8"
            ),
            report_text,
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["journal_status"], "reconciled")
        self.assertFalse(metadata["remote_publish_pending"])
        self.assertEqual(
            metadata["report_sha256"], hashlib.sha256(report_text.encode()).hexdigest()
        )
        self.assertIn("bridge: reconcile pending p report 008", run_git("log", "-1", "--format=%s", cwd=self.worker))

    def test_failed_result_restores_error_and_finalize_target(self):
        report_text = "# Failed report\n"
        self.save_pending(
            self.worker,
            report_text=report_text,
            kind="FINALIZE",
            previous_status="FINALIZING",
            outcome="FAILED",
            target_status="REPORT_READY",
            last_execution_error="Codex marker FAILED; process exit 1",
        )
        self.assertEqual(pending_report.reconcile_pending_reports(self.worker), 1)
        final_state = json.loads(
            (self.worker / "projects" / "p" / "state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(final_state["status"], "REPORT_READY")
        self.assertEqual(
            final_state["last_execution_error"], "Codex marker FAILED; process exit 1"
        )

        # A successful FINALIZE with its already-decided target is also
        # publication-only and must not be downgraded to REPORT_READY.
        self.root2 = self.root / "second"
        self.root2.mkdir()
        state_path = self.root2 / "projects" / "p" / "state.json"
        state_path.parent.mkdir(parents=True)
        (state_path.parent / "reports").mkdir()
        self.write_state(self.root2, running_state())
        self.save_pending(
            self.root2,
            kind="FINALIZE",
            previous_status="FINALIZING",
            target_status="FINAL_REPORT_READY",
        )
        def fake_publish(**kwargs):
            current = bridge_common.load_json(state_path)
            self.assertTrue(kwargs["expected"](current))
            payloads = kwargs["payload_builder"](current)
            for path, text in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return bridge_common.load_json(state_path)

        with patch.object(pending_report.git_store, "publish_cas", side_effect=fake_publish):
            self.assertEqual(pending_report.reconcile_pending_reports(self.root2), 1)
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["status"], "FINAL_REPORT_READY")

    def test_already_applied_is_idempotent_and_does_not_publish_again(self):
        report_text = "# Report 008\n\nalready canonical\n"
        state = running_state(status="REPORT_READY", generation=24, latest_report=8)
        self.write_state(self.worker, state)
        report_path = self.worker / "projects" / "p" / "reports" / "report-008.md"
        report_path.write_text(report_text, encoding="utf-8")
        self.save_pending(self.worker, report_text=report_text)
        with patch.object(pending_report.git_store, "publish_cas") as publish:
            self.assertEqual(pending_report.reconcile_pending_reports(self.worker), 1)
        publish.assert_not_called()
        metadata_path = self.worker / "worker" / "runtime" / "p" / "pending-report-008.meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["journal_status"], "superseded")

    def test_identity_generation_hash_missing_and_path_conflicts_refuse_publish(self):
        cases = (
            {"active_run": {"run_id": "run-other", "command_id": 8, "claimed_generation": 23}},
            {"generation": 24},
        )
        for change in cases:
            with self.subTest(change=change):
                root = self.root / ("case-" + str(len(list(self.root.glob("case-*")))))
                root.mkdir()
                self.write_state(root, running_state())
                state = json.loads((root / "projects" / "p" / "state.json").read_text())
                state.update(change)
                (root / "projects" / "p" / "state.json").write_text(json.dumps(state), encoding="utf-8")
                self.save_pending(root)
                with patch.object(pending_report.git_store, "publish_cas") as publish:
                    self.assertEqual(pending_report.reconcile_pending_reports(root), 0)
                publish.assert_not_called()

        root = self.root / "hash-mismatch"
        root.mkdir()
        self.write_state(root, running_state())
        metadata_path = self.save_pending(root)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["report_sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with patch.object(pending_report.git_store, "publish_cas") as publish:
            self.assertEqual(pending_report.reconcile_pending_reports(root), 0)
        publish.assert_not_called()

        root = self.root / "missing-report"
        root.mkdir()
        self.write_state(root, running_state())
        metadata_path = self.save_pending(root)
        pending_path = root / "worker" / "runtime" / "p" / "pending-report-008.md"
        pending_path.unlink()
        with patch.object(pending_report.git_store, "publish_cas") as publish:
            self.assertEqual(pending_report.reconcile_pending_reports(root), 0)
        publish.assert_not_called()

        root = self.root / "invalid-path"
        root.mkdir()
        self.write_state(root, running_state())
        metadata_path = self.save_pending(root)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["report_path"] = "projects/p/reports/escape.md"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with patch.object(pending_report.git_store, "publish_cas") as publish:
            self.assertEqual(pending_report.reconcile_pending_reports(root), 0)
        publish.assert_not_called()

    def test_network_recovery_pending_blocks_completed_report_reconciliation(self):
        self.save_pending(self.worker)
        recovery_path = recovery_journal.journal_path(self.worker, "p", "run-004-test")
        recovery_journal.write_journal(
            recovery_path,
            {
                "schema_version": 1,
                "project_id": "p",
                "command_id": 4,
                "run_id": "run-004-test",
                "claim_generation": 8,
                "interrupted_at": "2001-01-15T07:00:00+08:00",
                "interruption_kind": "network_guard",
                "interruption_reason_safe": "network unavailable",
                "remote_publish_pending": True,
                "journal_status": "pending",
            },
        )
        with patch.object(pending_report.git_store, "publish_cas") as publish:
            self.assertEqual(bw.reconcile_pending_reports(self.worker), 0)
        publish.assert_not_called()
        metadata_path = self.worker / "worker" / "runtime" / "p" / "pending-report-008.meta.json"
        self.assertEqual(
            json.loads(metadata_path.read_text(encoding="utf-8"))["journal_status"],
            "pending",
        )

    def test_poll_publication_has_zero_codex_calls(self):
        self.save_pending(self.worker)
        with patch.object(bw, "codex_run", side_effect=AssertionError("Codex rerun")) as codex:
            self.assertEqual(bw.reconcile_pending_reports(self.worker), 1)
            self.assertFalse(
                bw.process_project(
                    self.worker,
                    "p",
                    {"workdir": "__BRIDGE_ROOT__"},
                    {"network_guard": {"enabled": False}},
                )
            )
        codex.assert_not_called()

    def test_publication_failure_is_bounded_and_stays_pending(self):
        self.save_pending(self.worker)
        with patch.object(
            pending_report.git_store,
            "publish_cas",
            side_effect=__import__("bridge_common").WorkerError("temporary push failure"),
        ) as publish:
            self.assertEqual(pending_report.reconcile_pending_reports(self.worker), 0)
        publish.assert_called_once()
        metadata_path = self.worker / "worker" / "runtime" / "p" / "pending-report-008.meta.json"
        self.assertEqual(
            json.loads(metadata_path.read_text(encoding="utf-8"))["journal_status"],
            "pending",
        )

    def test_worker_failure_path_writes_machine_metadata_without_second_codex_call(self):
        state = running_state(status="COMMAND_READY")
        self.write_state(self.worker, state)
        command_path = self.worker / "projects" / "p" / "commands" / "command-008.md"
        command_path.parent.mkdir(parents=True, exist_ok=True)
        command_path.write_text(
            '<!-- bridge-command: {"command_id":8,"source":"scheduled_chatgpt",'
            '"based_on_report":7,"expected_generation":23,"kind":"EXECUTE"} -->\n'
            "# Command 008\n",
            encoding="utf-8",
        )
        run_git("add", "--all", cwd=self.worker)
        run_git("commit", "-m", "add pending report test command", cwd=self.worker)
        run_git("push", "origin", "HEAD:main", cwd=self.worker)

        fake_result = CodexRunResult(
            exit_code=0,
            final_message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
            stdout_tail="",
            stderr_tail="",
            marker={"status": "SUCCESS"},
            launched_at="2001-01-15T07:00:00+08:00",
            final_marker_detected_at="2001-01-15T07:00:01+08:00",
            process_exited_at="2001-01-15T07:00:02+08:00",
            wrapper_pid=123,
            stdout_log_path=Path("stdout.log"),
            stderr_log_path=Path("stderr.log"),
            process_scope="pending-report-test",
            marker_result="VALID",
        )
        publish_calls = 0

        def fake_publish(**kwargs):
            nonlocal publish_calls
            publish_calls += 1
            current = bridge_common.load_json(self.worker / "projects" / "p" / "state.json")
            if publish_calls == 1:
                self.assertTrue(kwargs["expected"](current))
                payloads = kwargs["payload_builder"](current)
                updated = json.loads(payloads[self.worker / "projects" / "p" / "state.json"])
                (self.worker / "projects" / "p" / "state.json").write_text(
                    payloads[self.worker / "projects" / "p" / "state.json"],
                    encoding="utf-8",
                )
                return updated
            raise bridge_common.WorkerError("temporary report publication failure")

        config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "network_guard": {"enabled": False},
        }
        with patch.object(bw, "publish_cas", side_effect=fake_publish), patch.object(
            bw, "codex_run", return_value=fake_result
        ) as codex:
            with self.assertRaises(bridge_common.WorkerError):
                bw.process_project(
                    self.worker,
                    "p",
                    {"workdir": "__BRIDGE_ROOT__"},
                    config,
                )

        codex.assert_called_once()
        self.assertEqual(publish_calls, 2)
        metadata_path = self.worker / "worker" / "runtime" / "p" / "pending-report-008.meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["command_id"], 8)
        self.assertEqual(metadata["claim_generation"], 24)
        self.assertEqual(metadata["outcome"], "SUCCESS")
        self.assertEqual(metadata["target_status"], "REPORT_READY")
        self.assertTrue(metadata["remote_publish_pending"])
        self.assertEqual(metadata["journal_status"], "pending")




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
