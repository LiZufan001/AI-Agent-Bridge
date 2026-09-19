"""Typed seam for launching a process inside an already-prepared boundary.

This module does not create an AppContainer or alter Windows policy.  It
defines the narrow launch call that a later owner-installed helper-backed
launcher must satisfy.  The mature lifecycle continues to own the process
tree and Job Object; a contained launcher only replaces the process creation
operation.
"""

from __future__ import annotations

import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Mapping, Protocol

from .egress import ContainmentIdentity
from .models import RunIdentity


class ProcessBoundaryError(RuntimeError):
    """A process cannot be created within the required exact-run boundary."""


@dataclass(frozen=True, slots=True)
class ProcessBoundaryBinding:
    """The helper receipt facts a launcher must retain before process creation."""

    identity: RunIdentity
    containment: ContainmentIdentity
    resource_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RunIdentity):
            raise ProcessBoundaryError("process boundary identity is not typed")
        if not isinstance(self.containment, ContainmentIdentity):
            raise ProcessBoundaryError("process boundary containment is not typed")
        if self.containment != ContainmentIdentity.derive(self.identity):
            raise ProcessBoundaryError(
                "process boundary containment is not exact-run bound"
            )
        if (
            not isinstance(self.resource_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.resource_digest) is None
        ):
            raise ProcessBoundaryError("process boundary resource digest is invalid")


class ContainedProcessLauncher(Protocol):
    """Create the supplied lifecycle gate inside an already-prepared boundary."""

    @property
    def is_contained(self) -> bool:
        """Prove that this launcher is the owner-installed contained path."""

    def bind_containment(self, binding: ProcessBoundaryBinding) -> None:
        """Pin the exact helper-prepared identity before any process creation."""

    @property
    def bound_binding(self) -> ProcessBoundaryBinding | None:
        """Return the exact binding retained by this launcher."""

    def launch(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdin: IO[Any],
        stdout: IO[Any],
        stderr: IO[Any],
        options: Mapping[str, object],
    ) -> subprocess.Popen[Any]:
        """Return the exact root process, or raise before process creation."""


class SubprocessProcessLauncher:
    """Compatibility launcher used only when egress isolation is disabled."""

    is_contained = False

    def launch(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdin: IO[Any],
        stdout: IO[Any],
        stderr: IO[Any],
        options: Mapping[str, object],
    ) -> subprocess.Popen[Any]:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            shell=False,
            **dict(options),
        )


class UnavailableContainedProcessLauncher:
    """Fail-closed placeholder until the owner installs a real launcher."""

    is_contained = True

    def __init__(self) -> None:
        self._bound_binding: ProcessBoundaryBinding | None = None

    def bind_containment(self, binding: ProcessBoundaryBinding) -> None:
        if not isinstance(binding, ProcessBoundaryBinding):
            raise ProcessBoundaryError("process boundary binding is not typed")
        self._bound_binding = binding

    @property
    def bound_binding(self) -> ProcessBoundaryBinding | None:
        return self._bound_binding

    def launch(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdin: IO[Any],
        stdout: IO[Any],
        stderr: IO[Any],
        options: Mapping[str, object],
    ) -> subprocess.Popen[Any]:
        del command, cwd, stdin, stdout, stderr, options
        raise ProcessBoundaryError("contained process launcher is not installed")


__all__ = [
    "ContainedProcessLauncher",
    "ProcessBoundaryBinding",
    "ProcessBoundaryError",
    "SubprocessProcessLauncher",
    "UnavailableContainedProcessLauncher",
]
