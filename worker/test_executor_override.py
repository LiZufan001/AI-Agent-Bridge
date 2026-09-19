import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bridge_worker as bw
from codex_lifecycle import CodexRunResult


DEFAULT_ARGS = [
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ephemeral",
    "-m",
    "gpt-5.6-luna",
    "-c",
    'model_reasoning_effort="high"',
]


def executor_meta(**executor):
    return {
        "command_id": 4,
        "source": "scheduled_chatgpt",
        "based_on_report": 3,
        "expected_generation": 7,
        "kind": "EXECUTE",
        "executor": executor,
    }


def fake_success_result():
    return CodexRunResult(
        exit_code=0,
        final_message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
        stdout_tail="",
        stderr_tail="",
        marker={"status": "SUCCESS"},
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at="2001-01-15T00:00:01+08:00",
        process_exited_at="2001-01-15T00:00:02+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result="VALID",
    )


class ExecutorOverrideTests(unittest.TestCase):
    def test_command_without_executor_preserves_worker_default(self):
        original = list(DEFAULT_ARGS)
        effective, profile = bw.effective_codex_args(DEFAULT_ARGS, {})

        self.assertEqual(effective, DEFAULT_ARGS)
        self.assertIsNot(effective, DEFAULT_ARGS)
        self.assertEqual(DEFAULT_ARGS, original)
        self.assertEqual(
            profile,
            {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
                "source": "worker_default",
            },
        )

    def test_sol_high_override_is_run_local_and_canonical(self):
        config = {
            "codex_execution_mode": "full_access",
            "codex_args": list(DEFAULT_ARGS),
        }
        original = json.loads(json.dumps(config))

        defaults = bw.codex_args_from_config(config)
        effective, profile = bw.effective_codex_args(
            defaults,
            executor_meta(model="gpt-5.6-sol", reasoning_effort="high"),
        )

        self.assertEqual(config, original)
        self.assertEqual(effective.count("-m"), 1)
        model_index = effective.index("-m")
        self.assertEqual(effective[model_index + 1], "gpt-5.6-sol")
        self.assertEqual(
            [arg for arg in effective if arg.startswith("model_reasoning_effort=")],
            ['model_reasoning_effort="high"'],
        )
        self.assertEqual(
            profile,
            {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "source": "command_override",
            },
        )

    def test_all_supported_model_and_effort_forms_are_deduplicated(self):
        defaults = [
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "-m",
            "old-one",
            "--model=old-two",
            "-c",
            'model="old-three"',
            "--config=model_reasoning_effort='low'",
            "-c=model_reasoning_effort=medium",
            "-c",
            "features.example=true",
        ]
        effective, _ = bw.effective_codex_args(
            defaults,
            executor_meta(model="gpt-5.6-sol", reasoning_effort="high"),
        )

        self.assertNotIn("old-one", effective)
        self.assertFalse(any(arg.startswith("--model=") for arg in effective))
        self.assertFalse(any("old-three" in arg for arg in effective))
        self.assertEqual(effective.count("-m"), 1)
        self.assertEqual(effective.count('model_reasoning_effort="high"'), 1)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", effective)
        self.assertIn("--ephemeral", effective)
        self.assertIn("features.example=true", effective)

    def test_malformed_executor_fails_closed(self):
        malformed = [
            "gpt-5.6-sol",
            {"model": "", "reasoning_effort": "high"},
            {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
            {"model": "gpt-5.6-sol"},
            {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "sandbox": "read-only",
            },
        ]
        for executor in malformed:
            with self.subTest(executor=executor), self.assertRaises(bw.WorkerError):
                bw.command_executor_override({"executor": executor})

    def test_model_rejects_control_whitespace_and_cli_injection_shapes(self):
        dangerous = [
            "--sandbox",
            "gpt-5.6-sol --sandbox read-only",
            "gpt-5.6-sol\n--sandbox",
            "gpt-5.6-sol\x00",
            'gpt-5.6-sol"',
        ]
        for model in dangerous:
            with self.subTest(model=repr(model)), self.assertRaises(bw.WorkerError):
                bw.command_executor_override(
                    executor_meta(model=model, reasoning_effort="high")
                )

    def test_override_cannot_replace_security_or_lifecycle_flags(self):
        effective, _ = bw.effective_codex_args(
            DEFAULT_ARGS,
            executor_meta(model="gpt-5.6-sol", reasoning_effort="high"),
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", effective)
        self.assertIn("--ephemeral", effective)
        self.assertNotIn("--sandbox", effective)
        self.assertNotIn("--approval-policy", effective)

    def test_cli_verified_reasoning_effort_values_are_accepted(self):
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                parsed = bw.command_executor_override(
                    executor_meta(model="future-model-1", reasoning_effort=effort)
                )
                self.assertEqual(parsed["reasoning_effort"], effort)

    def test_synthetic_command_without_executor_still_parses(self):
        command = ('<!-- bridge-command: {"command_id":3,"source":"scheduled_chatgpt",'
                   '"based_on_report":2,"expected_generation":7,"kind":"EXECUTE"} -->\n'
                   '# Command 003 - synthetic parser fixture\n')
        meta = bw.command_metadata(command)
        self.assertEqual(meta["command_id"], 3)
        self.assertNotIn("executor", meta)
        self.assertIsNone(bw.command_executor_override(meta))

    def test_process_records_active_run_and_report_executor_profile(self):
        with TemporaryDirectory() as tmp:
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
            state_path = project / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            metadata = json.dumps(
                executor_meta(model="gpt-5.6-sol", reasoning_effort="high"),
                separators=(",", ":"),
            )
            (project / "commands" / "command-004.md").write_text(
                f"<!-- bridge-command: {metadata} -->\n",
                encoding="utf-8",
            )

            remote_state = dict(state)
            claimed_states = []
            reports = []

            def fake_publish(**kwargs):
                nonlocal remote_state
                payloads = kwargs["payload_builder"](remote_state)
                if state_path in payloads:
                    remote_state = json.loads(payloads[state_path])
                    if remote_state.get("active_run"):
                        claimed_states.append(remote_state)
                for path, text in payloads.items():
                    if path.name.startswith("report-"):
                        reports.append(text)
                return remote_state

            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": list(DEFAULT_ARGS),
            }
            with patch.object(bw, "publish_cas", side_effect=fake_publish), patch.object(
                bw, "get_head", return_value="head"
            ), patch.object(
                bw, "codex_run", return_value=fake_success_result()
            ) as codex:
                processed = bw.process_project(
                    root,
                    "p",
                    {"workdir": "__BRIDGE_ROOT__"},
                    config,
                )

            self.assertTrue(processed)
            self.assertEqual(
                claimed_states[0]["active_run"]["executor"],
                {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "source": "command_override",
                },
            )
            effective = codex.call_args.kwargs["codex_args"]
            self.assertEqual(effective[effective.index("-m") + 1], "gpt-5.6-sol")
            self.assertIn('model_reasoning_effort="high"', effective)
            self.assertEqual(len(reports), 1)
            self.assertIn("- executor_profile_source: command_override", reports[0])
            self.assertIn("- requested_model: `gpt-5.6-sol`", reports[0])
            self.assertIn("- requested_reasoning_effort: `high`", reports[0])
            self.assertIn("- effective_cli_model: `gpt-5.6-sol`", reports[0])
            self.assertIn("- effective_cli_reasoning_effort: `high`", reports[0])




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
