import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge_worker as bw
import bridge_worker_hardened as hardened
import recovery_journal as rj


def _path_key(path: Path) -> tuple[object, ...]:
    """Return a test-only key stable across equivalent filesystem spellings."""
    try:
        stat = os.stat(os.fspath(path))
    except (OSError, ValueError):
        return ("spelled", os.path.normcase(os.path.realpath(os.fspath(path))))
    return ("file", stat.st_dev, stat.st_ino)


def _running_state(
    *,
    project_id="p",
    run_id="run-004-test",
    generation=8,
    command_id=4,
    status="CODEX_RUNNING",
):
    return {
        "protocol_version": 2,
        "project_id": project_id,
        "status": status,
        "generation": generation,
        "latest_command": command_id,
        "latest_report": command_id - 1,
        "active_run": (
            {
                "run_id": run_id,
                "command_id": command_id,
                "claimed_generation": generation,
                "claimed_at": "2001-01-15T03:00:00+08:00",
                "lease_expires_at": "2001-01-15T08:00:00+08:00",
            }
            if status == "CODEX_RUNNING"
            else None
        ),
        "worker_pid": 4321 if status == "CODEX_RUNNING" else None,
    }


def _journal(root: Path, *, project_id="p", run_id="run-004-test", **updates):
    data = {
        "schema_version": 1,
        "project_id": project_id,
        "command_id": 4,
        "run_id": run_id,
        "claim_generation": 8,
        "interrupted_at": "2001-01-15T03:01:00+08:00",
        "interruption_kind": "network_guard",
        "interruption_reason_safe": "country blocked: CN",
        "claimed_at": "2001-01-15T03:00:00+08:00",
        "lease_expires_at": "2001-01-15T08:00:00+08:00",
        "head_before": "a" * 40,
        "head_after": "a" * 40,
        "worktree_dirty": True,
        "local_commit_created": False,
        "unpushed_commits_present": None,
        "report_path": "projects/p/reports/report-004.md",
        "pending_report_path": "worker/runtime/p/recovery/pending-report-004.md",
        "marker_status": "PROCESS_TERMINATED_WITHOUT_MARKER",
        "process_exit_code": 125,
        "termination_reason": "network_guard",
        "external_side_effects_unknown": True,
        "remote_publish_pending": True,
        "journal_status": "pending",
    }
    data.update(updates)
    path = rj.journal_path(root, project_id, run_id)
    rj.write_journal(path, data)
    return path


class RecoveryJournalTests(unittest.TestCase):
    def _write_state(self, root: Path, state: dict):
        project = root / "projects" / "p"
        (project / "reports").mkdir(parents=True, exist_ok=True)
        (project / "state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        return project / "state.json"

    def _fake_publish(self, state_path: Path):
        def publish(**kwargs):
            current = json.loads(state_path.read_text(encoding="utf-8"))
            if kwargs["already_applied"](current):
                return current
            self.assertTrue(kwargs["expected"](current))
            payloads = kwargs["payload_builder"](current)
            state_key = _path_key(state_path)
            matching_payloads = [
                payload
                for payload_path, payload in payloads.items()
                if _path_key(payload_path) == state_key
            ]
            self.assertEqual(
                len(matching_payloads),
                1,
                f"expected one payload for {state_path}, got {list(payloads)}",
            )
            updated = json.loads(matching_payloads[0])
            state_path.write_text(json.dumps(updated), encoding="utf-8")
            return updated

        return publish

    def test_fake_publish_accepts_alternate_absolute_state_path_spelling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = self._write_state(root, _running_state())
            alternate_dir = state_path.parent / "alternate-spelling"
            alternate_dir.mkdir()
            alternate_path = alternate_dir / ".." / state_path.name
            self.assertNotEqual(str(alternate_path), str(state_path))
            self.assertEqual(_path_key(alternate_path), _path_key(state_path))

            updated = dict(_running_state(), status="RECOVERY_REQUIRED")
            published = self._fake_publish(state_path)(
                already_applied=lambda _current: False,
                expected=lambda _current: True,
                payload_builder=lambda _current: {
                    alternate_path: json.dumps(updated),
                },
            )
            self.assertEqual(published["status"], "RECOVERY_REQUIRED")

    def test_journal_is_atomic_allowlisted_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _journal(
                root,
                interruption_reason_safe=(
                    "Authorization: Bearer fake-token "
                    "Cookie: session=private-cookie "
                    "api_key=another-secret " + "ghp_" + "abcdefghijklmnopqrstuvwxyz"
                ),
                command_prompt="do not persist this prompt",
                raw_stderr="Cookie: session=private",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertNotIn("command_prompt", data)
            self.assertNotIn("raw_stderr", data)
            self.assertNotIn("fake-token", path.read_text(encoding="utf-8"))
            self.assertNotIn("private-cookie", path.read_text(encoding="utf-8"))
            self.assertNotIn("another-secret", path.read_text(encoding="utf-8"))
            self.assertNotIn("ghp_" + "abcdefghijklmnopqrstuvwxyz", path.read_text(encoding="utf-8"))
            self.assertTrue(
                all(name not in data for name in ("Cookie", "Authorization"))
            )
            self.assertFalse(any(path.parent.glob("*.tmp")))

    def test_report_final_response_is_redacted_before_local_or_remote_publish(self):
        rendered = bw.report_markdown(
            project_id="p",
            command_id=4,
            outcome="FAILED",
            exit_code=125,
            workdir=Path("X:/synthetic/d/fixture"),
            head_before=None,
            head_after=None,
            final_message=(
                "Authorization: Bearer fake-token; "
                "Cookie: session=private-cookie"
            ),
            stderr="",
            meta={"source": "fixture", "based_on_report": 3},
            run_id="run-004-test",
            claim_generation=8,
            executor_profile={"source": "fixture"},
        )
        self.assertNotIn("fake-token", rendered)
        self.assertNotIn("private-cookie", rendered)

    def test_network_interruption_writes_journal_then_report_then_remote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = self._write_state(root, _running_state())
            events = []

            def write_journal(path, data):
                events.append("journal")
                return data

            def save_report(*_args, **_kwargs):
                events.append("pending_report")

            def publish(**_kwargs):
                events.append("remote_publish")
                return {"status": "RECOVERY_REQUIRED"}

            with patch.object(rj, "write_journal", side_effect=write_journal), patch.object(
                bw, "save_pending_report", side_effect=save_report
            ), patch.object(rj, "update_journal"), patch.object(
                bw, "publish_cas", side_effect=publish
            ), patch.object(
                bw,
                "collect_recovery_evidence",
                return_value={
                    "head_before": "a" * 40,
                    "head_after": "a" * 40,
                    "worktree_dirty": False,
                    "local_commit_created": False,
                    "unpushed_commits_present": None,
                    "external_side_effects_unknown": True,
                },
            ):
                bw.publish_network_recovery(
                    bridge_root=root,
                    state_path=state_path,
                    project_id="p",
                    command_id=4,
                    run_id="run-004-test",
                    claim_generation=8,
                    reason="Authorization: Bearer fake-token; Cookie: session=private-cookie",
                    report_text="recovery report",
                    runtime_dir=root / "worker" / "runtime" / "p",
                )
            self.assertEqual(events, ["journal", "pending_report", "remote_publish"])

    def test_successful_remote_publish_marks_journal_reconciled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = self._write_state(root, _running_state())
            with patch.object(
                bw, "publish_cas", side_effect=self._fake_publish(state_path)
            ):
                bw.publish_network_recovery(
                    bridge_root=root,
                    state_path=state_path,
                    project_id="p",
                    command_id=4,
                    run_id="run-004-test",
                    claim_generation=8,
                    reason="Authorization: Bearer fake-token; Cookie: session=private-cookie",
                    report_text="recovery report",
                    runtime_dir=root / "worker" / "runtime" / "p",
                    workdir=root,
                )
            journal = rj.read_journal(rj.journal_path(root, "p", "run-004-test"))
            self.assertFalse(journal["remote_publish_pending"])
            self.assertEqual(journal["journal_status"], "reconciled")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("fake-token", state["recovery_reason"])
            self.assertNotIn("private-cookie", state["recovery_reason"])

    def test_publish_failure_keeps_pending_journal_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = self._write_state(root, _running_state())
            with patch.object(
                bw,
                "publish_cas",
                side_effect=bw.WorkerError("GitHub unavailable"),
            ):
                with self.assertRaises(bw.WorkerError):
                    bw.publish_network_recovery(
                        bridge_root=root,
                        state_path=state_path,
                        project_id="p",
                        command_id=4,
                        run_id="run-004-test",
                        claim_generation=8,
                        reason="probe failed",
                        report_text="recovery report",
                        runtime_dir=root / "worker" / "runtime" / "p",
                    )
            journal_path = rj.journal_path(root, "p", "run-004-test")
            journal = rj.read_journal(journal_path)
            self.assertTrue(journal["remote_publish_pending"])
            self.assertEqual(journal["journal_status"], "pending")
            self.assertTrue(
                (root / "worker" / "runtime" / "p" / "pending-report-004.md").exists()
            )

    def test_restart_reconciles_pending_journal_without_rerunning_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = self._write_state(root, _running_state())
            journal_path = _journal(root)
            with patch.object(
                bw, "publish_cas", side_effect=self._fake_publish(state_path)
            ), patch.object(bw, "codex_run") as codex:
                self.assertEqual(bw.reconcile_pending_recoveries(root), 1)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            journal = rj.read_journal(journal_path)
            self.assertEqual(state["status"], "RECOVERY_REQUIRED")
            self.assertIsNone(state["active_run"])
            self.assertIsNone(state["worker_pid"])
            self.assertEqual(state["generation"], 9)
            self.assertFalse(journal["remote_publish_pending"])
            self.assertEqual(journal["journal_status"], "reconciled")
            codex.assert_not_called()

    def test_deferred_publish_failure_keeps_journal_pending_and_worker_path_alive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = self._write_state(root, _running_state())
            journal_path = _journal(root)
            with patch.object(
                bw,
                "publish_cas",
                side_effect=bw.WorkerError("GitHub push unavailable"),
            ):
                self.assertEqual(bw.reconcile_pending_recoveries(root), 0)
            journal = rj.read_journal(journal_path)
            self.assertTrue(journal["remote_publish_pending"])
            self.assertEqual(journal["journal_status"], "pending")
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["status"],
                "CODEX_RUNNING",
            )

    def test_complete_fault_injection_round_trip_survives_remote_outage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("mission", encoding="utf-8")
            state = {
                **_running_state(status="COMMAND_READY"),
                "active_run": None,
                "worker_pid": None,
                "generation": 7,
                "latest_command": 4,
                "latest_report": 3,
            }
            state_path = self._write_state(root, state)
            (project / "commands" / "command-004.md").write_text(
                '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
                '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )
            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
                "network_guard": {"enabled": False},
            }
            publish_calls = 0

            def claim_then_fail(**kwargs):
                nonlocal publish_calls
                publish_calls += 1
                current = json.loads(state_path.read_text(encoding="utf-8"))
                if publish_calls == 1:
                    updated = json.loads(kwargs["payload_builder"](current)[state_path])
                    state_path.write_text(json.dumps(updated), encoding="utf-8")
                    return updated
                raise bw.WorkerError("GitHub unavailable during recovery publish")

            with patch.object(
                bw, "publish_cas", side_effect=claim_then_fail
            ), patch.object(
                bw,
                "codex_run",
                side_effect=bw.NetworkGuardInterruption(
                    "network guard became unsafe: country blocked: CN"
                ),
            ) as codex, patch.object(bw, "get_head", return_value="a" * 40):
                with self.assertRaises(bw.WorkerError):
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)
            codex.assert_called_once()
            journal_path = next(
                rj.pending_journal_paths(root, project_id="p")
            )
            pending = rj.read_journal(journal_path)
            self.assertTrue(pending["remote_publish_pending"])
            self.assertEqual(json.loads(state_path.read_text())["status"], "CODEX_RUNNING")

            with patch.object(bw, "publish_cas", side_effect=self._fake_publish(state_path)):
                self.assertEqual(bw.reconcile_pending_recoveries(root), 1)
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(final_state["status"], "RECOVERY_REQUIRED")
            self.assertIsNone(final_state["active_run"])
            self.assertIsNone(final_state["worker_pid"])
            self.assertEqual(rj.read_journal(journal_path)["journal_status"], "reconciled")

    def test_reconciliation_is_idempotent_after_remote_publish_before_local_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = _running_state()
            state["status"] = "RECOVERY_REQUIRED"
            state["active_run"] = None
            state["worker_pid"] = None
            state["generation"] = 9
            state["recovery_reason"] = (
                "network guard interrupted Codex run run-004-test; "
                "automatic rerun prohibited"
            )
            state_path = self._write_state(root, state)
            journal_path = _journal(root)
            with patch.object(bw, "publish_cas") as publish:
                self.assertEqual(bw.reconcile_pending_recoveries(root), 1)
            publish.assert_not_called()
            journal = rj.read_journal(journal_path)
            self.assertEqual(journal["journal_status"], "reconciled")

    def test_generation_or_run_conflict_never_overwrites_remote_state(self):
        cases = (
            {"generation": 9},
            {"active_run": {"run_id": "run-other", "command_id": 4, "claimed_generation": 8}},
        )
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                state = _running_state()
                state.update(change)
                state_path = self._write_state(root, state)
                journal_path = _journal(root)
                with patch.object(bw, "publish_cas") as publish:
                    self.assertEqual(bw.reconcile_pending_recoveries(root), 1)
                publish.assert_not_called()
                self.assertEqual(
                    json.loads(state_path.read_text(encoding="utf-8"))["status"],
                    "CODEX_RUNNING",
                )
                self.assertEqual(rj.read_journal(journal_path)["journal_status"], "conflict")

    def test_report_or_done_state_is_superseded_without_overwrite(self):
        cases = ("REPORT_READY", "FINAL_REPORT_READY", "DONE")
        for status in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                state = _running_state(status=status)
                state["latest_report"] = 3
                state_path = self._write_state(root, state)
                journal_path = _journal(root)
                if status == "REPORT_READY":
                    (root / "projects" / "p" / "reports" / "report-004.md").write_text(
                        "already published", encoding="utf-8"
                    )
                with patch.object(bw, "publish_cas") as publish:
                    self.assertEqual(bw.reconcile_pending_recoveries(root), 1)
                publish.assert_not_called()
                self.assertEqual(rj.read_journal(journal_path)["journal_status"], "superseded")

    def test_lease_expiry_clears_stale_execution_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = _running_state()
            state["active_run"]["lease_expires_at"] = "not-an-iso-date"
            state_path = self._write_state(root, state)
            with patch.object(bw, "publish_cas", side_effect=self._fake_publish(state_path)):
                self.assertTrue(bw.maybe_mark_expired_lease(root, "p", state_path))
            updated = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["status"], "RECOVERY_REQUIRED")
            self.assertIsNone(updated["active_run"])
            self.assertIsNone(updated["worker_pid"])
            self.assertEqual(updated["generation"], 9)

    def test_unexpired_lease_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = _running_state()
            # Keep this characterization independent of the wall-clock date.
            state["active_run"]["lease_expires_at"] = "2099-01-01T00:00:00+00:00"
            state_path = self._write_state(root, state)
            with patch.object(bw, "publish_cas") as publish:
                self.assertFalse(bw.maybe_mark_expired_lease(root, "p", state_path))
            publish.assert_not_called()

    def test_hardened_poll_reconciles_before_normal_processing(self):
        events = []
        lock = nullcontext()
        config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "projects": {"p": {"enabled": True, "workdir": "__BRIDGE_ROOT__"}},
        }

        def sync(_root):
            events.append("sync")

        def reconcile(_root, **_kwargs):
            events.append("reconcile")
            return 0

        def process(_root, project_id, _project_cfg, _config, **_kwargs):
            events.append(f"process:{project_id}")
            return False

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
                hardened.bw, "WorkerInstanceLock", return_value=lock
            ), patch.object(
                hardened.bw, "sync_to_remote", side_effect=sync
            ), patch.object(
                hardened.bw, "reconcile_pending_recoveries", side_effect=reconcile
            ), patch.object(
                hardened.rpr, "worker_code_changed", return_value=False
            ), patch.object(
                hardened, "_observe_project"
            ), patch.object(
                hardened, "_remote_project_is_safe_to_process", return_value=True
            ), patch.object(
                hardened.bw, "process_project", side_effect=process
            ), patch.object(
                hardened, "_record_worker_health"
            ), patch.object(
                hardened, "_record_worker_exit"
            ):
                self.assertEqual(
                    hardened.main(runtime_root=Path(temp) / "isolated-runtime"),
                    0,
                )
        self.assertEqual(events, ["sync", "reconcile", "process:p"])




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
