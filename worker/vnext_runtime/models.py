"""Immutable, agent-neutral execution models for the next provider phase."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .run_scope import RunScope


class ProviderState(str, Enum):
    """Runtime-local provider availability; never a Protocol-v2 state."""

    REGISTERED = "REGISTERED"
    AVAILABLE = "AVAILABLE"
    DRAINING = "DRAINING"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class ExecutionOutcome(str, Enum):
    """Normalized provider outcome values, independent of wire state names."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """The local identity of one already-claimed Protocol-v2 run."""

    project_id: str
    command_id: int
    run_id: str
    claim_generation: int

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.run_id, "run_id")
        _require_non_bool_int(self.command_id, "command_id", minimum=1)
        _require_non_bool_int(self.claim_generation, "claim_generation", minimum=0)


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """Provider-neutral profile facts passed to one execution."""

    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None

    def __post_init__(self) -> None:
        if self.model is not None:
            _require_text(self.model, "model")
        if self.reasoning_effort is not None:
            _require_text(self.reasoning_effort, "reasoning_effort")
        if self.service_tier is not None:
            _require_text(self.service_tier, "service_tier")


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """All immutable input facts needed by a future ExecutorProvider."""

    project_id: str
    command_id: int
    run_id: str
    workdir: Path
    mission: str
    command_text: str
    kind: str
    profile: ExecutionProfile

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.run_id, "run_id")
        _require_non_bool_int(self.command_id, "command_id", minimum=1)
        if not isinstance(self.workdir, Path):
            raise TypeError("workdir must be a pathlib.Path.")
        if not isinstance(self.mission, str):
            raise TypeError("mission must be a string.")
        if not isinstance(self.command_text, str):
            raise TypeError("command_text must be a string.")
        _require_text(self.kind, "kind")
        if not isinstance(self.profile, ExecutionProfile):
            raise TypeError("profile must be an ExecutionProfile.")


@dataclass(frozen=True, slots=True)
class ExecutionDiagnostics:
    """Bounded, provider-neutral diagnostics returned with an execution."""

    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stdout_tail, str):
            raise TypeError("stdout_tail must be a string.")
        if not isinstance(self.stderr_tail, str):
            raise TypeError("stderr_tail must be a string.")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string or None.")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Immutable evidence returned by a provider; it cannot publish state."""

    provider_id: str
    outcome: ExecutionOutcome
    exit_code: int | None
    final_message: str
    launched_at: str | None
    completed_at: str | None
    diagnostics: ExecutionDiagnostics

    def __post_init__(self) -> None:
        _require_text(self.provider_id, "provider_id")
        if not isinstance(self.outcome, ExecutionOutcome):
            try:
                object.__setattr__(self, "outcome", ExecutionOutcome(self.outcome))
            except (TypeError, ValueError) as exc:
                raise TypeError("outcome must be an ExecutionOutcome.") from exc
        if self.exit_code is not None:
            _require_non_bool_int(self.exit_code, "exit_code", minimum=-2147483648)
        if not isinstance(self.final_message, str):
            raise TypeError("final_message must be a string.")
        if self.launched_at is not None and not isinstance(self.launched_at, str):
            raise TypeError("launched_at must be a string or None.")
        if self.completed_at is not None and not isinstance(self.completed_at, str):
            raise TypeError("completed_at must be a string or None.")
        if not isinstance(self.diagnostics, ExecutionDiagnostics):
            raise TypeError("diagnostics must be an ExecutionDiagnostics.")


class ExecutorProvider(Protocol):
    """The narrow provider contract consumed by the next migration phase."""

    provider_id: str

    def validate_environment(self, request: ExecutionRequest) -> None:
        """Validate provider prerequisites without mutating canonical state."""

    def execute(self, request: ExecutionRequest, scope: RunScope) -> ExecutionResult:
        """Execute once and return evidence to a future Coordinator."""


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")


def _require_non_bool_int(value: int, label: str, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}.")
