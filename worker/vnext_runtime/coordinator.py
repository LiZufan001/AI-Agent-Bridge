"""Generic unattended Worker Coordinator boundary.

The Coordinator owns local scheduling and run-lifetime orchestration only.
Provider launch, Protocol-v2 CAS, reporting and recovery remain behind the
typed project-runner dependency and their existing authorities.
"""

from __future__ import annotations

import os
import platform
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .arbitration import HostResourceArbiter
from .adoption import AdmissionBarrier, AdmissionLease


class CoordinatorConfigurationError(ValueError):
    """Raised when local Coordinator configuration is unsafe."""


class ProjectRunner(Protocol):
    """One compatibility-safe per-project execution lifecycle."""

    def __call__(
        self,
        bridge_root: Path,
        project_id: str,
        project_config: Mapping[str, object],
        worker_config: Mapping[str, object],
        *,
        arbiter: HostResourceArbiter,
    ) -> bool: ...


class HealthSink(Protocol):
    """Receive non-authoritative local Coordinator health evidence."""

    def __call__(
        self,
        reservations: tuple[dict[str, object], ...],
        *,
        max_parallel_runs: int,
        lifecycle: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    """Validated local scheduling settings; Protocol v2 is unaffected."""

    max_parallel_runs: int = 1

    @classmethod
    def from_worker_config(cls, config: Mapping[str, object]) -> "CoordinatorConfig":
        runtime = config.get("runtime", {})
        runtime_map = runtime if isinstance(runtime, Mapping) else {}
        configured = config.get(
            "max_parallel_runs", runtime_map.get("max_parallel_runs", 1)
        )
        try:
            maximum = int(configured)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CoordinatorConfigurationError(
                "runtime.max_parallel_runs must be an integer >= 1"
            ) from exc
        if isinstance(configured, bool) or maximum < 1:
            raise CoordinatorConfigurationError(
                "runtime.max_parallel_runs must be an integer >= 1"
            )
        return cls(max_parallel_runs=maximum)


class Coordinator:
    """One local Coordinator for bounded independent project runs."""

    RUNNING = "RUNNING"
    DRAINING = "DRAINING"

    def __init__(
        self,
        bridge_root: Path,
        config: Mapping[str, object],
        *,
        runner: ProjectRunner,
        runtime_root: Path | None = None,
        health_sink: HealthSink | None = None,
    ) -> None:
        self.bridge_root = bridge_root
        self.settings = CoordinatorConfig.from_worker_config(config)
        self.runtime_root = (
            runtime_root
            if runtime_root is not None
            else bridge_root / "worker" / "runtime"
        ).resolve()
        self.arbiter = HostResourceArbiter(
            bridge_root,
            max_parallel_runs=self.settings.max_parallel_runs,
            registry_path=self.runtime_root / "resource-registry.json",
            lock_path=self.runtime_root / "resource-registry.lock",
            worker_host=platform.node() or "unknown",
            worker_pid=os.getpid(),
        )
        self._runner = runner
        self._health_sink = health_sink
        # This is a local admission barrier only.  It is deliberately separate
        # from Protocol-v2 state and is what lets an outer Launcher request a
        # drain without terminating active RunScopes.
        self.admission_barrier = AdmissionBarrier(
            max_active=self.settings.max_parallel_runs
        )
        self.lifecycle = self.RUNNING
        self._executor = ThreadPoolExecutor(
            max_workers=self.settings.max_parallel_runs,
            thread_name_prefix="bridge-run",
        )
        self._futures: dict[str, Future[bool]] = {}
        self._closed = False
        self._update_health()

    @property
    def max_parallel_runs(self) -> int:
        return self.settings.max_parallel_runs

    @property
    def active_run_count(self) -> int:
        return len(self.arbiter.active_reservations())

    def submit(
        self,
        project_id: str,
        project_config: Mapping[str, object],
        worker_config: Mapping[str, object],
    ) -> bool:
        """Submit one project while running; draining rejects admissions."""

        if self.lifecycle != self.RUNNING or project_id in self._futures:
            return False
        admission = self.admission_barrier.try_admit(project_id)
        if admission is None:
            return False
        try:
            self._futures[project_id] = self._executor.submit(
                self._run_admitted,
                admission,
                project_id,
                project_config,
                worker_config,
            )
        except BaseException:
            admission.close()
            raise
        self._update_health()
        return True

    def _run_admitted(
        self,
        admission: AdmissionLease,
        project_id: str,
        project_config: Mapping[str, object],
        worker_config: Mapping[str, object],
    ) -> bool:
        try:
            return self._runner(
                self.bridge_root,
                project_id,
                project_config,
                worker_config,
                arbiter=self.arbiter,
            )
        finally:
            # The exact admission token is released by the run that owns it;
            # disposal cannot clear another project's reservation.
            admission.close()
            self._update_health()

    def reap(self, *, wait: bool = False) -> bool:
        """Collect completed lifecycles without replaying them."""

        processed = False
        first_error: BaseException | None = None
        for project_id, future in list(self._futures.items()):
            if not wait and not future.done():
                continue
            try:
                processed = bool(future.result()) or processed
            except BaseException as exc:
                first_error = first_error or exc
            del self._futures[project_id]
        self._update_health()
        if first_error is not None:
            raise first_error
        return processed

    def begin_draining(self) -> bool:
        """Stop admissions and wait for every active run to finish."""

        self.lifecycle = self.DRAINING
        self._update_health()
        try:
            drained = self.admission_barrier.begin_draining()
            self.reap(wait=True)
        finally:
            self._update_health()
        return drained

    def close(self) -> None:
        if self._closed:
            return
        self.begin_draining()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.admission_barrier.close()
        self._closed = True

    def _update_health(self) -> None:
        if self._health_sink is not None:
            self._health_sink(
                self.arbiter.active_reservations(),
                max_parallel_runs=self.max_parallel_runs,
                lifecycle=self.lifecycle,
            )


__all__ = [
    "Coordinator",
    "CoordinatorConfig",
    "CoordinatorConfigurationError",
    "HealthSink",
    "ProjectRunner",
]
