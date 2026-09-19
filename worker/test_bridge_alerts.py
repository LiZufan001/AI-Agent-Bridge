import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge_alerts
import bridge_worker as bw
import bridge_worker_hardened as hardened
import recovery_journal as rj
from codex_lifecycle import CodexRunResult


ALERT_CONFIG = {"alerts": {"enabled": True, "smtp_timeout_seconds": 1}}
EMAIL_CONFIG = {
    "user": "sender@example.test",
    "password": "not-a-real-secret",
    "recipient": "recipient@example.test",
    "host": "smtp.example.test",
    "port": "2525",
    "from_name": "Bridge test",
}


def _context(**updates):
    value = {
        "timestamp": "2001-01-15T04:00:00+08:00",
        "project_id": "p",
        "command_id": 4,
        "run_id": "run-004-test",
        "alert_kind": "network_guard_interruption",
        "current_bridge_status": "RECOVERY_REQUIRED",
        "original_status": "CODEX_RUNNING",
        "generation": 9,
        "generation_before": 8,
        "claim_generation": 8,
        "interruption_recovery_type": "network guard interruption",
        "recovery_reason_safe": "country blocked: CN",
        "codex_terminated": True,
        "worker_is_alive": True,
        "journal_saved": True,
        "pending_report_saved": True,
        "remote_recovery_cas_success": True,
        "remote_publish_pending": False,
        "worktree_dirty": False,
        "local_commit_created": False,
        "unpushed_commits_present": None,
        "external_side_effects_unknown": True,
        "lease_expires_at": "2001-01-15T08:00:00+08:00",
    }
    value.update(updates)
    return value


def _running_state(*, status="CODEX_RUNNING", generation=8, run_id="run-004-test"):
    if status == "COMMAND_READY" and generation == 8:
        generation = 7
    return {
        "protocol_version": 2,
        "project_id": "p",
        "status": status,
        "generation": generation,
        "latest_command": 4,
        "latest_report": 3,
        "active_run": (
            {
                "run_id": run_id,
                "command_id": 4,
                "claimed_generation": 8,
                "claimed_at": "2001-01-15T03:00:00+08:00",
                "lease_expires_at": "2001-01-15T08:00:00+08:00",
            }
            if status == "CODEX_RUNNING"
            else None
        ),
        "worker_pid": 4321 if status == "CODEX_RUNNING" else None,
    }


def _write_project(root: Path, state: dict, *, command_id=4) -> Path:
    project = root / "projects" / "p"
    (project / "commands").mkdir(parents=True, exist_ok=True)
    (project / "reports").mkdir(parents=True, exist_ok=True)
    (project / "MISSION.md").write_text("mission", encoding="utf-8")
    (project / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (project / "commands" / f"command-{command_id:03d}.md").write_text(
        f'<!-- bridge-command: {{"command_id":{command_id},'
        '"source":"scheduled_chatgpt","based_on_report":3,'
        '"expected_generation":7,"kind":"EXECUTE"} -->\n',
        encoding="utf-8",
    )
    return project / "state.json"


def _config():
    return {
        "codex_command": "python",
        "codex_execution_mode": "full_access",
        "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
        "network_guard": {"enabled": False},
        **ALERT_CONFIG,
    }


def _fake_publisher(state_path: Path, *, fail_on_call=None):
    calls = 0

    def publish(**kwargs):
        nonlocal calls
        calls += 1
        if fail_on_call == calls:
            raise bw.WorkerError("simulated remote outage")
        current = json.loads(state_path.read_text(encoding="utf-8"))
        if kwargs["already_applied"](current):
            return current
        if not kwargs["expected"](current):
            raise bw.CASConflict("fixture CAS mismatch")
        payloads = kwargs["payload_builder"](current)
        for path, content in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return json.loads(state_path.read_text(encoding="utf-8"))

    return publish


def _run_result(*, marker=None, marker_result="VALID", contract_error=None):
    return CodexRunResult(
        exit_code=0,
        final_message="completed",
        stdout_tail="",
        stderr_tail="",
        marker=marker,
        launched_at="2001-01-15T04:00:00+08:00",
        final_marker_detected_at="2001-01-15T04:00:01+08:00",
        process_exited_at="2001-01-15T04:00:01+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="fixture",
        marker_result=marker_result,
        contract_error=contract_error,
    )


class AlertModuleTests(unittest.TestCase):
    def test_provider_neutral_smtp_config_requires_explicit_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "smtp.env"
            env_file.write_text(
                "BRIDGE_SMTP_HOST=smtp.example.test\n"
                "BRIDGE_SMTP_PORT=2525\n"
                "BRIDGE_SMTP_USER=sender@example.test\n"
                "BRIDGE_SMTP_PASSWORD=not-a-real-secret\n"
                "BRIDGE_SMTP_TO=recipient@example.test\n"
                "BRIDGE_SMTP_FROM_NAME=Synthetic Bridge\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config, resolved = bridge_alerts.resolve_email_config({"env_path": str(env_file)})
        self.assertEqual(resolved, env_file)
        self.assertEqual(config["host"], "smtp.example.test")
        self.assertEqual(config["port"], "2525")
        self.assertEqual(config["password"], "not-a-real-secret")
        self.assertNotIn("auth_code", config)

    def test_smtp_config_without_host_or_credentials_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(bridge_alerts.AlertConfigurationError):
                bridge_alerts.resolve_email_config({})

    def _email_patches(self, *, send_side_effect=None):
        sender = patch.object(
            bridge_alerts,
            "send_message",
            side_effect=send_side_effect,
        )
        resolver = patch.object(
            bridge_alerts,
            "resolve_email_config",
            return_value=(EMAIL_CONFIG, Path("private-config.env")),
        )
        return resolver, sender

    def test_alert_is_atomic_and_persistent_dedupe_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resolver, sender = self._email_patches()
            with resolver, sender as send:
                first = bridge_alerts.emit_alert(root, ALERT_CONFIG, _context())
                second = bridge_alerts.emit_alert(root, ALERT_CONFIG, _context())

            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "deduped")
            self.assertEqual(send.call_count, 1)
            record_path = Path(first["record_path"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "sent")
            self.assertEqual(record["attempt_count"], 1)
            self.assertFalse(list(record_path.parent.glob("*.tmp")))
            content = record_path.read_text(encoding="utf-8")
            self.assertNotIn("test-only-not-a-real-secret", content)
            self.assertEqual(record["project_id"], "p")
            self.assertEqual(record["command_id"], 4)
            self.assertEqual(record["run_id"], "run-004-test")
            self.assertEqual(record["alert_kind"], "network_guard_interruption")

    def test_network_pending_body_contains_safe_diagnostics_and_recovery_instructions(self):
        body = bridge_alerts.build_alert_body(
            _context(
                remote_recovery_cas_success=False,
                remote_publish_pending=True,
                recovery_reason_safe=(
                    "Authorization: Bearer fake-token; "
                    "Cookie: session=private-cookie"
                ),
            )
        )
        self.assertIn("remote recovery CAS 是否成功: false", body)
        self.assertIn("remote_publish_pending: true", body)
        self.assertIn("远端仍可能暂时显示 CODEX_RUNNING", body)
        self.assertIn("本地 recovery journal 已保存", body)
        self.assertIn("原 Command 不会自动重跑", body)
        self.assertNotIn("fake-token", body)
        self.assertNotIn("private-cookie", body)

    def test_smtp_failure_is_recorded_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resolver, sender = self._email_patches(
                send_side_effect=RuntimeError("fixture transport failure")
            )
            with resolver, sender as send:
                first = bridge_alerts.emit_alert(root, ALERT_CONFIG, _context())
                second = bridge_alerts.emit_alert(root, ALERT_CONFIG, _context())

            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "deduped")
            self.assertEqual(first["error_kind"], "RuntimeError")
            self.assertEqual(send.call_count, 1)
            record = json.loads(Path(first["record_path"]).read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "failed")
            content = Path(first["record_path"]).read_text(encoding="utf-8")
            self.assertNotIn("fixture transport failure", content)

    def test_disabled_alerting_is_a_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            result = bridge_alerts.emit_alert(Path(temp), {}, _context())
            self.assertEqual(result["status"], "disabled")
            self.assertFalse((Path(temp) / "worker" / "runtime").exists())


class WorkerAlertIntegrationTests(unittest.TestCase):
    def _email_patches(self, *, send_side_effect=None):
        return (
            patch.object(
                bridge_alerts,
                "resolve_email_config",
                return_value=(EMAIL_CONFIG, Path("private-config.env")),
            ),
            patch.object(bridge_alerts, "send_message", side_effect=send_side_effect),
        )

    def test_network_interruption_cas_success_sends_safe_alert_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = _write_project(root, _running_state(status="COMMAND_READY"))
            config = _config()
            resolver, sender = self._email_patches()
            with resolver, sender as send, patch.object(
                bw, "publish_cas", side_effect=_fake_publisher(state_path)
            ), patch.object(
                bw,
                "codex_run",
                side_effect=bw.NetworkGuardInterruption("country blocked: CN"),
            ) as codex, patch.object(bw, "get_head", return_value="head"), patch.object(
                bw,
                "collect_recovery_evidence",
                return_value={
                    "head_before": "head",
                    "head_after": "head",
                    "worktree_dirty": False,
                    "local_commit_created": False,
                    "unpushed_commits_present": None,
                    "external_side_effects_unknown": True,
                },
            ):
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)
                )
                self.assertFalse(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)
                )

            self.assertEqual(codex.call_count, 1)
            self.assertEqual(send.call_count, 1)
            message = send.call_args.args[1]
            body = message.get_content()
            self.assertIn("当前 Bridge status: RECOVERY_REQUIRED", body)
            self.assertIn("journal 是否已保存: true", body)
            self.assertIn("pending report 是否已保存: true", body)
            self.assertIn("remote recovery CAS 是否成功: true", body)
            self.assertIn("remote_publish_pending: false", body)
            self.assertNotIn("SMTP", body)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "RECOVERY_REQUIRED")
            self.assertIsNone(state["active_run"])
            self.assertTrue(list((root / "worker" / "runtime" / "alerts").glob("*.json")))

    def test_network_pending_alert_does_not_change_state_or_rerun(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = _write_project(root, _running_state(status="COMMAND_READY"))
            config = _config()
            resolver, sender = self._email_patches()
            with resolver, sender as send, patch.object(
                bw, "publish_cas", side_effect=_fake_publisher(state_path, fail_on_call=2)
            ), patch.object(
                bw,
                "codex_run",
                side_effect=bw.NetworkGuardInterruption("probe failed"),
            ) as codex, patch.object(bw, "get_head", return_value="head"), patch.object(
                bw,
                "collect_recovery_evidence",
                return_value={
                    "head_before": "head",
                    "head_after": "head",
                    "worktree_dirty": False,
                    "local_commit_created": False,
                    "unpushed_commits_present": None,
                    "external_side_effects_unknown": True,
                },
            ):
                with self.assertRaises(bw.WorkerError):
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)

            self.assertEqual(codex.call_count, 1)
            self.assertEqual(send.call_count, 1)
            body = send.call_args.args[1].get_content()
            self.assertIn("remote recovery CAS 是否成功: false", body)
            self.assertIn("remote_publish_pending: true", body)
            self.assertIn("原 Command 不会自动重跑", body)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "CODEX_RUNNING")
            journal = next(rj.pending_journal_paths(root, project_id="p"))
            self.assertTrue(rj.read_journal(journal)["remote_publish_pending"])

    def test_smtp_failure_does_not_change_successful_recovery_or_rerun(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = _write_project(root, _running_state(status="COMMAND_READY"))
            resolver, sender = self._email_patches(
                send_side_effect=RuntimeError("fixture transport failure")
            )
            with resolver, sender as send, patch.object(
                bw, "publish_cas", side_effect=_fake_publisher(state_path)
            ), patch.object(
                bw,
                "codex_run",
                side_effect=bw.NetworkGuardInterruption("probe failed"),
            ) as codex, patch.object(bw, "get_head", return_value="head"), patch.object(
                bw,
                "collect_recovery_evidence",
                return_value={
                    "head_before": "head",
                    "head_after": "head",
                    "worktree_dirty": False,
                    "local_commit_created": False,
                    "unpushed_commits_present": None,
                    "external_side_effects_unknown": True,
                },
            ):
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, _config())
                )

            self.assertEqual(send.call_count, 1)
            self.assertEqual(codex.call_count, 1)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["status"],
                "RECOVERY_REQUIRED",
            )
            record = next((root / "worker" / "runtime" / "alerts").glob("*.json"))
            self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "failed")


    def test_expired_lease_sends_one_orphan_fuse_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = _running_state()
            state["active_run"]["lease_expires_at"] = "not-an-iso-date"
            state_path = _write_project(root, state)
            resolver, sender = self._email_patches()
            with resolver, sender as send, patch.object(
                bw, "publish_cas", side_effect=_fake_publisher(state_path)
            ):
                self.assertTrue(bw.maybe_mark_expired_lease(root, "p", state_path, ALERT_CONFIG))
                self.assertFalse(bw.maybe_mark_expired_lease(root, "p", state_path, ALERT_CONFIG))

            self.assertEqual(send.call_count, 1)
            body = send.call_args.args[1].get_content()
            self.assertIn("execution lease 已过期", send.call_args.args[1]["Subject"])
            self.assertIn("orphaned-run safety fuse", body)
            self.assertIn("原 Command 禁止自动 rerun", body)
            updated = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["status"], "RECOVERY_REQUIRED")
            self.assertEqual(updated["generation"], 9)
            self.assertIsNone(updated["active_run"])

    def test_deferred_recovery_conflict_sends_once_and_does_not_overwrite_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = _running_state(generation=9)
            state_path = _write_project(root, state)
            journal = {
                "schema_version": 1,
                "project_id": "p",
                "command_id": 4,
                "run_id": "run-004-test",
                "claim_generation": 8,
                "interrupted_at": "2001-01-15T03:01:00+08:00",
                "interruption_kind": "network_guard",
                "interruption_reason_safe": "probe failed",
                "claimed_at": "2001-01-15T03:00:00+08:00",
                "lease_expires_at": "2001-01-15T08:00:00+08:00",
                "worktree_dirty": False,
                "local_commit_created": False,
                "unpushed_commits_present": None,
                "pending_report_path": "worker/runtime/p/pending-report-004.md",
                "external_side_effects_unknown": True,
                "remote_publish_pending": True,
                "journal_status": "pending",
            }
            journal_path = rj.journal_path(root, "p", "run-004-test")
            rj.write_journal(journal_path, journal)
            pending = root / "worker" / "runtime" / "p" / "pending-report-004.md"
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text("safe evidence", encoding="utf-8")

            resolver, sender = self._email_patches()
            with resolver, sender as send, patch.object(bw, "publish_cas") as publish:
                self.assertEqual(
                    bw.reconcile_pending_recoveries(root, config=ALERT_CONFIG),
                    1,
                )
                self.assertEqual(
                    bw.reconcile_pending_recoveries(root, config=ALERT_CONFIG),
                    0,
                )

            self.assertEqual(send.call_count, 1)
            publish.assert_not_called()
            body = send.call_args.args[1].get_content()
            self.assertIn("deferred_recovery_conflict", body)
            self.assertIn("remote recovery CAS 是否成功: false", body)
            self.assertEqual(rj.read_journal(journal_path)["journal_status"], "conflict")
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["status"],
                "CODEX_RUNNING",
            )

    def test_unreadable_deferred_journal_sends_safe_error_alert_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "worker" / "runtime" / "p" / "recovery" / "run-004-bad.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not-json", encoding="utf-8")
            resolver, sender = self._email_patches()
            with resolver, sender as send:
                self.assertEqual(
                    bw.reconcile_pending_recoveries(root, config=ALERT_CONFIG),
                    0,
                )
                self.assertEqual(
                    bw.reconcile_pending_recoveries(root, config=ALERT_CONFIG),
                    0,
                )
            self.assertEqual(send.call_count, 1)
            body = send.call_args.args[1].get_content()
            self.assertIn("deferred_recovery_error", body)
            self.assertIn("local recovery journal cannot be safely read", body)

    def test_normal_success_does_not_send_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = _write_project(root, _running_state(status="COMMAND_READY"))
            resolver, sender = self._email_patches()
            with resolver, sender as send, patch.object(
                bw, "publish_cas", side_effect=_fake_publisher(state_path)
            ), patch.object(
                bw,
                "codex_run",
                return_value=_run_result(marker={"status": "SUCCESS"}),
            ), patch.object(bw, "get_head", return_value="head"):
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, _config())
                )
            self.assertEqual(send.call_count, 0)
            self.assertFalse((root / "worker" / "runtime" / "alerts").exists())

    def test_fetch_failure_and_exit75_hot_reload_do_not_send_alert(self):
        config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "projects": {"p": {"enabled": True, "workdir": "__BRIDGE_ROOT__"}},
        }
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text("{}\n", encoding="utf-8")
            from test_state_fixture import make_state
            args = SimpleNamespace(config=str(config_path), once=True, project=None, state_root=make_state(Path(temp)))
            with patch.object(hardened.bw, "parse_args", return_value=args), patch.object(
                hardened.bw, "ensure_tool"
            ), patch.object(
                hardened, "_resolve_config_path", return_value=config_path
            ), patch.object(
                hardened.rpr, "load_runtime_config", return_value=config
            ), patch.object(
                hardened, "_validate_codex_config"
            ), patch.object(
                hardened.rpr, "git_head", return_value="head"
            ), patch.object(
                hardened.bw, "WorkerInstanceLock", return_value=nullcontext()
            ), patch.object(
                hardened.bw,
                "reconcile_pending_recoveries",
                return_value=0,
            ), patch.object(
                hardened, "_record_worker_health"
            ), patch.object(
                hardened, "_record_worker_exit"
            ), patch.object(
                hardened.bw, "emit_bridge_alert"
            ) as alert:
                with patch.object(
                    hardened.bw,
                    "sync_to_remote",
                    side_effect=bw.WorkerError("fetch failed"),
                ):
                    self.assertEqual(
                        hardened.main(runtime_root=Path(temp) / "isolated-runtime"),
                        1,
                    )
                alert.assert_not_called()

            with patch.object(hardened.bw, "parse_args", return_value=args), patch.object(
                hardened.bw, "ensure_tool"
            ), patch.object(
                hardened, "_resolve_config_path", return_value=config_path
            ), patch.object(
                hardened.rpr, "load_runtime_config", return_value=config
            ), patch.object(
                hardened, "_validate_codex_config"
            ), patch.object(
                hardened.rpr, "git_head", return_value="head"
            ), patch.object(
                hardened.bw, "WorkerInstanceLock", return_value=nullcontext()
            ), patch.object(
                hardened.bw, "sync_to_remote"
            ), patch.object(
                hardened.bw, "reconcile_pending_recoveries", return_value=0
            ), patch.object(
                hardened.rpr, "worker_code_changed", return_value=True
            ), patch.object(
                hardened, "_record_worker_health"
            ), patch.object(
                hardened, "_record_worker_exit"
            ), patch.object(
                hardened.bw, "emit_bridge_alert"
            ) as alert:
                self.assertEqual(
                    hardened.main(runtime_root=Path(temp) / "isolated-runtime"),
                    75,
                )
                alert.assert_not_called()




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
