import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
import bridge_worker_hardened as hardened
import executor
import phased_task
from codex_lifecycle import CodexRunResult
from vnext_runtime.models import ExecutionProfile, ExecutionRequest
from vnext_runtime.run_scope import RunScope


FULL_ACCESS_ARGS = [
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ephemeral",
    "-m",
    "gpt-5.6-luna",
    "-c",
    'model_reasoning_effort="high"',
]


def phased_command(phases=("A", "B")) -> bytes:
    lines = [
        '<!-- bridge-command: {"command_id":14,"source":"scheduled_chatgpt",'
        '"based_on_report":13,"expected_generation":40,"kind":"EXECUTE"} -->',
        f'<!-- bridge-phased-task: {json.dumps({"schema_version": 1, "phases": list(phases)})} -->',
        "",
        "<!-- bridge-global:start -->",
        "global instructions",
        "<!-- bridge-global:end -->",
    ]
    for phase in phases:
        lines.extend(
            [
                f"<!-- bridge-phase:{phase}:start -->",
                f"phase {phase} work",
                f"<!-- bridge-phase:{phase}:end -->",
            ]
        )
    lines.extend(
        [
            "<!-- bridge-final-acceptance:start -->",
            "final checks",
            "<!-- bridge-final-acceptance:end -->",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_runtime(root: Path) -> phased_task.RuntimeInspection:
    run_dir = root / "worker" / "runtime" / "p" / "runs" / "run-014-test"
    return phased_task.prepare_runtime(
        run_dir,
        phased_command(),
        phased_task.ExecutionIdentity(
            project_id="p",
            command_id=14,
            run_id="run-014-test",
            claim_generation=41,
        ),
        bridge_root=root,
    )


def fake_result(
    *,
    marker: dict[str, str] | None = None,
    marker_result: str = "VALID",
) -> CodexRunResult:
    return CodexRunResult(
        exit_code=0,
        final_message=(
            'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}'
            if marker is not None
            else ""
        ),
        stdout_tail="",
        stderr_tail="",
        marker=marker,
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at="2001-01-15T00:00:01+08:00",
        process_exited_at="2001-01-15T00:00:02+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result=marker_result,
    )


class CommandResolutionTests(unittest.TestCase):
    def test_normal_executable_cmd_and_bat_resolution(self):
        for suffix in (".exe", ".cmd", ".bat"):
            with self.subTest(suffix=suffix):
                resolved = rf"X:\synthetic\c\Tools\codex{suffix}"
                with patch.object(executor.os, "name", "nt"), patch.object(
                    executor.shutil, "which", return_value=resolved
                ):
                    self.assertEqual(executor.command_argv("codex"), [resolved])

    def test_powershell_launcher_resolution(self):
        ps1 = r"X:\synthetic\c\Tools\codex.ps1"
        pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"

        def which(name):
            return {"codex": ps1, "pwsh": pwsh}.get(name)

        with patch.object(executor.os, "name", "nt"), patch.object(
            executor.shutil, "which", side_effect=which
        ):
            self.assertEqual(
                executor.command_argv("codex"),
                [pwsh, "-NoLogo", "-NoProfile", "-File", ps1],
            )

    def test_powershell_launcher_without_powershell_fails_closed(self):
        ps1 = r"X:\synthetic\c\Tools\codex.ps1"
        with patch.object(executor.os, "name", "nt"), patch.object(
            executor.shutil, "which", side_effect=lambda name: ps1 if name == "codex" else None
        ):
            with self.assertRaisesRegex(executor.WorkerError, "no PowerShell executable"):
                executor.command_argv("codex")

    def test_unresolved_command_falls_back_without_shell(self):
        with patch.object(executor.os, "name", "nt"), patch.object(
            executor.shutil, "which", return_value=None
        ):
            self.assertEqual(executor.command_argv("codex"), ["codex"])


class ConfigAndProfileTests(unittest.TestCase):
    def test_full_access_contract_and_conflicts(self):
        config = {
            "codex_execution_mode": "full_access",
            "codex_args": list(FULL_ACCESS_ARGS),
        }
        args = executor.codex_args_from_config(config)
        self.assertEqual(args, FULL_ACCESS_ARGS)
        self.assertIsNot(args, config["codex_args"])

        with self.assertRaisesRegex(executor.WorkerError, "full_access"):
            executor.codex_args_from_config({"codex_execution_mode": "sandbox", "codex_args": []})
        with self.assertRaisesRegex(executor.WorkerError, "requires"):
            executor.codex_args_from_config(
                {"codex_execution_mode": "full_access", "codex_args": ["exec"]}
            )
        for conflict in (
            "-a",
            "-s",
            "--approval-policy",
            "--ask-for-approval",
            "--full-auto",
            "--sandbox",
            "--approval-policy=never",
            "--ask-for-approval=never",
            "--sandbox=read-only",
        ):
            with self.subTest(conflict=conflict), self.assertRaises(executor.WorkerError):
                executor.codex_args_from_config(
                    {
                        "codex_execution_mode": "full_access",
                        "codex_args": [*FULL_ACCESS_ARGS, conflict],
                    }
                )

    def test_self_maintenance_rewrites_full_access_to_permission_profile(self):
        rewritten = executor.self_maintenance_codex_args(list(FULL_ACCESS_ARGS))

        self.assertNotIn(executor.FULL_ACCESS_FLAG, rewritten)
        self.assertNotIn("--sandbox", rewritten)
        self.assertNotIn("--ask-for-approval", rewritten)
        self.assertEqual(rewritten.count("--ignore-user-config"), 1)
        self.assertEqual(rewritten.count("--ignore-rules"), 1)
        self.assertEqual(rewritten.count('windows.sandbox="elevated"'), 1)
        self.assertEqual(rewritten.count('approval_policy="never"'), 1)
        self.assertEqual(
            rewritten.count('default_permissions=":workspace"'),
            1,
        )
        self.assertFalse(any(item.startswith("permissions.") for item in rewritten))
        self.assertNotIn("features.network_proxy=true", rewritten)
        self.assertIn("gpt-5.6-luna", rewritten)
        self.assertEqual(
            executor.validate_self_maintenance_codex_args(rewritten),
            rewritten,
        )

        for escape in (
            ["--add-dir", "outside"],
            ["--add-dir=outside"],
            ["-C", "outside"],
            ["--cd=outside"],
        ):
            with self.subTest(escape=escape), self.assertRaises(executor.WorkerError):
                executor.self_maintenance_codex_args([*FULL_ACCESS_ARGS, *escape])

        stripped = executor.self_maintenance_codex_args(
            [
                *FULL_ACCESS_ARGS,
                "-c",
                'sandbox_mode="danger-full-access"',
                "-c",
                'permissions.synthetic.filesystem={":root"="write"}',
                "-c",
                'sandbox_workspace_write.writable_roots=["outside"]',
                "-c",
                'default_permissions="other"',
                "-c",
                "features.network_proxy=true",
                "-c",
                'windows.sandbox="unelevated"',
                "--ignore-user-config",
                "--ignore-rules",
            ]
        )
        self.assertNotIn('sandbox_mode="danger-full-access"', stripped)
        self.assertFalse(any("permissions.synthetic" in item for item in stripped))
        self.assertNotIn(
            'sandbox_workspace_write.writable_roots=["outside"]',
            stripped,
        )
        self.assertNotIn('default_permissions="other"', stripped)
        self.assertNotIn("features.network_proxy=true", stripped)
        self.assertNotIn('windows.sandbox="unelevated"', stripped)
        self.assertEqual(stripped.count("--ignore-user-config"), 1)
        self.assertEqual(stripped.count("--ignore-rules"), 1)
        self.assertEqual(stripped.count('windows.sandbox="elevated"'), 1)
        self.assertEqual(stripped.count('default_permissions=":workspace"'), 1)
        self.assertFalse(any(item.startswith("permissions.") for item in stripped))

    def test_self_maintenance_permission_profile_validation_fails_closed(self):
        with self.assertRaises(executor.WorkerError):
            executor.validate_self_maintenance_codex_args(list(FULL_ACCESS_ARGS))
        with self.assertRaises(executor.WorkerError):
            executor.validate_self_maintenance_codex_args(
                [
                    "exec",
                    "--sandbox",
                    "workspace-write",
                    "-c",
                    'approval_policy="never"',
                ]
            )

        missing_workspace_profile = list(
            executor.self_maintenance_codex_args(list(FULL_ACCESS_ARGS))
        )
        index = missing_workspace_profile.index('default_permissions=":workspace"')
        del missing_workspace_profile[index - 1 : index + 1]
        with self.assertRaises(executor.WorkerError):
            executor.validate_self_maintenance_codex_args(missing_workspace_profile)

        injected_network = list(
            executor.self_maintenance_codex_args(list(FULL_ACCESS_ARGS))
        )
        injected_network.extend(["-c", "features.network_proxy=true"])
        with self.assertRaises(executor.WorkerError):
            executor.validate_self_maintenance_codex_args(injected_network)

    def test_profile_direct_model_precedes_config_model(self):
        profile = executor.executor_profile_from_args(
            [
                "exec",
                "-c",
                'model="configured-model"',
                "--config=model_reasoning_effort='low'",
                "--model",
                "direct-model",
            ],
            source="worker_default",
        )
        self.assertEqual(
            profile,
            {
                "model": "direct-model",
                "reasoning_effort": "low",
                "source": "worker_default",
            },
        )

    def test_profile_supports_equals_forms_and_sources(self):
        profile = executor.executor_profile_from_args(
            ["--model=direct", "-c=model=from-config", "-c=model_reasoning_effort=high"],
            source="command_override",
        )
        self.assertEqual(profile["model"], "direct")
        self.assertEqual(profile["reasoning_effort"], "high")
        self.assertEqual(profile["source"], "command_override")

        default_effective, default_profile = executor.effective_codex_args(
            FULL_ACCESS_ARGS, None
        )
        self.assertEqual(default_effective, FULL_ACCESS_ARGS)
        self.assertEqual(default_profile["source"], "worker_default")

        override_effective, override_profile = executor.effective_codex_args(
            FULL_ACCESS_ARGS,
            {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        )
        self.assertEqual(override_profile["source"], "command_override")
        self.assertEqual(override_effective[override_effective.index("-m") + 1], "gpt-5.6-sol")

    def test_effective_args_replace_only_executor_keys_and_do_not_mutate_input(self):
        defaults = [
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            "old-one",
            "--model=old-two",
            "-c",
            'model="old-three"',
            "--config=model_reasoning_effort='low'",
            "-c",
            "features.example=true",
            "--config=other.setting=true",
        ]
        original = list(defaults)
        effective, _profile = executor.effective_codex_args(
            defaults,
            {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
        )
        self.assertEqual(defaults, original)
        self.assertNotIn("old-one", effective)
        self.assertFalse(any(arg.startswith("--model=") for arg in effective))
        self.assertFalse(any("old-three" in arg for arg in effective))
        self.assertFalse(any("model_reasoning_effort='low'" in arg for arg in effective))
        self.assertIn("features.example=true", effective)
        self.assertIn("--config=other.setting=true", effective)
        self.assertEqual(effective.count("-m"), 1)
        self.assertIn('model_reasoning_effort="xhigh"', effective)


class SelfMaintenancePromptTests(unittest.TestCase):
    def test_candidate_prompt_guard_preserves_read_and_reserves_deployment(self):
        base = "base prompt"
        guarded = executor.add_self_maintenance_prompt_guard(base)

        self.assertTrue(guarded.startswith(base))
        self.assertIn("You may READ host files", guarded)
        self.assertIn("WRITE only inside the current Candidate workspace", guarded)
        self.assertIn("Do not commit, push", guarded)
        self.assertIn("outer authority", guarded)


class PromptTests(unittest.TestCase):
    def test_ordinary_prompt_is_character_compatible(self):
        expected = """You are the Codex executor in an unattended AI-Agent-Bridge workflow.

Project id: p

Rules:
- Execute exactly one bridge command below.
- Treat the mission and command as authoritative task context.
- Do not edit the AI-Agent-Bridge message-bus files themselves.
- Do not broaden the task merely because extra improvements are possible.
- Perform only the validation needed by the command.
- If the command explicitly requires the configured completion-notification mechanism, invoke that mechanism exactly as requested.
- Your final response will be captured verbatim by the local worker and returned to the ChatGPT supervisor.
- In the final response, clearly distinguish verified results, failures, and remaining blockers.
- Do not claim tests, commits, pushes, email delivery, or other actions succeeded unless you verified them.
- For a FINALIZE command, include exactly one machine-readable line:
  BRIDGE_FINAL_JSON: {"final_delivery":"SUCCESS","completion_email":"SENT"}
  only if both final delivery and the completion email were actually verified successful.

===== MISSION =====
mission

===== COMMAND =====
command
        """
        expected = expected.rstrip(" ")
        self.assertEqual(executor.build_prompt("p", "mission", "command"), expected)
        # Other Worker integration tests intentionally exercise the real
        # hardened entry point in-process.  Its stable captured original is
        # the compatibility wrapper we are characterizing here.
        self.assertEqual(
            hardened._ORIGINAL_BUILD_PROMPT("p", "mission", "command"), expected
        )

    def test_phased_prompt_is_bootstrap_only_and_preserves_finalize_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            helper = root / "worker" / "bridge_worker.py"
            prompt = executor.build_phased_prompt(
                "p",
                "mission",
                runtime,
                bridge_root=root,
                helper_path=helper,
            )
            self.assertIn("ONE Bridge command", prompt)
            self.assertIn(str(runtime.paths.global_projection), prompt)
            self.assertIn(str(runtime.paths.current_phase), prompt)
            self.assertIn(str(runtime.paths.progress), prompt)
            self.assertIn(str(helper.resolve()), prompt)
            self.assertIn("complete-final", prompt)
            self.assertIn("BRIDGE_FINAL_JSON", prompt)
            self.assertNotIn("phase B work", prompt)
            self.assertNotIn("phase C work", prompt)


class HardenedBoundaryTests(unittest.TestCase):
    def test_install_hardening_injects_contract_into_ordinary_and_phased_prompts(self):
        original_prompt = bw.build_prompt
        original_run = bw.codex_run
        try:
            hardened.install_hardening()
            ordinary = bw.build_prompt("p", "mission", "command")
            self.assertEqual(ordinary.count("WORKER-OWNED LEASE / RESULT CONTRACT"), 1)
            # The existing hardening text lists SUCCESS/FAILED/BLOCKED marker
            # examples; the important extraction invariant is one contract
            # block, not duplicated ordinary prompt wrapping.
            self.assertEqual(
                ordinary.count("===== WORKER-OWNED LEASE / RESULT CONTRACT ====="),
                1,
            )

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = make_runtime(root)
                phased = bw.build_phased_prompt(
                    "p", "mission", runtime, bridge_root=root
                )
                self.assertEqual(
                    phased.count("WORKER-OWNED LEASE / RESULT CONTRACT"), 1
                )
                self.assertNotIn("phase B work", phased)
        finally:
            bw.build_prompt = original_prompt
            bw.codex_run = original_run

    def test_install_hardening_codex_run_injects_strict_parser(self):
        original_prompt = bw.build_prompt
        original_run = bw.codex_run
        try:
            hardened.install_hardening()
            result = fake_result(marker={"status": "SUCCESS"})
            with patch.object(
                hardened, "_ORIGINAL_CODEX_RUN", return_value=result
            ) as original:
                returned = bw.codex_run(example="value")
            self.assertIs(returned, result)
            self.assertIs(
                original.call_args.kwargs["marker_parser"],
                hardened.parse_execution_result,
            )
        finally:
            bw.build_prompt = original_prompt
            bw.codex_run = original_run


class LifecycleAdapterTests(unittest.TestCase):
    def test_adapter_passes_exact_invocation_and_returns_lifecycle_result(self):
        result = object()
        output = Path("run") / "final-message.txt"
        stdout = Path("run") / "stdout.log"
        stderr = Path("run") / "stderr.log"
        guard = lambda: (True, "allowed")
        parser = lambda _text: {"status": "SUCCESS"}
        logger = lambda _event, _fields: None

        with patch.object(
            executor.codex_lifecycle,
            "run_codex_process",
            return_value=result,
        ) as lifecycle:
            returned = executor.run_codex(
                codex_argv=["pwsh", "-NoLogo"],
                codex_args=["exec", "--ephemeral"],
                workdir=Path("project"),
                output_file=output,
                stdout_log_path=stdout,
                stderr_log_path=stderr,
                prompt="prompt",
                timeout_seconds=123,
                final_grace_timeout_seconds=4.5,
                cleanup_timeout_seconds=6.5,
                marker_stable_seconds=0.7,
                poll_interval_seconds=0.08,
                max_log_bytes=1234,
                max_final_message_bytes=5678,
                marker_parser=parser,
                guard_check=guard,
                guard_interval_seconds=9.0,
                event_logger=logger,
            )

        self.assertIs(returned, result)
        self.assertEqual(
            lifecycle.call_args.kwargs,
            {
                "args": [
                    "pwsh",
                    "-NoLogo",
                    "exec",
                    "--ephemeral",
                    "-C",
                    "project",
                    "--output-last-message",
                    str(Path("run") / "final-message.txt"),
                    "-",
                ],
                "workdir": Path("project"),
                "output_file": output,
                "stdout_log_path": stdout,
                "stderr_log_path": stderr,
                "prompt": "prompt",
                "execution_timeout_seconds": 123,
                "final_grace_timeout_seconds": 4.5,
                "cleanup_timeout_seconds": 6.5,
                "marker_stable_seconds": 0.7,
                "poll_interval_seconds": 0.08,
                "max_log_bytes": 1234,
                "max_final_message_bytes": 5678,
                "marker_parser": parser,
                "guard_check": guard,
                "guard_interval_seconds": 9.0,
                "event_logger": logger,
            },
        )


class ScopedProviderCompatibilityTests(unittest.TestCase):
    def test_scoped_worker_adapter_uses_one_legacy_lifecycle_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-004-test"
            scope = RunScope.from_claimed_state(
                {
                    "status": "CODEX_RUNNING",
                    "project_id": "p",
                    "latest_command": 4,
                    "generation": 8,
                    "active_run": {
                        "command_id": 4,
                        "run_id": "run-004-test",
                        "claimed_generation": 8,
                    },
                },
                run_dir=run_dir,
            )
            scope.activate()
            request = ExecutionRequest(
                project_id="p",
                command_id=4,
                run_id="run-004-test",
                workdir=Path(tmp),
                mission="mission",
                command_text="command",
                kind="EXECUTE",
                profile=ExecutionProfile(),
            )
            result = fake_result(marker={"status": "SUCCESS"})
            with patch.object(bw, "_codex_run_legacy", return_value=result) as lifecycle:
                returned = bw.codex_run(
                    codex_argv=["python"],
                    codex_args=FULL_ACCESS_ARGS,
                    workdir=Path(tmp),
                    output_file=run_dir / "final-message.txt",
                    prompt="scoped prompt",
                    timeout_seconds=10,
                    run_scope=scope,
                    execution_request=request,
                )

            self.assertIs(returned, result)
            lifecycle.assert_called_once()
            self.assertEqual(lifecycle.call_args.kwargs["prompt"], "scoped prompt")
            self.assertEqual(
                lifecycle.call_args.kwargs["output_file"],
                run_dir / "final-message.txt",
            )
            self.assertTrue(scope.provider_invoked)
            scope.close()


class WorkerExecutorIntegrationTests(unittest.TestCase):
    def process(self, command: str, *, phased: bool) -> tuple[bool, list[dict], Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        project = root / "projects" / "p"
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir()
        (project / "MISSION.md").write_text("mission", encoding="utf-8")
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "generation": 40,
            "latest_command": 14,
            "latest_report": 13,
            "active_run": None,
        }
        state_path = project / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (project / "commands" / "command-014.md").write_text(
            command, encoding="utf-8"
        )
        remote_state = dict(state)
        lifecycle_calls: list[dict] = []

        def publish(**kwargs):
            nonlocal remote_state
            payloads = kwargs["payload_builder"](remote_state)
            for path, text in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            remote_state = json.loads(payloads[kwargs["state_path"]])
            return remote_state

        def run(**kwargs):
            lifecycle_calls.append(kwargs)
            if phased:
                run_dir = kwargs["output_file"].parent
                phased_task.advance_phase(run_dir, "A")
                phased_task.advance_phase(run_dir, "B")
                phased_task.complete_final(run_dir)
            return fake_result(marker={"status": "SUCCESS"})

        config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", executor.FULL_ACCESS_FLAG],
            "network_guard": {"enabled": False},
        }
        with patch.object(bw, "publish_cas", side_effect=publish), patch.object(
            bw, "get_head", return_value="head"
        ), patch.object(executor, "run_codex", side_effect=run):
            processed = bw.process_project(
                root, "p", {"workdir": "__BRIDGE_ROOT__"}, config
            )
        return processed, lifecycle_calls, root

    def test_ordinary_worker_command_invokes_executor_once(self):
        command = (
            '<!-- bridge-command: {"command_id":14,"source":"scheduled_chatgpt",'
            '"based_on_report":13,"expected_generation":40,"kind":"EXECUTE"} -->\n'
            "ordinary work\n"
        )
        processed, calls, _root = self.process(command, phased=False)
        self.assertTrue(processed)
        self.assertEqual(len(calls), 1)
        self.assertIn("ordinary work", calls[0]["prompt"])

    def test_phased_worker_command_invokes_executor_once(self):
        processed, calls, _root = self.process(
            phased_command().decode("utf-8"), phased=True
        )
        self.assertTrue(processed)
        self.assertEqual(len(calls), 1)
        self.assertIn("current-phase.md", calls[0]["prompt"])
        self.assertNotIn("phase B work", calls[0]["prompt"])




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
