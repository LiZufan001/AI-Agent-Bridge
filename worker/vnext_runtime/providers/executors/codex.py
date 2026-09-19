"""Compatibility adapter for the existing Codex executor.

This module is intentionally a provider boundary, not a second Codex
implementation.  It validates the existing Worker configuration, delegates
argv construction and lifecycle management to :mod:`executor`, and exposes
only the normalized vNext result to callers.

The provider consumes the run-local artifact paths from ``RunScope``.  The
settings output path remains as a compatibility assertion for older callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, cast

import executor
import owner_notification
import protocol_core

from ...models import (
    ExecutionDiagnostics,
    ExecutionOutcome,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    ExecutorProvider,
)
from ...run_scope import RunScope, RunScopeState


class CodexProviderError(RuntimeError):
    """Raised when a Codex provider request cannot be safely executed."""


class _CodexRunResult(Protocol):
    """The provider's narrow view of the existing Codex result object."""

    exit_code: int
    final_message: str
    stdout_tail: str
    stderr_tail: str
    marker: dict[str, Any] | None
    launched_at: str
    process_exited_at: str | None
    final_marker_detected_at: str | None
    contract_error: str | None
    runtime_error: str | None
    cleanup_error: str | None
    timed_out: bool


CodexRunner = Callable[..., _CodexRunResult]
PromptBuilder = Callable[[ExecutionRequest], str]


@dataclass(frozen=True, slots=True)
class CodexProviderSettings:
    """Run-local settings translated to the existing Codex executor API."""

    codex_command: str
    codex_execution_mode: str
    codex_args: tuple[str, ...]
    output_file: Path
    codex_argv: tuple[str, ...] | None = None
    network_config: Mapping[str, object] | None = None
    timeout_seconds: int = 14_400
    final_grace_timeout_seconds: float = 20.0
    cleanup_timeout_seconds: float = 10.0
    marker_stable_seconds: float = 0.5
    poll_interval_seconds: float = 0.1
    max_log_bytes: int = 10 * 1024 * 1024
    max_final_message_bytes: int = 2 * 1024 * 1024
    marker_parser: Callable[[str], dict[str, Any] | None] | None = None
    event_logger: Callable[[str, dict[str, Any]], None] | None = None
    process_launcher: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.codex_command, str) or not self.codex_command.strip():
            raise ValueError("codex_command must be a non-empty string.")
        if not isinstance(self.codex_execution_mode, str):
            raise TypeError("codex_execution_mode must be a string.")
        if not isinstance(self.codex_args, tuple) or not all(
            isinstance(arg, str) for arg in self.codex_args
        ):
            raise TypeError("codex_args must be a tuple of strings.")
        if not isinstance(self.output_file, Path):
            raise TypeError("output_file must be a pathlib.Path.")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    @classmethod
    def from_worker_config(
        cls,
        config: Mapping[str, object],
        *,
        output_file: Path,
        marker_parser: Callable[[str], dict[str, Any] | None] | None = None,
        event_logger: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> "CodexProviderSettings":
        """Create typed provider settings from the existing Worker config."""

        raw_args = config.get("codex_args", ())
        if not isinstance(raw_args, (list, tuple)):
            raise TypeError("codex_args must be a list or tuple of strings.")
        return cls(
            codex_command=str(config.get("codex_command", "codex")),
            codex_execution_mode=str(config.get("codex_execution_mode", "")),
            codex_args=tuple(raw_args),
            output_file=output_file,
            timeout_seconds=int(config.get("codex_timeout_seconds", 14_400)),
            final_grace_timeout_seconds=float(
                config.get("codex_final_grace_seconds", 20)
            ),
            cleanup_timeout_seconds=float(
                config.get("codex_cleanup_timeout_seconds", 10)
            ),
            marker_stable_seconds=float(
                config.get("codex_final_marker_stable_seconds", 0.5)
            ),
            poll_interval_seconds=float(
                config.get("codex_lifecycle_poll_seconds", 0.1)
            ),
            max_log_bytes=int(
                config.get(
                    "codex_log_warning_bytes",
                    config.get("codex_max_log_bytes", 10 * 1024 * 1024),
                )
            ),
            max_final_message_bytes=int(
                config.get("codex_max_final_message_bytes", 2 * 1024 * 1024)
            ),
            marker_parser=marker_parser,
            event_logger=event_logger,
        )


class CodexExecutorProvider:
    """The vNext ``ExecutorProvider`` view of the mature Codex path."""

    __slots__ = ("_settings", "_runner", "_prompt_builder")

    def __init__(
        self,
        settings: CodexProviderSettings,
        *,
        runner: CodexRunner | None = None,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._runner = (
            runner if runner is not None else cast(CodexRunner, executor.run_codex)
        )
        self._prompt_builder = prompt_builder or self._ordinary_prompt

    @property
    def provider_id(self) -> str:
        """Return the immutable provider identity pinned by this adapter."""

        return "codex"

    def validate_environment(self, request: ExecutionRequest) -> None:
        """Reuse existing full-access and command-profile validation."""

        self._validate_request(request)
        config = {
            "codex_execution_mode": self._settings.codex_execution_mode,
            "codex_args": list(self._settings.codex_args),
        }
        try:
            executor.codex_args_from_config(config)
        except executor.WorkerError as exc:
            raise CodexProviderError(str(exc)) from exc
        self._codex_profile_override(request.profile)
        try:
            executor.validate_codex_command(self._settings.codex_command)
            executor.command_argv(self._settings.codex_command)
        except executor.WorkerError as exc:
            raise CodexProviderError(str(exc)) from exc

    def execute(self, request: ExecutionRequest, scope: RunScope) -> ExecutionResult:
        """Invoke Codex once, then let the Worker own any declared email send."""

        self.validate_environment(request)
        self._validate_scope(request, scope)
        output_file = self._scope_output_file(scope)
        scope.begin_provider_execution(self.provider_id)
        override = self._codex_profile_override(request.profile)
        codex_args, _profile = executor.effective_codex_args(
            list(self._settings.codex_args), override
        )

        try:
            notification_spec = owner_notification.parse_spec(request.command_text)
        except owner_notification.OwnerNotificationError as exc:
            raise CodexProviderError(
                f"owner-notification declaration preflight failed: {exc}"
            ) from exc

        prompt = self._prompt_builder(request)
        if notification_spec is not None:
            prompt += (
                "\n\nWORKER OWNER-NOTIFICATION BOUNDARY:\n"
                "This command declares a Worker-owned owner notification. "
                "Do not invoke an external mail sender or SMTP directly; "
                "or any other email sender. Verify only the requested blocker semantics "
                "and return BRIDGE_EXECUTION_JSON status SUCCESS only when the declared "
                "notification should still be sent. The Worker will perform the single "
                "side effect after your successful final marker.\n"
            )

        runner_kwargs: dict[str, object] = {
            "codex_argv": list(self._settings.codex_argv)
            if self._settings.codex_argv is not None
            else executor.command_argv(self._settings.codex_command),
            "codex_args": codex_args,
            "workdir": request.workdir,
            "output_file": output_file,
            "prompt": prompt,
            "timeout_seconds": self._settings.timeout_seconds,
            "stdout_log_path": output_file.with_name("stdout.log"),
            "stderr_log_path": output_file.with_name("stderr.log"),
            "final_grace_timeout_seconds": self._settings.final_grace_timeout_seconds,
            "cleanup_timeout_seconds": self._settings.cleanup_timeout_seconds,
            "marker_stable_seconds": self._settings.marker_stable_seconds,
            "poll_interval_seconds": self._settings.poll_interval_seconds,
            "max_log_bytes": self._settings.max_log_bytes,
            "max_final_message_bytes": self._settings.max_final_message_bytes,
            "marker_parser": self._settings.marker_parser,
            "event_logger": self._settings.event_logger or scope.event_logger,
        }
        if self._settings.network_config is not None:
            runner_kwargs["network_config"] = dict(self._settings.network_config)
        if self._settings.process_launcher is not None:
            runner_kwargs["process_launcher"] = self._settings.process_launcher

        result = self._runner(**runner_kwargs)
        if notification_spec is not None:
            setattr(result, "owner_notification_receipt", None)
            setattr(result, "owner_notification_receipt_error", None)
            marker = result.marker or {}
            if marker.get("status") == "SUCCESS":
                assessment = owner_notification.deliver(
                    project_id=request.project_id,
                    command_id=request.command_id,
                    run_id=request.run_id,
                    spec=notification_spec,
                )
                setattr(result, "owner_notification_receipt", assessment.receipt)
                setattr(result, "owner_notification_receipt_error", assessment.error)
                if assessment.receipt is None:
                    result.marker = {
                        "status": "FAILED",
                        "reason": "OWNER_NOTIFICATION_RECEIPT_MISSING",
                    }
                    result.contract_error = (
                        "OWNER_NOTIFICATION_RECEIPT_MISSING: Worker-owned email gateway "
                        "did not produce an authoritative provider-accepted receipt; "
                        "automatic resend is suppressed"
                    )
        return self._normalize(result)

    @staticmethod
    def _ordinary_prompt(request: ExecutionRequest) -> str:
        return executor.build_prompt(
            request.project_id, request.mission, request.command_text
        )

    @staticmethod
    def _validate_request(request: ExecutionRequest) -> None:
        if not isinstance(request, ExecutionRequest):
            raise TypeError("request must be an ExecutionRequest.")
        if request.kind != "EXECUTE":
            raise CodexProviderError(
                f"Codex provider does not support request kind {request.kind!r}."
            )

    @staticmethod
    def _validate_scope(request: ExecutionRequest, scope: RunScope) -> None:
        if not isinstance(scope, RunScope):
            raise TypeError("scope must be a RunScope.")
        if scope.state is not RunScopeState.ACTIVE:
            raise CodexProviderError("Codex execution requires an ACTIVE RunScope.")
        identity = scope.identity
        if (
            identity.project_id != request.project_id
            or identity.command_id != request.command_id
            or identity.run_id != request.run_id
        ):
            raise CodexProviderError(
                "RunScope identity does not match the ExecutionRequest."
            )

    def _scope_output_file(self, scope: RunScope) -> Path:
        artifacts = scope.artifacts
        if artifacts is None:
            return self._settings.output_file
        if self._settings.output_file != artifacts.final_message:
            raise CodexProviderError(
                "Codex output path does not match the RunScope artifact identity."
            )
        return artifacts.final_message

    @staticmethod
    def _codex_profile_override(
        profile: ExecutionProfile,
    ) -> dict[str, str] | None:
        model = profile.model
        effort = profile.reasoning_effort
        service_tier = profile.service_tier
        if model is None and effort is None and service_tier is None:
            return None
        if model is None or effort is None:
            raise CodexProviderError(
                "Codex model and reasoning_effort must be supplied together."
            )
        raw_executor = {
            "model": model,
            "reasoning_effort": effort,
        }
        if service_tier is not None:
            raw_executor["service_tier"] = service_tier
        try:
            return protocol_core.command_executor_override(
                {"executor": raw_executor}
            )
        except protocol_core.ProtocolViolation as exc:
            raise CodexProviderError(str(exc)) from exc

    def _normalize(self, result: _CodexRunResult) -> ExecutionResult:
        marker = result.marker or {}
        status = marker.get("status")
        outcome = (
            ExecutionOutcome(status)
            if status in {"SUCCESS", "FAILED", "BLOCKED"}
            else ExecutionOutcome.FAILED
        )
        error = (
            result.contract_error
            or result.runtime_error
            or result.cleanup_error
            or ("Codex execution timed out." if result.timed_out else None)
        )
        return ExecutionResult(
            provider_id=self.provider_id,
            outcome=outcome,
            exit_code=result.exit_code,
            final_message=result.final_message,
            launched_at=result.launched_at,
            completed_at=result.process_exited_at or result.final_marker_detected_at,
            diagnostics=ExecutionDiagnostics(
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
                error=error,
            ),
        )


def as_executor_provider(provider: CodexExecutorProvider) -> ExecutorProvider:
    """Type-checking seam documenting that the adapter satisfies the contract."""

    return provider
