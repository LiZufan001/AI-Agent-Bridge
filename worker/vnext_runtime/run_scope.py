"""Local ownership for one already-claimed Protocol-v2 execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .effects import Disposable, EffectHandle, EffectScope
from .models import RunIdentity


class RunScopeState(str, Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    COMPLETING = "COMPLETING"
    DISPOSING = "DISPOSING"
    CLOSED = "CLOSED"


class RunScopeError(RuntimeError):
    """Raised when a scope lifecycle operation is invalid."""


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """The three mature per-run artifact paths owned by one scope."""

    run_dir: Path
    final_message: Path
    stdout_log: Path
    stderr_log: Path

    @classmethod
    def from_run_dir(cls, run_dir: Path, run_id: str) -> "RunArtifacts":
        if not isinstance(run_dir, Path):
            raise TypeError("run_dir must be a pathlib.Path.")
        if run_dir.name != run_id:
            raise RunScopeError(
                f"Run directory {run_dir} does not belong to run {run_id!r}."
            )
        return cls(
            run_dir=run_dir,
            final_message=run_dir / "final-message.txt",
            stdout_log=run_dir / "stdout.log",
            stderr_log=run_dir / "stderr.log",
        )


class RunScope:
    """Own local effects for exactly one claimed Protocol-v2 run.

    ``from_claimed_state`` is the production construction seam.  It verifies
    the active lease before deriving the local identity; it does not write or
    otherwise mutate canonical state.
    """

    __slots__ = (
        "_identity",
        "_effects",
        "_state",
        "_artifacts",
        "_event_logger",
        "_provider_id",
        "_provider_invoked",
        "_containment",
        "_process_launcher",
    )

    def __init__(
        self,
        identity: RunIdentity,
        *,
        run_dir: Path | None = None,
        event_logger: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._identity = identity
        self._effects = EffectScope(identity.run_id)
        self._state = RunScopeState.CREATED
        self._artifacts = (
            RunArtifacts.from_run_dir(run_dir, identity.run_id)
            if run_dir is not None
            else None
        )
        self._event_logger = event_logger
        self._provider_id: str | None = None
        self._provider_invoked = False
        self._containment: Disposable | None = None
        self._process_launcher: object | None = None

    @classmethod
    def from_claimed_state(
        cls,
        state: Mapping[str, object],
        *,
        project_id: str | None = None,
        command_id: int | None = None,
        run_id: str | None = None,
        run_dir: Path | None = None,
        event_logger: Callable[[str, dict[str, object]], None] | None = None,
    ) -> "RunScope":
        """Derive a scope only from an exact active Protocol-v2 lease."""

        if not isinstance(state, Mapping) or state.get("status") != "CODEX_RUNNING":
            raise RunScopeError("RunScope requires a claimed CODEX_RUNNING state.")
        active = state.get("active_run")
        if not isinstance(active, Mapping):
            raise RunScopeError("Claimed state is missing its active_run identity.")

        actual_project = project_id if project_id is not None else state.get("project_id")
        actual_command = command_id if command_id is not None else active.get("command_id")
        actual_run = run_id if run_id is not None else active.get("run_id")
        if not isinstance(actual_project, str):
            raise RunScopeError("Claimed state does not identify a project.")
        if isinstance(actual_command, bool) or not isinstance(actual_command, int):
            raise RunScopeError("Claimed state does not identify a command.")
        if not isinstance(actual_run, str):
            raise RunScopeError("Claimed state does not identify a run.")

        if active.get("project_id", actual_project) != actual_project:
            raise RunScopeError("Claimed active_run project identity does not match.")
        if active.get("command_id") != actual_command:
            raise RunScopeError("Claimed active_run command identity does not match.")
        if active.get("run_id") != actual_run:
            raise RunScopeError("Claimed active_run run identity does not match.")
        claimed_generation = active.get("claimed_generation")
        if isinstance(claimed_generation, bool) or not isinstance(claimed_generation, int):
            raise RunScopeError("Claimed active_run is missing claimed_generation.")
        if state.get("generation") != claimed_generation:
            raise RunScopeError("Claimed generation is not the current state generation.")
        if state.get("latest_command") not in {None, actual_command}:
            raise RunScopeError("Claimed command is not the canonical latest command.")

        return cls(
            RunIdentity(actual_project, actual_command, actual_run, claimed_generation),
            run_dir=run_dir,
            event_logger=event_logger,
        )

    from_claimed_run = from_claimed_state

    @property
    def identity(self) -> RunIdentity:
        return self._identity

    @property
    def state(self) -> RunScopeState:
        return self._state

    @property
    def artifacts(self) -> RunArtifacts | None:
        return self._artifacts

    @property
    def event_logger(self) -> Callable[[str, dict[str, object]], None] | None:
        return self._event_logger

    @property
    def provider_id(self) -> str | None:
        return self._provider_id

    @property
    def provider_invoked(self) -> bool:
        return self._provider_invoked

    @property
    def containment(self) -> Disposable | None:
        """Return the one optional run-owned egress containment resource."""

        return self._containment

    @property
    def process_launcher(self) -> object | None:
        """Return the launcher bound to this exact run, if one is required."""

        return self._process_launcher

    def activate(self) -> None:
        self._require(RunScopeState.CREATED)
        self._state = RunScopeState.ACTIVE

    def begin_completion(self) -> None:
        if self._state is RunScopeState.COMPLETING:
            return
        self._require(RunScopeState.ACTIVE)
        self._state = RunScopeState.COMPLETING

    def own(self, disposable: Disposable) -> EffectHandle:
        if self._state is not RunScopeState.ACTIVE:
            raise RunScopeError(
                f"Run scope must be ACTIVE to own an effect, current state is "
                f"{self._state.value}."
            )
        return self._effects.own(disposable)

    register = own

    def own_guard(self, guard: Disposable) -> EffectHandle:
        """Register a run guard as an effect owned by this exact scope."""

        return self.own(guard)

    def own_containment(
        self, containment: Disposable, *, process_launcher: object | None = None
    ) -> EffectHandle:
        """Bind one containment resource and its contained-process launcher."""

        if self._containment is not None:
            raise RunScopeError("RunScope already owns a containment resource.")
        resource_identity = getattr(containment, "identity", None)
        if resource_identity != self._identity:
            raise RunScopeError(
                "containment resource is not owned by this exact RunScope."
            )
        if process_launcher is None or getattr(process_launcher, "is_contained", False) is not True:
            raise RunScopeError("contained process launcher is required with containment.")
        if not callable(getattr(process_launcher, "bind_containment", None)):
            raise RunScopeError(
                "contained process launcher cannot bind the exact containment."
            )
        resource_launcher = getattr(containment, "process_launcher", process_launcher)
        if resource_launcher is not process_launcher:
            raise RunScopeError(
                "containment resource and process launcher are not the same boundary."
            )
        handle = self.own(containment)
        self._containment = containment
        self._process_launcher = process_launcher
        return handle

    def pin_provider(self, provider_id: str) -> None:
        """Pin one provider for this scope; replacements are rejected."""

        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string.")
        if self._state is not RunScopeState.ACTIVE:
            raise RunScopeError("Provider pinning requires an ACTIVE RunScope.")
        if self._provider_id is None:
            self._provider_id = provider_id
        elif self._provider_id != provider_id:
            raise RunScopeError(
                f"RunScope provider is pinned to {self._provider_id!r}, "
                f"not {provider_id!r}."
            )

    def begin_provider_execution(self, provider_id: str) -> None:
        """Reserve the single provider invocation allowed by this scope."""

        self.pin_provider(provider_id)
        if self._provider_invoked:
            raise RunScopeError("A provider execution has already started for this scope.")
        self._provider_invoked = True

    def release(self, handle: EffectHandle) -> None:
        if self._state in {RunScopeState.DISPOSING, RunScopeState.CLOSED}:
            return
        self._effects.release(handle)

    @property
    def disposal_errors(self) -> tuple[BaseException, ...]:
        return self._effects.disposal_errors

    def close(self) -> tuple[BaseException, ...]:
        """Close the scope and release every owned effect exactly once."""

        if self._state is RunScopeState.CLOSED:
            return self._effects.disposal_errors
        if self._state is RunScopeState.CREATED:
            self._state = RunScopeState.COMPLETING
        elif self._state is RunScopeState.ACTIVE:
            self._state = RunScopeState.COMPLETING
        self._state = RunScopeState.DISPOSING
        try:
            return self._effects.close()
        finally:
            self._state = RunScopeState.CLOSED

    dispose = close

    def __enter__(self) -> "RunScope":
        self.activate()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _require(self, expected: RunScopeState) -> None:
        if self._state is not expected:
            raise RunScopeError(
                f"Expected run scope state {expected.value}, "
                f"current state is {self._state.value}."
            )
