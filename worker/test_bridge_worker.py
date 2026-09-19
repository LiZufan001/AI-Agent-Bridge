import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
from codex_lifecycle import CodexRunResult


def fake_run_result(*, status="SUCCESS", code=0):
    message = f'BRIDGE_EXECUTION_JSON: {{"status":"{status}"}}'
    return CodexRunResult(
        exit_code=code,
        final_message=message,
        stdout_tail="",
        stderr_tail="",
        marker={"status": status},
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at="2001-01-15T00:00:01+08:00",
        process_exited_at="2001-01-15T00:00:02+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result="VALID",
    )


def fake_capture_failure_result():
    return CodexRunResult(
        exit_code=125,
        final_message="",
        stdout_tail="",
        stderr_tail="",
        marker=None,
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at=None,
        process_exited_at="2001-01-15T00:00:02+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result="MARKER_CAPTURE_FAILED",
        runtime_error="final-message file could not be read",
        contract_error="MARKER_CAPTURE_FAILED: final-message file could not be read",
    )


class BridgeWorkerV2Tests(unittest.TestCase):
    class _ProbeResponse:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.body

    def _guard_config(self):
        return {
            "network_guard": {
                "enabled": True,
                "probe_url": "https://probe.invalid/trace",
                "timeout_seconds": 1,
                "watchdog_interval_seconds": 1,
                "blocked_country_codes": ["CN"],
                "fail_closed": True,
            }
        }

    def test_safe_probe_allows_codex_egress(self):
        with patch.object(
            bw,
            "urlopen",
            return_value=self._ProbeResponse(b"loc=US\n").__enter__(),
        ) as probe:
            result = bw.network_guard_check(self._guard_config())
        self.assertTrue(result.allowed)
        self.assertEqual(result.country, "US")
        probe.assert_called_once()

    def test_cn_probe_blocks_before_claim_and_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("mission", encoding="utf-8")
            state = {
                "protocol_version": 2,
                "status": "COMMAND_READY",
                "generation": 7,
                "latest_command": 4,
                "latest_report": 3,
                "active_run": None,
            }
            (project / "state.json").write_text(json.dumps(state), encoding="utf-8")
            (project / "commands" / "command-004.md").write_text(
                '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
                '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )
            config = self._guard_config()
            with patch.object(
                bw,
                "network_guard_check",
                return_value=bw.NetworkGuardResult(False, "country blocked: CN", "CN"),
            ), patch.object(bw, "publish_cas") as publish, patch.object(
                bw, "codex_run"
            ) as codex:
                processed = bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)
            self.assertFalse(processed)
            publish.assert_not_called()
            codex.assert_not_called()
            self.assertEqual(json.loads((project / "state.json").read_text())["status"], "COMMAND_READY")

    def test_safe_preflight_reaches_claim_and_codex_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("mission", encoding="utf-8")
            state = {
                "protocol_version": 2,
                "status": "COMMAND_READY",
                "generation": 7,
                "latest_command": 4,
                "latest_report": 3,
                "active_run": None,
            }
            (project / "state.json").write_text(json.dumps(state), encoding="utf-8")
            (project / "commands" / "command-004.md").write_text(
                '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
                '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )

            remote_state = dict(state)
            claimed_profiles = []

            def fake_publish(**kwargs):
                nonlocal remote_state
                payloads = kwargs["payload_builder"](remote_state)
                remote_state = json.loads(payloads[kwargs["state_path"]])
                if remote_state.get("active_run"):
                    claimed_profiles.append(remote_state["active_run"]["executor"])
                return remote_state

            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": [
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-m",
                    "gpt-5.6-luna",
                    "-c",
                    'model_reasoning_effort="high"',
                ],
                **self._guard_config(),
            }
            with patch.object(
                bw,
                "network_guard_check",
                return_value=bw.NetworkGuardResult(True, "country allowed: US", "US"),
            ), patch.object(bw, "publish_cas", side_effect=fake_publish) as publish, patch.object(
                bw, "get_head", return_value="head"
            ), patch.object(
                bw,
                "codex_run",
                return_value=fake_run_result(),
            ) as codex:
                processed = bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)
            self.assertTrue(processed)
            self.assertEqual(publish.call_count, 2)
            codex.assert_called_once()
            self.assertEqual(
                claimed_profiles,
                [
                    {
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "high",
                        "source": "worker_default",
                    }
                ],
            )
            health = json.loads(
                (root / "worker" / "runtime" / "worker-health.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(health["last_seen_project"], "p")
            self.assertEqual(health["last_seen_state"], "REPORT_READY")
            self.assertEqual(health["last_command_seen"], "p#004")
            self.assertEqual(health["last_claim_attempt_detail"], "p#004")
            self.assertIsNotNone(health["last_claim_at"])

    def test_marker_capture_failure_transitions_to_recovery_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("mission", encoding="utf-8")
            state = {
                "protocol_version": 2,
                "status": "COMMAND_READY",
                "generation": 7,
                "latest_command": 4,
                "latest_report": 3,
                "active_run": None,
            }
            (project / "state.json").write_text(json.dumps(state), encoding="utf-8")
            (project / "commands" / "command-004.md").write_text(
                '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
                '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )
            remote_state = dict(state)
            published_report = ""

            def fake_publish(**kwargs):
                nonlocal remote_state, published_report
                payloads = kwargs["payload_builder"](remote_state)
                remote_state = json.loads(payloads[kwargs["state_path"]])
                for path, value in payloads.items():
                    if path.name == "report-004.md":
                        published_report = value
                return remote_state

            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
                "network_guard": {"enabled": False},
            }
            with patch.object(
                bw, "publish_cas", side_effect=fake_publish
            ), patch.object(bw, "get_head", return_value="head"), patch.object(
                bw, "codex_run", return_value=fake_capture_failure_result()
            ):
                processed = bw.process_project(
                    root, "p", {"workdir": "__BRIDGE_ROOT__"}, config
                )
            self.assertTrue(processed)
            self.assertEqual(remote_state["status"], "RECOVERY_REQUIRED")
            self.assertEqual(remote_state["latest_report"], 4)
            self.assertIsNone(remote_state["active_run"])
            self.assertIn("MARKER_CAPTURE_FAILED", remote_state["last_execution_error"])
            self.assertIn("marker_result: MARKER_CAPTURE_FAILED", published_report)

    def test_probe_timeout_and_malformed_response_fail_closed(self):
        config = self._guard_config()
        with patch.object(bw, "urlopen", side_effect=TimeoutError):
            timeout_result = bw.network_guard_check(config)
        self.assertFalse(timeout_result.allowed)
        with patch.object(
            bw,
            "urlopen",
            return_value=self._ProbeResponse(b"not-a-country-response").__enter__(),
        ):
            malformed_result = bw.network_guard_check(config)
        self.assertFalse(malformed_result.allowed)

    def test_guard_disabled_is_backward_compatible(self):
        self.assertEqual(
            bw.network_guard_check({}), bw.NetworkGuardResult(True, "disabled")
        )
        self.assertEqual(
            bw.network_guard_check({"network_guard": {"enabled": False}}),
            bw.NetworkGuardResult(True, "disabled"),
        )

    def test_network_interruption_publishes_recovery_without_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "projects" / "p" / "state.json"
            state_path.parent.mkdir(parents=True)
            current = {
                "status": "CODEX_RUNNING",
                "generation": 8,
                "latest_command": 4,
                "latest_report": 3,
                "active_run": {"run_id": "run-004", "command_id": 4},
            }
            captured = {}

            def fake_publish(**kwargs):
                captured["state"] = json.loads(
                    kwargs["payload_builder"](current)[state_path]
                )
                return captured["state"]

            with patch.object(bw, "publish_cas", side_effect=fake_publish):
                bw.publish_network_recovery(
                    bridge_root=root,
                    state_path=state_path,
                    project_id="p",
                    command_id=4,
                    run_id="run-004",
                    claim_generation=8,
                    reason="network guard became unsafe: country blocked: CN",
                    report_text="NETWORK_INTERRUPTED evidence",
                    runtime_dir=root / "runtime",
                )
            self.assertEqual(captured["state"]["status"], "RECOVERY_REQUIRED")
            self.assertIsNone(captured["state"]["active_run"])
            self.assertEqual(captured["state"]["latest_report"], 3)
            self.assertIn("automatic rerun prohibited", captured["state"]["recovery_reason"])
            self.assertTrue((root / "runtime" / "pending-report-004.md").exists())

    def test_watchdog_stops_dummy_process_tree_on_unsafe_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "dummy_codex.py"
            script.write_text(
                "import subprocess, sys, time\nfrom pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "print(child.pid, flush=True)\n"
                "Path('child.ready').write_text(str(child.pid))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            config = self._guard_config()
            with patch.object(
                bw,
                "network_guard_check",
                side_effect=lambda *_args, **_kwargs: bw.NetworkGuardResult(False, "country blocked: CN", "CN")
                    if (Path(tmp) / "child.ready").exists() else bw.NetworkGuardResult(True, "synthetic startup", "US"),
            ):
                with self.assertRaises(bw.NetworkGuardInterruption) as caught:
                    bw.codex_run(
                        codex_argv=[sys.executable],
                        codex_args=[str(script)],
                        workdir=Path(tmp),
                        output_file=Path(tmp) / "last-message.txt",
                        prompt="test",
                        timeout_seconds=20,
                        network_config=config,
                    )

            # The captured PID comes from the actual guarded run, proving the
            # per-run scope cleaned the real descendant rather than a mock.
            child_pid = int(caught.exception.stdout.strip())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _pid_exists(child_pid):
                time.sleep(0.1)
            self.assertFalse(_pid_exists(child_pid))

    def test_windows_prefers_resolved_cmd_shim_when_ps1_coexists(self):
        cmd = r"X:\synthetic\c\profiles\someone\AppData\Roaming\npm\codex.CMD"
        with patch.object(bw.os, "name", "nt"), patch.object(
            bw.shutil, "which", return_value=cmd
        ):
            self.assertEqual(bw.command_argv("codex"), [cmd])

    def test_windows_ps1_resolution_is_explicit_only(self):
        ps1 = r"X:\synthetic\c\profiles\someone\AppData\Roaming\npm\codex.ps1"
        pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"

        def which(name):
            return {"codex": ps1, "pwsh": pwsh}.get(name)

        with patch.object(bw.os, "name", "nt"), patch.object(
            bw.shutil, "which", side_effect=which
        ):
            self.assertEqual(
                bw.command_argv("codex"),
                [pwsh, "-NoLogo", "-NoProfile", "-File", ps1],
            )

    def test_non_windows_command_resolution_is_unchanged(self):
        with patch.object(bw.os, "name", "posix"), patch.object(
            bw.shutil, "which", return_value="/usr/local/bin/codex"
        ):
            self.assertEqual(bw.command_argv("codex"), ["codex"])

    def test_codex_args_preserve_luna_high_full_access_flags(self):
        config = {
            "codex_execution_mode": "full_access",
            "codex_args": [
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--ephemeral",
                "-m",
                "gpt-5.6-luna",
                "-c",
                'model_reasoning_effort="high"',
            ],
        }
        self.assertEqual(bw.codex_args_from_config(config), config["codex_args"])

    def test_command_metadata(self):
        text = (
            '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
            '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n'
            "# Command 004\n"
        )
        meta = bw.command_metadata(text)
        self.assertEqual(meta["command_id"], 4)
        self.assertEqual(meta["source"], "scheduled_chatgpt")

    def test_validate_command_rejects_stale_generation(self):
        state = {"generation": 8, "latest_report": 3}
        meta = {
            "command_id": 4,
            "source": "scheduled_chatgpt",
            "based_on_report": 3,
            "expected_generation": 7,
            "kind": "EXECUTE",
        }
        with self.assertRaises(bw.CASConflict):
            bw.validate_command(state=state, command_id=4, meta=meta)

    def test_validate_command_rejects_stale_report(self):
        state = {"generation": 7, "latest_report": 4}
        meta = {
            "command_id": 5,
            "source": "manual_chatgpt",
            "based_on_report": 3,
            "expected_generation": 7,
            "kind": "EXECUTE",
        }
        with self.assertRaises(bw.CASConflict):
            bw.validate_command(state=state, command_id=5, meta=meta)

    def test_final_result_requires_strict_json_marker(self):
        misleading = (
            "I should have said final_delivery: SUCCESS and completion_email: SENT, "
            "but email failed."
        )
        self.assertFalse(
            bw.maybe_final_report_ready("FINALIZING", "SUCCESS", misleading)
        )
        good = (
            'Done.\nBRIDGE_FINAL_JSON: '
            '{"final_delivery":"SUCCESS","completion_email":"SENT"}'
        )
        self.assertTrue(
            bw.maybe_final_report_ready("FINALIZING", "SUCCESS", good)
        )

    def test_same_ready_snapshot_detects_generation_change(self):
        snapshot = {
            "status": "COMMAND_READY",
            "generation": 10,
            "latest_command": 8,
            "latest_report": 7,
            "active_run": None,
        }
        current = dict(snapshot)
        self.assertTrue(bw.same_ready_snapshot(current, snapshot))
        current["generation"] = 11
        self.assertFalse(bw.same_ready_snapshot(current, snapshot))

    def test_json_text_is_utf8_friendly(self):
        rendered = bw.json_text({"message": "目标已达成"})
        self.assertIn("目标已达成", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_report_diagnostics_redact_common_secret_shapes(self):
        diagnostic = (
            "Authorization: Bearer abcdefghijklmnop\n"
            "Cookie: session=private-cookie-value\n"
            "api_key=super-secret-value\n"
            "token " + "ghp_" + "abcdefghijklmnopqrstuvwxyz"
        )
        redacted = bw.redact_diagnostics(diagnostic)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertNotIn("private-cookie-value", redacted)
        self.assertNotIn("super-secret-value", redacted)
        self.assertNotIn("ghp_" + "abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertIn("[REDACTED", redacted)


def _pid_exists(pid):
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=False,
            check=False,
        )
        return str(pid).encode("ascii") in (result.stdout or b"")
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
