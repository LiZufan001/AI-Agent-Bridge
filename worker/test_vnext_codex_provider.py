import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codex_lifecycle import CodexRunResult
import executor
from vnext_runtime.models import ExecutionOutcome, ExecutionProfile, ExecutionRequest, RunIdentity
from vnext_runtime.providers.executors.codex import (
    CodexExecutorProvider,
    CodexProviderError,
    CodexProviderSettings,
    as_executor_provider,
)
from vnext_runtime.run_scope import RunScope, RunScopeError


FULL_ACCESS_ARGS = (
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ephemeral",
    "-m",
    "gpt-5.6-luna",
    "-c",
    'model_reasoning_effort="high"',
)


class _Disposable:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def dispose(self) -> None:
        self.events.append(self.name)


def request(profile: ExecutionProfile | None = None) -> ExecutionRequest:
    return ExecutionRequest(
        project_id="engine-maintenance",
        command_id=4,
        run_id="run-004-test",
        workdir=Path("X:/synthetic/c/candidate"),
        mission="mission",
        command_text="command",
        kind="EXECUTE",
        profile=profile or ExecutionProfile(),
    )


def scope() -> RunScope:
    value = RunScope(RunIdentity("engine-maintenance", 4, "run-004-test", 8))
    value.activate()
    return value


def fake_codex_result(**updates: object) -> CodexRunResult:
    values = {
        "exit_code": 0,
        "final_message": "final response",
        "stdout_tail": "stdout",
        "stderr_tail": "stderr",
        "marker": {"status": "SUCCESS", "provider_detail": "hidden"},
        "launched_at": "2001-01-15T00:00:00+08:00",
        "final_marker_detected_at": "2001-01-15T00:00:01+08:00",
        "process_exited_at": "2001-01-15T00:00:02+08:00",
        "wrapper_pid": 123,
        "stdout_log_path": Path("stdout.log"),
        "stderr_log_path": Path("stderr.log"),
        "process_scope": "test",
        "contract_error": None,
        "runtime_error": None,
        "cleanup_error": None,
        "timed_out": False,
    }
    values.update(updates)
    return CodexRunResult(**values)  # type: ignore[arg-type]


class CodexProviderTests(unittest.TestCase):
    def settings(self, **updates: object) -> CodexProviderSettings:
        values = {
            "codex_command": sys.executable,
            "codex_execution_mode": "full_access",
            "codex_args": FULL_ACCESS_ARGS,
            "output_file": Path("run/final-message.txt"),
        }
        values.update(updates)
        return CodexProviderSettings(**values)  # type: ignore[arg-type]

    def test_identity_is_stable_and_satisfies_typed_contract(self) -> None:
        provider = CodexExecutorProvider(self.settings(), runner=Mock())

        self.assertEqual(provider.provider_id, "codex")
        self.assertIs(as_executor_provider(provider), provider)
        with self.assertRaises(AttributeError):
            provider.provider_id = "other"  # type: ignore[misc]

    def test_valid_request_maps_profile_and_invokes_existing_executor_once(self) -> None:
        runner = Mock(return_value=fake_codex_result())
        provider = CodexExecutorProvider(self.settings(), runner=runner)

        result = provider.execute(
            request(ExecutionProfile(model="gpt-5.6-sol", reasoning_effort="high")),
            scope(),
        )

        runner.assert_called_once()
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["codex_argv"], [sys.executable])
        self.assertEqual(kwargs["codex_args"].count("-m"), 1)
        self.assertEqual(
            kwargs["codex_args"][kwargs["codex_args"].index("-m") + 1],
            "gpt-5.6-sol",
        )
        self.assertIn('model_reasoning_effort="high"', kwargs["codex_args"])
        self.assertEqual(kwargs["workdir"], Path("X:/synthetic/c/candidate"))
        self.assertIn("engine-maintenance", kwargs["prompt"])
        self.assertEqual(result.provider_id, "codex")
        self.assertEqual(result.outcome, ExecutionOutcome.SUCCESS)

    def test_permission_profile_mode_is_validated_and_invoked(self) -> None:
        runner = Mock(return_value=fake_codex_result())
        permission_args = tuple(
            executor.self_maintenance_codex_args(list(FULL_ACCESS_ARGS))
        )
        provider = CodexExecutorProvider(
            self.settings(
                codex_execution_mode=executor.SELF_MAINTENANCE_PERMISSIONS_MODE,
                codex_args=permission_args,
            ),
            runner=runner,
        )

        provider.execute(request(), scope())

        args = runner.call_args.kwargs["codex_args"]
        self.assertNotIn(executor.FULL_ACCESS_FLAG, args)
        self.assertNotIn("--sandbox", args)
        self.assertIn('approval_policy="never"', args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        self.assertIn('windows.sandbox="elevated"', args)
        self.assertIn(
            f'permissions.{executor.SELF_MAINTENANCE_PERMISSION_PROFILE}.filesystem={{":root"="read"}}',
            args,
        )

    def test_scope_artifacts_are_used_and_provider_is_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-004-test"
            run_scope = RunScope(
                RunIdentity("engine-maintenance", 4, "run-004-test", 8),
                run_dir=run_dir,
            )
            run_scope.activate()
            runner = Mock(return_value=fake_codex_result())
            provider = CodexExecutorProvider(
                self.settings(output_file=run_dir / "final-message.txt"),
                runner=runner,
            )

            provider.execute(request(), run_scope)

            self.assertEqual(run_scope.provider_id, "codex")
            self.assertTrue(run_scope.provider_invoked)
            self.assertEqual(runner.call_args.kwargs["output_file"], run_dir / "final-message.txt")
            self.assertEqual(runner.call_args.kwargs["stdout_log_path"], run_dir / "stdout.log")
            self.assertEqual(runner.call_args.kwargs["stderr_log_path"], run_dir / "stderr.log")

    def test_lifecycle_exception_is_closed_by_scope_owner(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-004-test"
            run_scope = RunScope(
                RunIdentity("engine-maintenance", 4, "run-004-test", 8),
                run_dir=run_dir,
            )
            run_scope.activate()
            run_scope.own(_Disposable("cleanup", events))
            provider = CodexExecutorProvider(
                self.settings(output_file=run_dir / "final-message.txt"),
                runner=Mock(side_effect=RuntimeError("lifecycle failed")),
            )

            with self.assertRaisesRegex(RuntimeError, "lifecycle failed"):
                try:
                    provider.execute(request(), run_scope)
                finally:
                    run_scope.close()

        self.assertEqual(events, ["cleanup"])
        self.assertEqual(run_scope.state.value, "CLOSED")

    def test_forced_cleanup_after_valid_marker_preserves_business_success(self) -> None:
        runner = Mock(
            return_value=fake_codex_result(
                forced_cleanup=True,
                forced_cleanup_after_final=True,
                exit_code=9,
            )
        )
        provider = CodexExecutorProvider(self.settings(), runner=runner)

        result = provider.execute(request(), scope())

        self.assertEqual(result.outcome, ExecutionOutcome.SUCCESS)
        self.assertEqual(result.exit_code, 9)

    def test_second_execute_cannot_rerun_lifecycle(self) -> None:
        runner = Mock(return_value=fake_codex_result())
        provider = CodexExecutorProvider(self.settings(), runner=runner)
        run_scope = scope()

        provider.execute(request(), run_scope)
        with self.assertRaises(RunScopeError):
            provider.execute(request(), run_scope)
        runner.assert_called_once()

    def test_scope_artifact_mismatch_is_rejected_without_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-004-test"
            other_dir = Path(directory) / "run-other"
            run_scope = RunScope(
                RunIdentity("engine-maintenance", 4, "run-004-test", 8),
                run_dir=run_dir,
            )
            run_scope.activate()
            runner = Mock()
            provider = CodexExecutorProvider(
                self.settings(output_file=other_dir / "final-message.txt"),
                runner=runner,
            )

            with self.assertRaisesRegex(CodexProviderError, "artifact identity"):
                provider.execute(request(), run_scope)
            runner.assert_not_called()

    def test_invalid_environment_fails_before_launch(self) -> None:
        runner = Mock()
        provider = CodexExecutorProvider(
            self.settings(codex_execution_mode="sandbox"), runner=runner
        )

        with self.assertRaisesRegex(CodexProviderError, "full_access"):
            provider.execute(request(), scope())
        runner.assert_not_called()

    def test_invalid_profile_fails_before_launch(self) -> None:
        runner = Mock()
        provider = CodexExecutorProvider(self.settings(), runner=runner)

        with self.assertRaisesRegex(CodexProviderError, "unsupported characters"):
            provider.execute(
                request(ExecutionProfile(model="bad model", reasoning_effort="high")),
                scope(),
            )
        runner.assert_not_called()

    def test_missing_codex_command_fails_before_launch(self) -> None:
        runner = Mock()
        provider = CodexExecutorProvider(
            self.settings(codex_command="codex-command-that-is-not-installed"),
            runner=runner,
        )

        with self.assertRaisesRegex(CodexProviderError, "command not found"):
            provider.execute(request(), scope())
        runner.assert_not_called()

    def test_result_normalization_does_not_leak_codex_marker_details(self) -> None:
        runner = Mock(
            return_value=fake_codex_result(
                exit_code=17,
                marker={"status": "BLOCKED", "reason": "guard", "argv": ["secret"]},
                contract_error="blocked by provider",
            )
        )
        provider = CodexExecutorProvider(self.settings(), runner=runner)

        result = provider.execute(request(), scope())

        self.assertEqual(result.outcome, ExecutionOutcome.BLOCKED)
        self.assertEqual(result.exit_code, 17)
        self.assertEqual(result.final_message, "final response")
        self.assertEqual(result.completed_at, "2001-01-15T00:00:02+08:00")
        self.assertEqual(result.diagnostics.error, "blocked by provider")
        self.assertFalse(hasattr(result, "marker"))
        self.assertFalse(hasattr(result, "codex_args"))

    def test_scope_mismatch_is_rejected_without_launch(self) -> None:
        runner = Mock()
        provider = CodexExecutorProvider(self.settings(), runner=runner)
        other = RunScope(RunIdentity("other", 4, "run-004-test", 8))
        other.activate()

        with self.assertRaisesRegex(CodexProviderError, "does not match"):
            provider.execute(request(), other)
        runner.assert_not_called()

    def test_provider_has_no_canonical_publication_surface(self) -> None:
        provider = CodexExecutorProvider(self.settings(), runner=Mock())

        self.assertFalse(hasattr(provider, "publish_cas"))
        self.assertFalse(hasattr(provider, "publish_report"))
        self.assertFalse(hasattr(provider, "recover"))


if __name__ == "__main__":
    unittest.main()
