#!/usr/bin/env python3
"""
AI-Agent-Bridge local worker (protocol v2).

The worker polls a GitHub-backed bridge repository for commands, claims exactly
one command with a generation-based lease, runs Codex once, and publishes the
result with compare-and-swap style checks. It never reruns Codex merely because
GitHub advanced while Codex was working.

Standard library only.
"""

from __future__ import annotations

import state_roots

import argparse
import hashlib
import inspect
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import urlopen

from codex_lifecycle import (
    MARKER_CAPTURE_FAILED,
    CodexRunResult,
    MarkerParser,
)
import worker_health
import console_observation
import owner_console_control
import worker_execution_control
import recovery_journal
import bridge_alerts
import bridge_common
import executor
import git_store
import protocol_core
import pending_report
import report_builder
import phased_task
import self_maintenance
from supervisor_publication_gateway import SupervisorPublicationGateway
from vnext_runtime.models import ExecutionProfile, ExecutionRequest, RunIdentity
from vnext_runtime.egress_runtime import (
    EgressPreflight,
    EgressRuntimeError,
    EgressRuntimeState,
    RunEgressResources,
    preclaim_egress,
)
from vnext_runtime.recovery_evidence import WalEventKind
from vnext_runtime.recovery_wal import DurableRunWal, DurableWalError
from vnext_runtime.git_effects import WalPushEffectRecorder
from vnext_runtime.providers.executors.codex import (
    CodexExecutorProvider,
    CodexProviderSettings,
)
from vnext_runtime.run_scope import RunScope, RunScopeState
from vnext_runtime.arbitration import HostResourceArbiter, ResourceReservation, git_target_identity
from vnext_runtime.guards.network import (
    GuardVerdict,
    NetworkGuard,
    network_guard_settings as _network_guard_settings,
    parse_probe_country,
)
from vnext_runtime.services.recovery import (
    RecoveryCoordinator,
    collect_recovery_evidence as _collect_recovery_evidence,
)
from vnext_runtime.coordinator import Coordinator, CoordinatorConfigurationError

PENDING_STATES = protocol_core.PENDING_STATES
FULL_ACCESS_FLAG = executor.FULL_ACCESS_FLAG
CONFLICTING_CODEX_FLAGS = executor.CONFLICTING_CODEX_FLAGS
COMMAND_META_RE = protocol_core.COMMAND_META_RE
FINAL_RESULT_PREFIX = protocol_core.FINAL_RESULT_PREFIX
EXECUTOR_FIELDS = protocol_core.EXECUTOR_FIELDS
SUPPORTED_REASONING_EFFORTS = protocol_core.SUPPORTED_REASONING_EFFORTS
MODEL_VALUE_RE = protocol_core.MODEL_VALUE_RE
EXECUTOR_CONFIG_KEYS = executor.EXECUTOR_CONFIG_KEYS


# These names remain available to existing Worker callers while the shared
# definitions live in the small dependency-free support module.
WorkerError = bridge_common.WorkerError
CASConflict = bridge_common.CASConflict
now_dt = bridge_common.now_dt
now_iso = bridge_common.now_iso
load_json = bridge_common.load_json
json_text = bridge_common.json_text
write_text_atomic = bridge_common.write_text_atomic
save_pending_report = bridge_common.save_pending_report
ensure_tool = bridge_common.ensure_tool


NetworkGuardResult = GuardVerdict


class NetworkGuardInterruption(WorkerError):
    """Codex was stopped because the configured safe egress disappeared."""

    def __init__(
        self,
        reason: str,
        *,
        stdout: str = "",
        stderr: str = "",
        run_result: CodexRunResult | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stdout = stdout
        self.stderr = stderr
        self.run_result = run_result


WORKER_RESTART_CODE = 75


class PostRunHandoffRestartRequested(WorkerError):
    """A settled Worker-owned run has an exact fresh handoff to yield."""


@dataclass(frozen=True, slots=True)
class WorkerRunSettlement:
    """In-memory identity passed across the post-run Worker boundary."""

    project_id: str
    command_id: int
    run_id: str
    claim_generation: int
    outcome: str
    final_state: dict[str, Any]


class WorkerInstanceLock:
    """Keep overlapping worker processes on this machine from claiming work."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "WorkerInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.handle.close()
            self.handle = None
            raise WorkerError(
                f"Another bridge worker instance already holds the lock: {self.path}"
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _record_worker_health(
    bridge_root: Path,
    *,
    runtime_root: Path | None = None,
    **updates: Any,
) -> None:
    """Health evidence is best effort and must never break protocol processing."""
    try:
        worker_health.update_worker_health(
            bridge_root,
            runtime_root=runtime_root,
            **updates,
        )
    except Exception:
        return None


LEGACY_MAX_PARALLEL_RUNS = 1


def record_idle_run_health(bridge_root: Path, *, runtime_root: Path | None = None) -> None:
    """Publish the serial Worker idle projection used by the outer Launcher."""

    _record_worker_health(
        bridge_root,
        runtime_root=runtime_root,
        max_parallel_runs=LEGACY_MAX_PARALLEL_RUNS,
        active_run_count=0,
        active_runs=[],
    )


def record_active_run_health(
    bridge_root: Path,
    *,
    project_id: str,
    command_id: int,
    run_id: str,
    claim_generation: int,
    claimed_state: dict[str, Any],
    executor_profile: dict[str, str | None],
) -> None:
    """Publish one exact claimed run in the legacy serial health projection."""

    active = claimed_state.get("active_run")
    active_record = active if isinstance(active, dict) else {}
    claimed_at = active_record.get("claimed_at") or now_iso()
    _record_worker_health(
        bridge_root,
        max_parallel_runs=LEGACY_MAX_PARALLEL_RUNS,
        active_run_count=1,
        active_runs=[
            {
                "project_id": project_id,
                "run_id": run_id,
                "command_id": command_id,
                "claimed_generation": claim_generation,
                "provider": "codex",
                "model": executor_profile.get("model"),
                "claimed_at": claimed_at,
                "started_at": claimed_at,
                "lease_expires_at": active_record.get("lease_expires_at"),
            }
        ],
    )


def refresh_active_run_health(bridge_root: Path, *, runtime_root: Path | None = None) -> None:
    """Reconcile local run evidence with canonical Protocol-v2 state."""

    records: list[dict[str, Any]] = []
    state_root = bridge_root / "projects"
    try:
        if not state_root.is_dir():
            _record_worker_health(
                bridge_root,
                runtime_root=runtime_root,
                max_parallel_runs=LEGACY_MAX_PARALLEL_RUNS,
                active_run_count=1,
                active_runs=[{}],
            )
            return
        state_paths = sorted(state_root.glob("*/state.json"))
    except OSError:
        _record_worker_health(
            bridge_root,
            runtime_root=runtime_root,
            max_parallel_runs=LEGACY_MAX_PARALLEL_RUNS,
            active_run_count=1,
            active_runs=[{}],
        )
        return
    if not state_paths:
        _record_worker_health(
            bridge_root,
            runtime_root=runtime_root,
            max_parallel_runs=LEGACY_MAX_PARALLEL_RUNS,
            active_run_count=1,
            active_runs=[{}],
        )
        return
    for state_path in state_paths:
        try:
            state = load_json(state_path)
        except Exception:
            records.append({})
            continue
        if state.get("status") != "CODEX_RUNNING":
            continue
        active = state.get("active_run")
        if not isinstance(active, dict):
            records.append({})
            continue
        project_id = state.get("project_id") or state_path.parent.name
        run_id = active.get("run_id")
        command_id = active.get("command_id", state.get("latest_command"))
        claim_generation = active.get(
            "claimed_generation", state.get("generation")
        )
        claimed_at = active.get("claimed_at")
        if (
            not isinstance(project_id, str)
            or not project_id
            or not isinstance(run_id, str)
            or not run_id
            or isinstance(command_id, bool)
            or not isinstance(command_id, int)
            or isinstance(claim_generation, bool)
            or not isinstance(claim_generation, int)
            or not isinstance(claimed_at, str)
            or not claimed_at
        ):
            records.append({})
            continue
        executor_profile = active.get("executor")
        profile = executor_profile if isinstance(executor_profile, dict) else {}
        records.append(
            {
                "project_id": project_id,
                "run_id": run_id,
                "command_id": command_id,
                "claimed_generation": claim_generation,
                "provider": "codex",
                "model": profile.get("model"),
                "claimed_at": claimed_at,
                "started_at": claimed_at,
                "lease_expires_at": active.get("lease_expires_at"),
            }
        )
    records = records[:128]
    if not records:
        record_idle_run_health(bridge_root, runtime_root=runtime_root)
        return
    _record_worker_health(
        bridge_root,
        runtime_root=runtime_root,
        max_parallel_runs=LEGACY_MAX_PARALLEL_RUNS,
        active_run_count=len(records),
        active_runs=records,
    )


def _record_coordinator_health(
    bridge_root: Path,
    runtime_root: Path,
    reservations: tuple[dict[str, object], ...],
    *,
    max_parallel_runs: int,
    lifecycle: str,
) -> None:
    """Project generic Coordinator facts into the legacy Worker health schema."""

    active = [
        {
            "project_id": record.get("project_id"),
            "run_id": record.get("run_id"),
            "provider": "codex",
            "start_time": record.get("acquired_at"),
        }
        for record in reservations[:max_parallel_runs]
    ]
    _record_worker_health(
        bridge_root,
        runtime_root=runtime_root,
        max_parallel_runs=max_parallel_runs,
        active_run_count=len(active),
        active_runs=active,
        coordinator_lifecycle=lifecycle,
    )


class WorkerCoordinator(Coordinator):
    """Compatibility shell for the generic vNext Coordinator."""

    def __init__(
        self,
        bridge_root: Path,
        config: Mapping[str, Any],
        *,
        runtime_root: Path | None = None,
        on_run_settled: Callable[[WorkerRunSettlement], None] | None = None,
    ) -> None:
        self._on_run_settled = on_run_settled
        try:
            super().__init__(
                bridge_root,
                config,
                runner=self._run_project,
                runtime_root=runtime_root,
                health_sink=lambda reservations, **kwargs: _record_coordinator_health(
                    bridge_root,
                    self.runtime_root,
                    reservations,
                    **kwargs,
                ),
            )
        except CoordinatorConfigurationError as exc:
            raise WorkerError(str(exc)) from exc

    def _run_project(
        self,
        bridge_root: Path,
        project_id: str,
        project_config: Mapping[str, object],
        worker_config: Mapping[str, object],
        *,
        arbiter: HostResourceArbiter,
    ) -> bool:
        process_kwargs: dict[str, object] = {}
        # Preserve the historical patchable test/integration seam for callers
        # that still provide the pre-Coordinator runner signature.
        try:
            if "arbiter" in inspect.signature(process_project).parameters:
                process_kwargs["arbiter"] = arbiter
        except (TypeError, ValueError):
            process_kwargs["arbiter"] = arbiter
        if self._on_run_settled is not None:
            # The settlement callback is the Worker-owned lifecycle seam.  A
            # patched runner may hide its signature, but must still receive
            # the callback whenever the Coordinator was constructed with one.
            process_kwargs["on_run_settled"] = self._on_run_settled
        return process_project(
            bridge_root,
            project_id,
            dict(project_config),
            dict(worker_config),
            **process_kwargs,
        )


def _record_preclaim_failure(
    bridge_root: Path,
    *,
    project_id: str,
    command_id: int,
    state: str,
    error: BaseException,
) -> None:
    """Record bounded context for deterministic validation failures before CAS."""

    _record_worker_health(
        bridge_root,
        last_failure_kind=type(error).__name__,
        last_failure_at=worker_health.now_iso(),
        last_failure_stage="pre_claim_validation",
        last_failure_project=project_id,
        last_failure_command_id=command_id,
        last_failure_state=state,
    )


def emit_bridge_alert(
    bridge_root: Path,
    config: dict[str, Any] | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Send one best-effort alert without allowing it to affect the Worker."""

    try:
        result = bridge_alerts.emit_alert(bridge_root, config, context)
    except Exception as exc:
        # The alert module already isolates its own failures.  Keep this second
        # boundary because alerting is explicitly ancillary to canonical state.
        result = {
            "status": "failed",
            "identity": None,
            "attempted": False,
            "error_kind": type(exc).__name__,
        }
    _record_worker_health(
        bridge_root,
        last_alert_at=now_iso(),
        last_alert_kind=str(context.get("alert_kind", "unknown")),
        last_alert_identity=result.get("identity"),
        last_alert_send_status=result.get("status", "failed"),
        last_alert_error=result.get("error_kind"),
    )
    print(
        f"[{now_iso()}] alert kind={context.get('alert_kind', 'unknown')} "
        f"status={result.get('status', 'failed')}"
    )
    return result


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


network_guard_settings = _network_guard_settings


def network_guard_check(config: dict[str, Any]) -> NetworkGuardResult:
    """Compatibility adapter for the extracted network guard capability."""

    # Keep the historical patchable opener seam for existing Worker tests and
    # local integrations while policy ownership remains in NetworkGuard.
    guard = NetworkGuard(config, opener=urlopen)
    try:
        return guard.preflight()
    finally:
        guard.dispose()


def run_process(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    timeout: int | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Compatibility wrapper for the shared local process runner."""

    return git_store.run_process(
        args,
        cwd=cwd,
        input_text=input_text,
        timeout=timeout,
        check=check,
    )


def git(
    bridge_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Compatibility wrapper for the canonical Git store."""

    return git_store.git(bridge_root, *args, check=check)


def resolve_path(value: str, bridge_root: Path) -> Path:
    if value == "__BRIDGE_ROOT__":
        return bridge_root
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    if not p.is_absolute():
        p = (bridge_root / p).resolve()
    return p


def command_argv(command: str) -> list[str]:
    """Compatibility wrapper for the executor command resolver."""

    return executor.command_argv(command)


def codex_args_from_config(config: dict[str, Any]) -> list[str]:
    """Compatibility wrapper for unattended full-access argv validation."""

    return executor.codex_args_from_config(config)


def command_executor_override(meta: dict[str, Any]) -> dict[str, str] | None:
    """Compatibility wrapper for the shared protocol executor validation."""

    try:
        return protocol_core.command_executor_override(meta)
    except protocol_core.ProtocolViolation as exc:
        raise WorkerError(str(exc)) from exc


def _config_assignment(argument: str) -> tuple[str, str] | None:
    """Compatibility wrapper for Codex ``-c key=value`` parsing."""

    return executor._config_assignment(argument)


def executor_profile_from_args(
    codex_args: list[str],
    *,
    source: str,
) -> dict[str, str | None]:
    """Compatibility wrapper for executor profile construction."""

    return executor.executor_profile_from_args(codex_args, source=source)


def effective_codex_args(
    default_args: list[str],
    meta: dict[str, Any],
) -> tuple[list[str], dict[str, str | None]]:
    """Validate Protocol override, then delegate run-local argv rewriting."""

    override = command_executor_override(meta)
    return executor.effective_codex_args(default_args, override)


def get_head(repo: Path) -> str | None:
    return git_store.get_head(repo)


def current_branch(bridge_root: Path) -> str:
    return git_store.current_branch(bridge_root)


def tracked_dirty(bridge_root: Path) -> bool:
    return git_store.tracked_dirty(bridge_root)


def sync_to_remote(bridge_root: Path, *, discard_worker_commit: bool = False) -> None:
    return git_store.sync_to_remote(
        bridge_root,
        discard_worker_commit=discard_worker_commit,
    )


def relative_path(path: Path, bridge_root: Path) -> str:
    return git_store.relative_path(path, bridge_root)


def commit_payloads(
    bridge_root: Path,
    payloads: dict[Path, str | bytes],
    message: str,
) -> bool:
    return git_store.commit_payloads(bridge_root, payloads, message)


def publish_cas(
    *,
    bridge_root: Path,
    state_path: Path,
    expected: Callable[[dict[str, Any]], bool],
    already_applied: Callable[[dict[str, Any]], bool],
    payload_builder: Callable[[dict[str, Any]], dict[Path, str | bytes]],
    message: str,
    retries: int = 5,
    push_effect_recorder: object | None = None,
) -> dict[str, Any]:
    return git_store.publish_cas(
        bridge_root=bridge_root,
        state_path=state_path,
        expected=expected,
        already_applied=already_applied,
        payload_builder=payload_builder,
        message=message,
        retries=retries,
        push_effect_recorder=push_effect_recorder,
    )


def command_metadata(command_text: str) -> dict[str, Any]:
    """Compatibility wrapper for the shared Protocol-v2 metadata parser."""

    try:
        return protocol_core.parse_command_metadata(command_text)
    except protocol_core.ProtocolViolation as exc:
        raise WorkerError(str(exc)) from exc


def validate_command(
    *,
    state: dict[str, Any],
    command_id: int,
    meta: dict[str, Any],
) -> None:
    """Compatibility wrapper preserving WorkerError/CASConflict boundaries."""

    try:
        protocol_core.validate_pending_command(
            state=state,
            command_id=command_id,
            meta=meta,
        )
    except protocol_core.ProtocolConflict as exc:
        raise CASConflict(str(exc)) from exc
    except protocol_core.ProtocolViolation as exc:
        raise WorkerError(str(exc)) from exc


def build_prompt(project_id: str, mission: str, command: str) -> str:
    """Compatibility wrapper for ordinary prompt construction."""

    return executor.build_prompt(project_id, mission, command)


def build_phased_prompt(
    project_id: str,
    mission: str,
    runtime: phased_task.RuntimeInspection,
    *,
    bridge_root: Path | None = None,
) -> str:
    """Compatibility wrapper preserving the dynamic hardening hook."""

    return executor.build_phased_prompt(
        project_id,
        mission,
        runtime,
        bridge_root=bridge_root,
        base_prompt_builder=build_prompt,
        # Preserve the historical helper path in the phased bootstrap while
        # the prompt implementation lives in executor.py.
        helper_path=Path(__file__).resolve(),
    )


def _codex_run_legacy(
    *,
    codex_argv: list[str],
    codex_args: list[str],
    workdir: Path,
    output_file: Path,
    prompt: str,
    timeout_seconds: int,
    network_config: dict[str, Any] | None = None,
    stdout_log_path: Path | None = None,
    stderr_log_path: Path | None = None,
    final_grace_timeout_seconds: float = 20,
    cleanup_timeout_seconds: float = 10,
    marker_stable_seconds: float = 0.5,
    poll_interval_seconds: float = 0.1,
    max_log_bytes: int = 10 * 1024 * 1024,
    max_final_message_bytes: int = 2 * 1024 * 1024,
    marker_parser: MarkerParser | None = None,
    event_logger: Callable[[str, dict[str, Any]], None] | None = None,
    network_guard: NetworkGuard | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> CodexRunResult:
    """Compatibility wrapper for the existing executor lifecycle seam."""

    guard_enabled = network_guard_settings(network_config or {}) is not None
    owned_guard = False
    if guard_enabled and network_guard is None:
        # The no-scope compatibility form retains the historical patchable
        # network_guard_check seam.  Scoped production runs supply their
        # RunScope-owned guard below.
        network_guard = NetworkGuard(
            network_config or {},
            probe_check=lambda: network_guard_check(network_config or {}),
            event_logger=event_logger,
        )
        owned_guard = True

    guard_check = network_guard.watchdog_callback() if guard_enabled and network_guard else None
    watchdog_interval = (
        network_guard.watchdog_interval_seconds
        if guard_enabled and network_guard
        else 10.0
    )
    try:
        result = executor.run_codex(
            codex_argv=codex_argv,
            codex_args=codex_args,
            workdir=workdir,
            output_file=output_file,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            final_grace_timeout_seconds=final_grace_timeout_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
            marker_stable_seconds=marker_stable_seconds,
            poll_interval_seconds=poll_interval_seconds,
            max_log_bytes=max_log_bytes,
            max_final_message_bytes=max_final_message_bytes,
            marker_parser=marker_parser,
            guard_check=guard_check,
            stop_check=stop_check,
            guard_interval_seconds=watchdog_interval,
            event_logger=event_logger,
        )
    finally:
        if owned_guard and network_guard is not None:
            network_guard.dispose()
    if result.network_interrupted:
        raise NetworkGuardInterruption(
            result.runtime_error or "network guard became unsafe",
            stdout=result.stdout_tail,
            stderr=result.stderr_tail,
            run_result=result,
        )
    return result


def codex_run(
    *,
    run_scope: RunScope | None = None,
    execution_request: ExecutionRequest | None = None,
    **kwargs: Any,
) -> CodexRunResult:
    """Run Codex through the vNext provider when a scope is supplied.

    The no-scope form is retained for existing callers and tests.  The scoped
    form keeps the mature Worker lifecycle as the provider runner, so marker,
    timeout, process-tree and network-guard behavior remain unchanged.
    """

    if (run_scope is None) != (execution_request is None):
        raise WorkerError(
            "run_scope and execution_request must be supplied together."
        )
    if run_scope is None:
        return _codex_run_legacy(**kwargs)

    required = {
        "codex_argv",
        "codex_args",
        "workdir",
        "output_file",
        "prompt",
        "timeout_seconds",
    }
    missing = sorted(required.difference(kwargs))
    if missing:
        raise WorkerError(
            "Scoped Codex invocation is missing arguments: " + ", ".join(missing)
        )
    codex_argv = kwargs["codex_argv"]
    if not isinstance(codex_argv, list) or not codex_argv:
        raise WorkerError("codex_argv must be a non-empty list.")

    holder: dict[str, CodexRunResult] = {}
    runtime_guard: NetworkGuard | None = None
    network_config = kwargs.get("network_config")
    if isinstance(network_config, Mapping) and network_guard_settings(network_config) is not None:
        runtime_guard = NetworkGuard(
            network_config,
            event_logger=kwargs.get("event_logger"),
        )
        run_scope.own_guard(runtime_guard)

    def run_once(**runner_kwargs: Any) -> CodexRunResult:
        if kwargs.get("stop_check") is not None:
            runner_kwargs["stop_check"] = kwargs["stop_check"]
        if runtime_guard is not None:
            runner_kwargs["network_guard"] = runtime_guard
        result = _codex_run_legacy(**runner_kwargs)
        holder["result"] = result
        return result

    settings = CodexProviderSettings(
        codex_command=str(codex_argv[0]),
        codex_execution_mode="full_access",
        codex_args=tuple(kwargs["codex_args"]),
        output_file=kwargs["output_file"],
        codex_argv=tuple(codex_argv),
        network_config=kwargs.get("network_config"),
        timeout_seconds=kwargs["timeout_seconds"],
        final_grace_timeout_seconds=kwargs.get("final_grace_timeout_seconds", 20),
        cleanup_timeout_seconds=kwargs.get("cleanup_timeout_seconds", 10),
        marker_stable_seconds=kwargs.get("marker_stable_seconds", 0.5),
        poll_interval_seconds=kwargs.get("poll_interval_seconds", 0.1),
        max_log_bytes=kwargs.get("max_log_bytes", 10 * 1024 * 1024),
        max_final_message_bytes=kwargs.get(
            "max_final_message_bytes", 2 * 1024 * 1024
        ),
        marker_parser=kwargs.get("marker_parser"),
        event_logger=kwargs.get("event_logger"),
        process_launcher=kwargs.get("process_launcher"),
    )
    provider = CodexExecutorProvider(
        settings,
        runner=run_once,
        prompt_builder=lambda _request: kwargs["prompt"],
    )
    provider.execute(execution_request, run_scope)
    try:
        return holder["result"]
    except KeyError as exc:
        raise WorkerError("Codex provider returned without a lifecycle result.") from exc


def parse_final_result(final_message: str) -> dict[str, Any] | None:
    return protocol_core.parse_final_result(final_message)


def maybe_final_report_ready(
    previous_status: str,
    outcome: str,
    final_message: str,
) -> bool:
    return protocol_core.maybe_final_report_ready(
        previous_status,
        outcome,
        final_message,
    )


def report_markdown(
    *,
    project_id: str,
    command_id: int,
    outcome: str,
    exit_code: int,
    workdir: Path,
    head_before: str | None,
    head_after: str | None,
    final_message: str,
    stderr: str,
    meta: dict[str, Any],
    run_id: str,
    claim_generation: int,
    executor_profile: dict[str, str | None],
    run_result: CodexRunResult | None = None,
    phased_runtime: phased_task.RuntimeInspection | None = None,
    phase_contract_error: str | None = None,
    self_maintenance_preflight: self_maintenance.SelfMaintenancePreflight | None = None,
    self_maintenance_guard: self_maintenance.SelfMaintenanceGuard | None = None,
) -> str:
    report = report_builder.build_worker_report(
        project_id=project_id,
        command_id=command_id,
        outcome=outcome,
        exit_code=exit_code,
        workdir=workdir,
        head_before=head_before,
        head_after=head_after,
        final_message=final_message,
        stderr=stderr,
        meta=meta,
        run_id=run_id,
        claim_generation=claim_generation,
        executor_profile=executor_profile,
        completed_at=now_iso(),
        worker_host=platform.node() or "unknown",
        run_result=run_result,
    )
    if (
        phased_runtime is None
        and phase_contract_error is None
        and self_maintenance_preflight is None
        and self_maintenance_guard is None
    ):
        return report

    if phased_runtime is not None or phase_contract_error is not None:
        lines = [report.rstrip(), "", "## Phased task runtime", ""]
    else:
        lines = [report.rstrip()]
    if phased_runtime is not None:
        progress = phased_runtime.progress
        lines.extend(
            [
                f"- phases: {', '.join(progress['phases'])}",
                f"- completed_phases: {', '.join(progress['completed_phases']) or 'none'}",
                f"- current_phase: {progress['current_phase']}",
                (
                    "- final_acceptance_verified: "
                    f"{str(progress['final_acceptance_verified']).lower()}"
                ),
            ]
        )
    if phase_contract_error is not None:
        lines.append(f"- phase_contract: {phase_contract_error}")
    elif phased_runtime is not None:
        lines.append("- phase_contract: runtime projection verified")
    if self_maintenance_preflight is not None or self_maintenance_guard is not None:
        lines.extend(["", "## Self-maintenance guard", ""])
        if self_maintenance_preflight is not None:
            lines.extend(
                [
                    "- preflight: accepted",
                    f"- live_branch: {self_maintenance_preflight.live.branch}",
                    f"- live_head: {self_maintenance_preflight.live.head}",
                    f"- candidate_branch: {self_maintenance_preflight.candidate.branch}",
                    f"- candidate_head_before: {self_maintenance_preflight.candidate.head}",
                    f"- bootstrap_base: {self_maintenance_preflight.config.bootstrap_base}",
                ]
            )
        if self_maintenance_guard is not None:
            lines.append(
                f"- post_run_guard: {self_maintenance_guard.reason}"
            )
            if self_maintenance_guard.violations:
                lines.append(
                    f"- protected_path_violations: {self_maintenance_guard.violations}"
                )
    return "\n".join(lines).rstrip() + "\n"


def redact_diagnostics(text: str) -> str:
    """Compatibility wrapper for the shared report redaction implementation."""

    return report_builder.redact_diagnostics(text)


def execution_outcome(run_result: CodexRunResult | None) -> str:
    """Return the strict business result; process exit is runtime diagnostics."""
    if run_result is not None and run_result.termination_reason == "owner_stop":
        return "FAILED"
    marker = (run_result.marker if run_result else None) or {}
    status = str(marker.get("status", ""))
    return status if status in {"SUCCESS", "FAILED", "BLOCKED"} else "FAILED"


def intended_report_status(
    *,
    previous_status: str,
    kind: str,
    outcome: str,
    final_message: str,
    run_result: CodexRunResult | None,
) -> str:
    """Return the status the completed Worker run already decided to publish."""

    if (
        run_result is not None
        and run_result.marker_result == MARKER_CAPTURE_FAILED
    ):
        return "RECOVERY_REQUIRED"
    if run_result is not None and run_result.termination_reason == "owner_stop" and run_result.cleanup_error:
        return "RECOVERY_REQUIRED"
    if maybe_final_report_ready(previous_status, outcome, final_message):
        return "FINAL_REPORT_READY"
    return "REPORT_READY"


def intended_execution_error(
    *,
    outcome: str,
    run_result: CodexRunResult | None,
    exit_code: int,
) -> str | None:
    """Return the bounded error snapshot used by normal report publication."""

    if run_result is not None and run_result.termination_reason == "owner_stop":
        return "OWNER_STOP: exact run stopped; inspect partial effects before new work"
    if outcome == "SUCCESS":
        return None
    if run_result is not None and run_result.marker_result == MARKER_CAPTURE_FAILED:
        value = run_result.contract_error or (
            "MARKER_CAPTURE_FAILED: independent final result could not be read"
        )
    elif run_result is not None and run_result.timed_out:
        value = f"Codex execution timeout; process exit {exit_code}"
    elif run_result is not None and run_result.marker is not None:
        value = f"Codex marker {outcome}; process exit {exit_code}"
    elif run_result is not None and run_result.contract_error:
        value = f"{run_result.contract_error} Process exit {exit_code}"
    else:
        value = f"PROCESS_EXITED_WITHOUT_MARKER; process exit {exit_code}"
    return pending_report.safe_execution_error(value)


def _phase_diagnostic(prefix: str, detail: object) -> str:
    """Return a bounded, redacted diagnostic for phased evidence failures."""

    safe = pending_report.safe_execution_error(f"{prefix}: {detail}")
    return safe or prefix


def _protected_run_ids(runs_dir: Path) -> set[str]:
    """Keep runs referenced by unresolved local publication/recovery evidence."""

    runtime_dir = runs_dir.parent
    protected: set[str] = set()
    evidence_paths = list(runtime_dir.glob("pending-report-*.meta.json"))
    evidence_paths.extend((runtime_dir / "recovery").glob("*.json"))
    for path in evidence_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not phased_task.RUN_ID_RE.fullmatch(run_id):
            continue
        if (
            data.get("journal_status") == "pending"
            and data.get("remote_publish_pending") is True
        ):
            protected.add(run_id)

    recovery_dir = runtime_dir / "recovery"
    for path in recovery_dir.glob("*.json"):
        if phased_task.RUN_ID_RE.fullmatch(path.stem):
            try:
                recovery_journal.read_journal(path)
            except recovery_journal.RecoveryJournalError:
                # An unreadable run-specific journal represents unresolved
                # side-effect uncertainty; retain its run evidence.
                protected.add(path.stem)
    return protected


def _phased_compaction_gates(
    bridge_root: Path,
    project_id: str,
    command_id: int,
    run_id: str,
) -> tuple[bool, bool, bool]:
    """Inspect local publication/recovery evidence before deleting projections."""

    pending = False
    recovery_unresolved = False
    conflict = False
    metadata_path = pending_report.canonical_metadata_path(
        bridge_root, project_id, command_id
    )
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recovery_unresolved = True
        else:
            if isinstance(metadata, dict) and metadata.get("run_id") == run_id:
                status = metadata.get("journal_status")
                remote_pending = metadata.get("remote_publish_pending") is True
                if status == "pending" and remote_pending:
                    pending = True
                elif status == "conflict":
                    conflict = True
                elif status not in {"reconciled", "superseded"}:
                    recovery_unresolved = True

    journal_path = recovery_journal.journal_path(bridge_root, project_id, run_id)
    if journal_path.exists():
        try:
            journal = recovery_journal.read_journal(journal_path)
        except recovery_journal.RecoveryJournalError:
            recovery_unresolved = True
        else:
            status = journal.get("journal_status")
            remote_pending = journal.get("remote_publish_pending") is True
            if status == "pending" and remote_pending:
                pending = True
                recovery_unresolved = True
            elif status == "conflict":
                conflict = True
            elif status not in {"reconciled", "superseded"}:
                recovery_unresolved = True
    return pending, recovery_unresolved, conflict


def prune_run_directories(runs_dir: Path, current_run_dir: Path, keep: int) -> None:
    """Bound per-run diagnostics while retaining unresolved evidence runs."""
    if keep < 1 or not runs_dir.is_dir():
        return
    protected = _protected_run_ids(runs_dir)
    candidates = sorted(
        (
            path
            for path in runs_dir.iterdir()
            if (
                path.is_dir()
                and path.name.startswith("run-")
                and path != current_run_dir
                and path.name not in protected
            )
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in candidates[max(0, keep - 1) :]:
        shutil.rmtree(stale, ignore_errors=True)


def same_ready_snapshot(
    current: dict[str, Any],
    snapshot: dict[str, Any],
) -> bool:
    return protocol_core.same_ready_snapshot(current, snapshot)


def lease_expired(state: dict[str, Any]) -> bool:
    active = state.get("active_run")
    if not isinstance(active, dict):
        return False
    value = active.get("lease_expires_at")
    if not value:
        return False
    try:
        return parse_iso(str(value)) <= now_dt()
    except ValueError:
        return True


def collect_recovery_evidence(
    *,
    workdir: Path,
    head_before: str | None,
    head_after: str | None,
) -> dict[str, Any]:
    """Compatibility entry point for the typed RecoveryCoordinator service."""
    return _collect_recovery_evidence(
        workdir=workdir,
        head_before=head_before,
        head_after=head_after,
    )


def maybe_mark_expired_lease(
    bridge_root: Path,
    project_id: str,
    state_path: Path,
    config: dict[str, Any] | None = None,
) -> bool:
    """Compatibility entry point for lease recovery orchestration."""
    return RecoveryCoordinator(
        bridge_root,
        publish_cas=publish_cas,
        emit_alert=emit_bridge_alert,
        config=config,
    ).mark_expired_lease(
        project_id,
        state_path,
        config=config,
        lease_expired=lease_expired,
    )


def _evidence_path(path: Path, bridge_root: Path) -> str:
    try:
        return relative_path(path, bridge_root)
    except ValueError:
        return str(path)


def publish_network_recovery(
    *,
    bridge_root: Path,
    state_path: Path,
    project_id: str,
    command_id: int,
    run_id: str,
    claim_generation: int,
    reason: str,
    report_text: str,
    runtime_dir: Path,
    workdir: Path | None = None,
    head_before: str | None = None,
    head_after: str | None = None,
    claimed_at: str | None = None,
    lease_expires_at: str | None = None,
    report_path: Path | None = None,
    run_result: CodexRunResult | None = None,
    interrupted_at: str | None = None,
) -> dict[str, Any]:
    """Compatibility entry point for interrupted-run recovery."""
    return RecoveryCoordinator(
        bridge_root,
        publish_cas=publish_cas,
        emit_alert=emit_bridge_alert,
        config=None,
        save_pending_report=save_pending_report,
        collect_evidence=collect_recovery_evidence,
    ).publish_network_recovery(
        state_path=state_path,
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        claim_generation=claim_generation,
        reason=reason,
        report_text=report_text,
        runtime_dir=runtime_dir,
        workdir=workdir,
        head_before=head_before,
        head_after=head_after,
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
        report_path=report_path,
        run_result=run_result,
        interrupted_at=interrupted_at,
    )


def _state_int(value: Any, default: int | None = -1) -> int | None:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _state_for_alert(state_path: Path) -> dict[str, Any]:
    try:
        return load_json(state_path)
    except Exception:
        return {}


def _network_alert_context(
    *,
    bridge_root: Path,
    state_path: Path,
    project_id: str,
    command_id: int,
    run_id: str,
    claim_generation: int,
    reason: str,
    claimed_state: dict[str, Any],
    report_path: Path,
    runtime_dir: Path,
    run_result: CodexRunResult | None,
    recovery_result: dict[str, Any] | None,
    remote_cas_success: bool,
    remote_publish_pending: bool | None,
) -> dict[str, Any]:
    journal_path = recovery_journal.journal_path(bridge_root, project_id, run_id)
    try:
        journal = recovery_journal.read_journal(journal_path)
    except recovery_journal.RecoveryJournalError:
        journal = {}
    if recovery_result and isinstance(recovery_result.get("state"), dict):
        current_state = recovery_result["state"]
    else:
        current_state = _state_for_alert(state_path)
    pending_report_path = runtime_dir / f"pending-report-{command_id:03d}.md"
    active = claimed_state.get("active_run") or {}
    return {
        "timestamp": now_iso(),
        "project_id": project_id,
        "command_id": command_id,
        "run_id": run_id,
        "alert_kind": "network_guard_interruption",
        "current_bridge_status": str(current_state.get("status", "unknown")),
        "original_status": "CODEX_RUNNING",
        "generation": _state_int(current_state.get("generation"), claim_generation),
        "generation_before": claim_generation,
        "claim_generation": claim_generation,
        "interruption_recovery_type": "network guard interruption",
        "recovery_reason_safe": str(
            journal.get("interruption_reason_safe", reason)
        ),
        "codex_terminated": (
            bool(run_result and run_result.process_exited_at)
            if run_result is not None
            else None
        ),
        "worker_is_alive": True,
        "journal_saved": journal_path.exists(),
        "pending_report_saved": pending_report_path.exists(),
        "remote_recovery_cas_success": remote_cas_success,
        "remote_publish_pending": (
            journal.get("remote_publish_pending")
            if journal
            else remote_publish_pending
        ),
        "worktree_dirty": journal.get("worktree_dirty"),
        "local_commit_created": journal.get("local_commit_created"),
        "unpushed_commits_present": journal.get("unpushed_commits_present"),
        "external_side_effects_unknown": journal.get(
            "external_side_effects_unknown", True
        ),
        "lease_expires_at": active.get("lease_expires_at"),
        "claimed_at": active.get("claimed_at"),
    }


def reconcile_pending_recoveries(
    bridge_root: Path,
    *,
    project_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> int:
    """Reconcile durable local journals before normal command processing."""
    return RecoveryCoordinator(
        bridge_root,
        publish_cas=publish_cas,
        emit_alert=emit_bridge_alert,
        config=config,
    ).reconcile_pending_recoveries(project_id=project_id, config=config)

def pending_network_recovery_projects(
    bridge_root: Path,
    *,
    project_id: str | None = None,
) -> set[str]:
    """Return projects whose network recovery evidence is still unresolved."""
    return RecoveryCoordinator(bridge_root).pending_network_recovery_projects(
        project_id=project_id
    )

def reconcile_pending_reports(
    bridge_root: Path,
    *,
    project_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> int:
    """Publish completed Worker reports before lease expiry or new execution."""
    return RecoveryCoordinator(
        bridge_root,
        publish_cas=publish_cas,
        emit_alert=emit_bridge_alert,
        config=config,
    ).reconcile_pending_reports(project_id=project_id, config=config)

def process_project(
    bridge_root: Path,
    project_id: str,
    project_cfg: dict[str, Any],
    config: dict[str, Any],
    *,
    arbiter: HostResourceArbiter | None = None,
    on_run_settled: Callable[[WorkerRunSettlement], None] | None = None,
    egress_helper: object | None = None,
    egress_process_launcher: object | None = None,
) -> bool:
    project_dir = bridge_root / "projects" / project_id
    state_path = project_dir / "state.json"
    mission_path = project_dir / "MISSION.md"

    if not state_path.exists() or not mission_path.exists():
        raise WorkerError(f"Missing mission/state for project: {project_id}")

    state = load_json(state_path)
    status = str(state.get("status", ""))
    _record_worker_health(
        bridge_root,
        last_seen_project=project_id,
        last_seen_state=status,
    )
    if int(state.get("protocol_version", 1)) < 2:
        return False

    if state.get("status") == "CODEX_RUNNING":
        maybe_mark_expired_lease(
            bridge_root,
            project_id,
            state_path,
            config=config,
        )
        return False

    if status not in PENDING_STATES:
        return False

    command_id = int(state.get("latest_command", 0))
    latest_report = int(state.get("latest_report", 0))
    if command_id <= 0 or command_id <= latest_report:
        return False

    _record_worker_health(
        bridge_root,
        last_command_seen=f"{project_id}#{command_id:03d}",
    )

    command_path = project_dir / "commands" / f"command-{command_id:03d}.md"
    report_path = project_dir / "reports" / f"report-{command_id:03d}.md"
    if not command_path.exists():
        raise WorkerError(f"State points to missing command: {command_path}")
    if report_path.exists():
        return False

    # Source labels (including manual_chatgpt) never bypass Owner control.
    # Running/recovery paths above are deliberately not gated by this switch.
    execution_control = worker_execution_control.new_execution_allowed(bridge_root)
    if not execution_control.execution_allowed:
        _record_worker_health(bridge_root, resource_wait_project=project_id, resource_wait_reason="owner_execution_blocked", **execution_control.health_projection())
        return False
    try:
        owner_console_control.require_project_admission(bridge_root, project_id)
    except (owner_console_control.ControlConflict, OSError, ValueError):
        _record_worker_health(bridge_root, resource_wait_project=project_id, resource_wait_reason="owner_project_paused_or_unavailable")
        return False
    command = command_path.read_text(encoding="utf-8")
    try:
        meta = command_metadata(command)
        validate_command(state=state, command_id=command_id, meta=meta)
        # Parse the optional phased body before claiming anything.  A malformed
        # phased command must fail closed rather than silently falling back to
        # the ordinary prompt path.
        phased_definition = phased_task.parse_phased_task(command)
    except (CASConflict, WorkerError, phased_task.PhasedTaskError) as exc:
        _record_preclaim_failure(
            bridge_root,
            project_id=project_id,
            command_id=command_id,
            state=status,
            error=exc,
        )
        raise
    phased_command_bytes = (
        command_path.read_bytes() if phased_definition is not None else None
    )

    workdir = resolve_path(
        str(project_cfg.get("workdir", "__BRIDGE_ROOT__")),
        bridge_root,
    )

    state_roots.guard_execution_workdir(bridge_root, workdir, project_cfg)

    self_maintenance_preflight: self_maintenance.SelfMaintenancePreflight | None = None
    try:
        self_maintenance_preflight = self_maintenance.preflight(
            project_cfg,
            candidate_workdir=workdir,
        )
    except self_maintenance.SelfMaintenancePreflightError as exc:
        _record_preclaim_failure(
            bridge_root,
            project_id=project_id,
            command_id=command_id,
            state=status,
            error=exc,
        )
        _record_worker_health(
            bridge_root,
            last_failure_stage="self_maintenance_preflight",
            last_self_maintenance_preflight_at=now_iso(),
            last_self_maintenance_preflight_project=project_id,
            last_self_maintenance_preflight_command_id=command_id,
            last_self_maintenance_preflight_status="rejected",
            last_self_maintenance_preflight_reason=exc.code,
        )
        print(
            f"[{now_iso()}] {project_id}: self-maintenance preflight rejected "
            f"({exc.code}); command remains unclaimed"
        )
        return False

    if self_maintenance_preflight is not None:
        _record_worker_health(
            bridge_root,
            last_self_maintenance_preflight_at=now_iso(),
            last_self_maintenance_preflight_project=project_id,
            last_self_maintenance_preflight_command_id=command_id,
            last_self_maintenance_preflight_status="accepted",
            last_self_maintenance_preflight_reason="accepted",
            last_self_maintenance_live_branch=self_maintenance_preflight.live.branch,
            last_self_maintenance_live_head=self_maintenance_preflight.live.head,
            last_self_maintenance_candidate_branch=self_maintenance_preflight.candidate.branch,
            last_self_maintenance_candidate_head=self_maintenance_preflight.candidate.head,
            last_self_maintenance_bootstrap_base=self_maintenance_preflight.config.bootstrap_base,
        )
    if not workdir.exists() or not workdir.is_dir():
        raise WorkerError(f"Configured workdir does not exist: {workdir}")

    network_decision = network_guard_check(config)
    if not network_decision.allowed:
        print(
            f"[{now_iso()}] {project_id}: network guard blocked "
            f"({network_decision.reason}); command remains unclaimed"
        )
        return False

    default_codex_args = codex_args_from_config(config)
    command_executor = command_executor_override(meta)
    codex_args, executor_profile = effective_codex_args(default_codex_args, meta)

    previous_status = status
    ready_snapshot = dict(state)
    run_id = f"run-{command_id:03d}-{uuid.uuid4().hex[:12]}"
    proposed_identity = RunIdentity(
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        claim_generation=protocol_core.claim_generation(
            int(state.get("generation", 0))
        ),
    )
    try:
        egress_preflight = preclaim_egress(
            config,
            proposed_identity,
            helper=egress_helper,  # type: ignore[arg-type]
            process_launcher=egress_process_launcher,
        )
    except (EgressRuntimeError, ValueError, TypeError) as exc:
        _record_preclaim_failure(
            bridge_root,
            project_id=project_id,
            command_id=command_id,
            state=status,
            error=exc,
        )
        print(
            f"[{now_iso()}] {project_id}: required egress containment rejected "
            f"({type(exc).__name__}); command remains unclaimed"
        )
        return False
    local_arbiter = arbiter or HostResourceArbiter(
        bridge_root,
        max_parallel_runs=1,
        worker_host=platform.node() or "unknown",
        worker_pid=os.getpid(),
    )
    exclusive_paths = project_cfg.get("exclusive_paths")
    if exclusive_paths is not None:
        if not isinstance(exclusive_paths, (list, tuple)):
            _record_worker_health(
                bridge_root,
                resource_wait_project=project_id,
                resource_wait_reason="invalid_exclusive_paths",
            )
            return False
        resolved_paths = []
        try:
            for value in exclusive_paths:
                resolved_paths.append(
                    resolve_path(str(value), bridge_root)
                )
        except (OSError, ValueError) as exc:
            _record_worker_health(
                bridge_root,
                resource_wait_project=project_id,
                resource_wait_reason="resource_identity_unknown",
            )
            print(
                f"[{now_iso()}] {project_id}: resource identity unavailable; "
                "command remains unclaimed"
            )
            return False
    else:
        resolved_paths = None
    git_target = git_target_identity(workdir)
    decision = local_arbiter.check_and_reserve(
        project_id,
        workdir=workdir,
        exclusive_paths=resolved_paths,
        run_id=run_id,
        admission_id=f"{project_id}#{command_id:03d}:{run_id}",
        git_target=git_target,
    )
    reservation = decision.reservation
    if reservation is None:
        _record_worker_health(
            bridge_root,
            resource_wait_project=project_id,
            resource_wait_reason=decision.reason or "resource_wait",
        )
        print(
            f"[{now_iso()}] {project_id}: resource wait "
            f"({decision.reason or 'unknown'}); command remains unclaimed"
        )
        return False
    _record_worker_health(
        bridge_root,
        resource_wait_project=None,
        resource_wait_reason=None,
        max_parallel_runs=local_arbiter.max_parallel_runs,
        active_run_count=len(local_arbiter.active_reservations()),
    )

    # The local reservation is not a Protocol claim.  Re-read the canonical
    # state after reserving and abandon locally if another writer changed it.
    try:
        reread = load_json(state_path)
    except Exception:
        reservation.close()
        raise
    if not same_ready_snapshot(reread, ready_snapshot):
        reservation.close()
        _record_worker_health(
            bridge_root,
            resource_wait_project=project_id,
            resource_wait_reason="canonical_state_changed",
        )
        return False

    timeout_seconds = int(config.get("codex_timeout_seconds", 14400))
    final_grace_seconds = float(config.get("codex_final_grace_seconds", 20))
    cleanup_timeout_seconds = float(config.get("codex_cleanup_timeout_seconds", 10))
    marker_stable_seconds = float(config.get("codex_final_marker_stable_seconds", 0.5))
    lifecycle_poll_seconds = float(config.get("codex_lifecycle_poll_seconds", 0.1))
    max_log_bytes = int(
        config.get(
            "codex_log_warning_bytes",
            config.get("codex_max_log_bytes", 10 * 1024 * 1024),
        )
    )
    max_final_message_bytes = int(
        config.get("codex_max_final_message_bytes", 2 * 1024 * 1024)
    )
    grace_seconds = max(300, int(config.get("lease_grace_seconds", 900)))
    lease_expires = now_dt() + timedelta(
        seconds=timeout_seconds + final_grace_seconds + cleanup_timeout_seconds + grace_seconds
    )

    _record_worker_health(
        bridge_root,
        last_claim_attempt=now_iso(),
        last_claim_attempt_detail=f"{project_id}#{command_id:03d}",
    )

    def claim_expected(current: dict[str, Any]) -> bool:
        return same_ready_snapshot(current, ready_snapshot)

    def claim_already(current: dict[str, Any]) -> bool:
        active = current.get("active_run")
        return (
            current.get("status") == "CODEX_RUNNING"
            and isinstance(active, dict)
            and active.get("run_id") == run_id
        )

    def claim_payload(current: dict[str, Any]) -> dict[Path, str]:
        # publish_cas re-enters this builder after a lost race: re-read each time.
        worker_execution_control.require_new_execution(bridge_root)
        owner_console_control.require_project_admission(bridge_root, project_id)
        updated = dict(current)
        updated["status"] = "CODEX_RUNNING"
        updated["generation"] = protocol_core.claim_generation(
            int(current.get("generation", 0))
        )
        updated["active_run"] = {
            "run_id": run_id,
            "command_id": command_id,
            "source": meta.get("source"),
            "based_on_report": meta.get("based_on_report"),
            "base_generation": ready_snapshot.get("generation"),
            "claimed_generation": updated["generation"],
            "claimed_at": now_iso(),
            "lease_expires_at": lease_expires.isoformat(timespec="seconds"),
            "worker_host": platform.node() or "unknown",
            "worker_pid": os.getpid(),
            "executor": dict(executor_profile),
        }
        updated["worker_host"] = platform.node() or "unknown"
        updated["worker_pid"] = os.getpid()
        updated["updated_at"] = now_iso()
        return {state_path: json_text(updated)}

    try:
        claimed_state = publish_cas(
            bridge_root=bridge_root,
            state_path=state_path,
            expected=claim_expected,
            already_applied=claim_already,
            payload_builder=claim_payload,
            message=f"bridge: claim {project_id} command {command_id:03d}",
        )
    except BaseException:
        reservation.close()
        raise
    claim_generation = int(claimed_state.get("generation", 0))
    record_active_run_health(
        bridge_root,
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        claim_generation=claim_generation,
        claimed_state=claimed_state,
        executor_profile=executor_profile,
    )
    _record_worker_health(
        bridge_root,
        last_claim_at=now_iso(),
        last_seen_project=project_id,
        last_seen_state="CODEX_RUNNING",
    )

    # Re-read immutable task inputs from the synchronized bridge tree after claim.
    runtime_dir = bridge_root / "worker" / "runtime" / project_id
    run_dir = runtime_dir / "runs" / run_id
    output_file = run_dir / "final-message.txt"
    stdout_log_path = run_dir / "stdout.log"
    stderr_log_path = run_dir / "stderr.log"
    run_identity = RunIdentity(
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        claim_generation=claim_generation,
    )

    mission = mission_path.read_text(encoding="utf-8")
    command = command_path.read_text(encoding="utf-8")
    phased_runtime: phased_task.RuntimeInspection | None = None
    preparation_error: str | None = None
    prompt = ""
    if phased_definition is None:
        # Keep the ordinary command prompt path byte-for-byte compatible with
        # the historical Worker behavior.
        prompt = build_prompt(project_id, mission, command)
    else:
        try:
            claimed_command_bytes = command_path.read_bytes()
            if phased_command_bytes != claimed_command_bytes:
                raise phased_task.PhasedTaskError(
                    "phased command changed after pre-claim validation"
                )
            phased_runtime = phased_task.prepare_runtime(
                run_dir,
                claimed_command_bytes,
                phased_task.ExecutionIdentity(
                    project_id=project_id,
                    command_id=command_id,
                    run_id=run_id,
                    claim_generation=claim_generation,
                ),
                bridge_root=bridge_root,
            )
            prompt = build_phased_prompt(
                project_id,
                mission,
                phased_runtime,
                bridge_root=bridge_root,
            )
        except (OSError, phased_task.PhasedTaskError) as exc:
            preparation_error = (
                _phase_diagnostic(
                    "PHASE_CONTRACT_FAILED",
                    f"phased runtime preparation failed: {type(exc).__name__}: {exc}",
                )
            )

    codex_command = str(config.get("codex_command", "codex"))
    codex_argv = command_argv(codex_command)

    head_before = get_head(workdir)
    run_result: CodexRunResult | None = None
    run_scope: RunScope | None = None
    scope_cleanup_errors: tuple[BaseException, ...] = ()
    reservation_owned = False
    wal_writer: DurableRunWal | None = None
    egress_resources: RunEgressResources | None = None
    # The recovery WAL remains activation-gated to the accepted vNext
    # runtime paths.  Typed recovery consumes only runs that actually own this
    # durable evidence; historical ordinary runs continue through the legacy
    # compatibility reconciler until their runtime is explicitly selected.
    wal_capture_enabled = (
        phased_definition is not None or egress_preflight.activation.required
    )

    def lifecycle_event(event: str, fields: dict[str, Any]) -> None:
        console_observation.record_event(run_dir, event, fields)
        if wal_writer is not None:
            try:
                wal_writer.record_lifecycle(event, fields)
            except DurableWalError as exc:
                raise WorkerError("durable WAL lifecycle capture failed") from exc
        rendered = " ".join(f"{key}={value}" for key, value in fields.items())
        print(
            f"[{now_iso()}] command_id={command_id} run_id={run_id} "
            f"event={event} {rendered}".rstrip()
        )

    def append_wal(
        kind: WalEventKind,
        *,
        artifact_digest: str | None = None,
        required: bool = True,
    ) -> None:
        if not wal_capture_enabled:
            return
        if wal_writer is None:
            raise WorkerError("durable WAL was not initialized")
        try:
            wal_writer.append(kind, artifact_digest=artifact_digest)
        except DurableWalError as exc:
            if required:
                raise WorkerError("durable WAL capture failed") from exc
            print(
                f"[{now_iso()}] {project_id}: durable WAL terminal event was not "
                f"recorded ({kind.value})",
                file=sys.stderr,
            )

    def report_digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    execution_request = ExecutionRequest(
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        workdir=workdir,
        mission=mission,
        command_text=command,
        kind=str(meta.get("kind", "EXECUTE")),
        profile=ExecutionProfile(
            model=executor_profile.get("model"),
            reasoning_effort=executor_profile.get("reasoning_effort"),
            # A tier parsed from the Worker default argv is already effective
            # configuration, not a command override.  Only an explicitly
            # validated command tier belongs in the provider override.
            service_tier=(
                command_executor.get("service_tier")
                if command_executor is not None
                else None
            ),
        ),
    )

    def close_run_scope() -> None:
        nonlocal scope_cleanup_errors
        if run_scope is None or run_scope.state is RunScopeState.CLOSED:
            return
        scope_cleanup_errors = run_scope.close()

    def scope_cleanup_diagnostic() -> str | None:
        if not scope_cleanup_errors:
            return None
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in scope_cleanup_errors
        )
        return f"RunScope cleanup diagnostics: {details}"

    try:
        if wal_capture_enabled:
            run_dir.mkdir(parents=True, exist_ok=True)
            wal_writer = DurableRunWal(run_dir / "recovery.wal", run_identity)
            append_wal(WalEventKind.RUN_CLAIMED)
        run_scope = RunScope.from_claimed_state(
            claimed_state,
            project_id=project_id,
            command_id=command_id,
            run_id=run_id,
            run_dir=run_dir,
            event_logger=lifecycle_event,
        )
        run_scope.activate()
        run_scope.own(reservation)
        reservation_owned = True
        if egress_preflight.activation.required:
            if (
                egress_preflight.policy is None
                or egress_preflight.helper is None
                or egress_process_launcher is None
            ):
                raise EgressRuntimeError(
                    "required egress preflight did not retain exact resources"
                )
            egress_resources = RunEgressResources(
                run_identity,
                egress_preflight.activation,
                helper=egress_preflight.helper,
                process_launcher=egress_process_launcher,
                run_dir=run_dir,
                event_logger=lifecycle_event,
            )
            run_scope.own_containment(
                egress_resources,
                process_launcher=egress_resources.process_launcher,
            )
            egress_resources.prepare()
            if not egress_resources.process_boundary_ready:
                raise EgressRuntimeError(
                    "contained process launcher lost the exact helper binding"
                )
        if preparation_error is not None:
            raise WorkerError(preparation_error)
        codex_kwargs: dict[str, Any] = {
            "codex_argv": codex_argv,
            "codex_args": codex_args,
            "workdir": workdir,
            "output_file": output_file,
            "stdout_log_path": stdout_log_path,
            "stderr_log_path": stderr_log_path,
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
            "final_grace_timeout_seconds": final_grace_seconds,
            "cleanup_timeout_seconds": cleanup_timeout_seconds,
            "marker_stable_seconds": marker_stable_seconds,
            "poll_interval_seconds": lifecycle_poll_seconds,
            "max_log_bytes": max_log_bytes,
            "max_final_message_bytes": max_final_message_bytes,
            "network_config": config,
            "event_logger": lifecycle_event,
            "run_scope": run_scope,
            "execution_request": execution_request,
            "stop_check": lambda: owner_console_control.stop_requested(bridge_root, project_id, {
                "run_id": run_id, "command_id": command_id, "generation": run_identity.claim_generation,
            }),
        }
        if egress_resources is not None:
            codex_kwargs["process_launcher"] = egress_resources.process_launcher
        run_result = codex_run(**codex_kwargs)
        close_run_scope()
        exit_code = run_result.exit_code
        final_message = run_result.final_message
        stderr_parts = [run_result.stderr_tail]
        for diagnostic in (
            run_result.log_threshold_diagnostic,
            run_result.runtime_error,
            run_result.cleanup_error,
            run_result.contract_error,
            scope_cleanup_diagnostic(),
        ):
            if diagnostic:
                stderr_parts.append(diagnostic)
        stderr = "\n".join(part.strip() for part in stderr_parts if part.strip())
    except NetworkGuardInterruption as exc:
        close_run_scope()
        run_result = exc.run_result
        interrupted_at = now_iso()
        _record_worker_health(
            bridge_root,
            last_process_exit=run_result.exit_code if run_result else 125,
            last_process_exit_at=interrupted_at,
            last_seen_state="RECOVERY_REQUIRED",
        )
        head_after = get_head(workdir)
        lifecycle_event("report_finalize_start", {"outcome": "NETWORK_INTERRUPTED"})
        report_text = report_markdown(
            project_id=project_id,
            command_id=command_id,
            outcome="NETWORK_INTERRUPTED",
            exit_code=run_result.exit_code if run_result else 125,
            workdir=workdir,
            head_before=head_before,
            head_after=head_after,
            final_message=run_result.final_message if run_result else "",
            stderr="\n".join(
                part
                for part in (
                    exc.stderr,
                    exc.reason,
                    run_result.cleanup_error if run_result else None,
                    scope_cleanup_diagnostic(),
                )
                if part
            ),
            meta=meta,
            run_id=run_id,
            claim_generation=claim_generation,
            executor_profile=executor_profile,
            run_result=run_result,
            self_maintenance_preflight=self_maintenance_preflight,
        )
        network_report_digest = report_digest(report_text)
        append_wal(
            WalEventKind.REPORT_MATERIALIZED,
            artifact_digest=network_report_digest,
        )
        append_wal(
            WalEventKind.REPORT_PUBLISH_INTENT,
            artifact_digest=network_report_digest,
        )
        recovery_result: dict[str, Any] | None = None
        try:
            recovery_result = publish_network_recovery(
                bridge_root=bridge_root,
                state_path=state_path,
                project_id=project_id,
                command_id=command_id,
                run_id=run_id,
                claim_generation=claim_generation,
                reason=exc.reason,
                report_text=report_text,
                runtime_dir=runtime_dir,
                workdir=workdir,
                head_before=head_before,
                head_after=head_after,
                claimed_at=str(
                    (claimed_state.get("active_run") or {}).get("claimed_at", "")
                )
                or None,
                lease_expires_at=str(
                    (claimed_state.get("active_run") or {}).get(
                        "lease_expires_at", ""
                    )
                )
                or None,
                report_path=report_path,
                run_result=run_result,
                interrupted_at=interrupted_at,
            )
        except (CASConflict, WorkerError) as recovery_error:
            refresh_active_run_health(bridge_root)
            emit_bridge_alert(
                bridge_root,
                config,
                _network_alert_context(
                    bridge_root=bridge_root,
                    state_path=state_path,
                    project_id=project_id,
                    command_id=command_id,
                    run_id=run_id,
                    claim_generation=claim_generation,
                    reason=exc.reason,
                    claimed_state=claimed_state,
                    report_path=report_path,
                    runtime_dir=runtime_dir,
                    run_result=run_result,
                    recovery_result=None,
                    remote_cas_success=False,
                    remote_publish_pending=True,
                ),
            )
            raise WorkerError(
                "Network guard interrupted Codex and recovery state could not be "
                "published safely; automatic rerun is prohibited. "
                f"{type(recovery_error).__name__}: {recovery_error}"
            ) from recovery_error
        append_wal(
            WalEventKind.REPORT_PUBLISHED,
            artifact_digest=network_report_digest,
            required=False,
        )
        emit_bridge_alert(
            bridge_root,
            config,
            _network_alert_context(
                bridge_root=bridge_root,
                state_path=state_path,
                project_id=project_id,
                command_id=command_id,
                run_id=run_id,
                claim_generation=claim_generation,
                reason=exc.reason,
                claimed_state=claimed_state,
                report_path=report_path,
                runtime_dir=runtime_dir,
                run_result=run_result,
                recovery_result=recovery_result,
                remote_cas_success=True,
                remote_publish_pending=(
                    recovery_result or {}
                ).get("remote_publish_pending", False),
            ),
        )
        refresh_active_run_health(bridge_root)
        print(
            f"[{now_iso()}] {project_id}: network interruption -> "
            "RECOVERY_REQUIRED (automatic rerun prohibited)"
        )
        prune_run_directories(
            run_dir.parent,
            run_dir,
            int(config.get("codex_log_retention_runs", 20)),
        )
        return True
    except Exception as exc:
        close_run_scope()
        exit_code = 125
        final_message = ""
        stderr = "\n".join(
            part
            for part in (f"{type(exc).__name__}: {exc}", scope_cleanup_diagnostic())
            if part
        )
    finally:
        close_run_scope()
        if not reservation_owned:
            reservation.close()

    _record_worker_health(
        bridge_root,
        last_process_exit=exit_code,
        last_process_exit_at=now_iso(),
    )

    head_after = get_head(workdir)
    outcome = execution_outcome(run_result)
    phase_inspection = phased_runtime
    phase_contract_error: str | None = preparation_error
    if phased_runtime is not None:
        try:
            phase_inspection = phased_task.verify_runtime(
                run_dir,
                bridge_root=bridge_root,
            )
        except (OSError, phased_task.PhasedTaskError) as exc:
            phase_contract_error = _phase_diagnostic(
                "PHASE_CONTRACT_FAILED"
                if outcome == "SUCCESS"
                else "PHASE_RUNTIME_DIAGNOSTIC",
                f"{type(exc).__name__}: {exc}",
            )
            try:
                # A tampered projection must not be trusted for acceptance,
                # but the still-readable progress is useful bounded evidence
                # in the failure report.
                progress = phased_task.load_progress(
                    run_dir,
                    bridge_root=bridge_root,
                )
                phase_inspection = phased_task.RuntimeInspection(
                    paths=phased_runtime.paths,
                    task=phased_runtime.task,
                    manifest=phased_runtime.manifest,
                    progress=progress,
                )
            except (OSError, phased_task.PhasedTaskError):
                pass
        else:
            if outcome == "SUCCESS" and not phase_inspection.progress[
                "final_acceptance_verified"
            ]:
                current_phase = phase_inspection.progress["current_phase"]
                phase_label = (
                    "final acceptance"
                    if current_phase == phased_task.FINAL_ACCEPTANCE
                    else f"phase {current_phase}"
                )
                phase_contract_error = (
                    _phase_diagnostic("PHASE_CONTRACT_FAILED", f"{phase_label} incomplete")
                )
        if phase_contract_error and phase_contract_error.startswith(
            "PHASE_CONTRACT_FAILED:"
        ):
            outcome = "FAILED"
        if phase_contract_error:
            stderr = "\n".join(
                part for part in (stderr, phase_contract_error) if part
            )
    self_maintenance_guard: self_maintenance.SelfMaintenanceGuard | None = None
    if self_maintenance_preflight is not None:
        self_maintenance_guard = self_maintenance.verify_after_run(
            self_maintenance_preflight
        )
        _record_worker_health(
            bridge_root,
            last_self_maintenance_guard_at=now_iso(),
            last_self_maintenance_guard_status=(
                "accepted" if self_maintenance_guard.allowed else "rejected"
            ),
            last_self_maintenance_guard_reason=self_maintenance_guard.reason,
            last_self_maintenance_guard_violations=self_maintenance_guard.violations,
        )
        if not self_maintenance_guard.allowed:
            outcome = "FAILED"
            stderr = "\n".join(
                part
                for part in (
                    stderr,
                    "SELF_MAINTENANCE_GUARD_FAILED: "
                    f"{self_maintenance_guard.reason}",
                )
                if part
            )
    lifecycle_event("report_finalize_start", {"outcome": outcome})
    report_text = report_markdown(
        project_id=project_id,
        command_id=command_id,
        outcome=outcome,
        exit_code=exit_code,
        workdir=workdir,
        head_before=head_before,
        head_after=head_after,
        final_message=final_message,
        stderr=stderr,
        meta=meta,
        run_id=run_id,
        claim_generation=claim_generation,
        executor_profile=executor_profile,
        run_result=run_result,
        phased_runtime=phase_inspection,
        phase_contract_error=phase_contract_error,
        self_maintenance_preflight=self_maintenance_preflight,
        self_maintenance_guard=self_maintenance_guard,
    )
    final_report_digest = report_digest(report_text)
    append_wal(
        WalEventKind.REPORT_MATERIALIZED,
        artifact_digest=final_report_digest,
    )
    append_wal(
        WalEventKind.REPORT_PUBLISH_INTENT,
        artifact_digest=final_report_digest,
    )
    target_status = intended_report_status(
        previous_status=previous_status,
        kind=str(meta.get("kind", "EXECUTE")),
        outcome=outcome,
        final_message=final_message,
        run_result=run_result,
    )
    last_execution_error = intended_execution_error(
        outcome=outcome,
        run_result=run_result,
        exit_code=exit_code,
    )
    if phase_contract_error and phase_contract_error.startswith(
        "PHASE_CONTRACT_FAILED:"
    ):
        last_execution_error = pending_report.safe_execution_error(phase_contract_error)
    if self_maintenance_guard is not None and not self_maintenance_guard.allowed:
        last_execution_error = pending_report.safe_execution_error(
            "SELF_MAINTENANCE_GUARD_FAILED: "
            f"{self_maintenance_guard.reason}"
        )

    def complete_expected(current: dict[str, Any]) -> bool:
        return protocol_core.matches_execution_lease(
            current,
            run_id=run_id,
            command_id=command_id,
            claimed_generation=claim_generation,
            source=str(meta.get("source", "")),
            project_id=project_id,
        )

    def complete_already(current: dict[str, Any]) -> bool:
        return (
            int(current.get("latest_report", -1)) == command_id
            and current.get("status")
            in {"REPORT_READY", "FINAL_REPORT_READY", "RECOVERY_REQUIRED"}
        )

    def complete_payload(current: dict[str, Any]) -> dict[Path, str]:
        updated = dict(current)
        updated["latest_report"] = command_id
        updated["generation"] = protocol_core.report_generation(
            int(current.get("generation", 0))
        )
        updated["updated_at"] = now_iso()
        updated["worker_pid"] = None
        updated["active_run"] = None
        updated["status"] = target_status
        if last_execution_error is not None:
            updated["last_execution_error"] = last_execution_error
        else:
            updated.pop("last_execution_error", None)
        return {
            report_path: report_text,
            state_path: json_text(updated),
        }

    report_push_recorder: WalPushEffectRecorder | None = None
    if wal_writer is not None:
        report_push_recorder = WalPushEffectRecorder(
            wal_writer,
            effect_prefix=f"canonical-report-{command_id}",
        )

    pending_metadata_path: Path | None = None
    if wal_writer is not None:
        pending_metadata_path = pending_report.save_worker_pending_report(
            bridge_root=bridge_root,
            project_id=project_id,
            command_id=command_id,
            run_id=run_id,
            claim_generation=claim_generation,
            source=str(meta.get("source", "")),
            kind=str(meta.get("kind", "EXECUTE")),
            previous_status=previous_status,
            outcome=outcome,
            target_status=target_status,
            report_text=report_text,
            reason="canonical report publication is pending; recovery may retry this exact report only",
            last_execution_error=last_execution_error,
            evidence_mode="typed",
        )

    try:
        final_state = publish_cas(
            bridge_root=bridge_root,
            state_path=state_path,
            expected=complete_expected,
            already_applied=complete_already,
            payload_builder=complete_payload,
            message=f"bridge: report {project_id} command {command_id:03d}",
            push_effect_recorder=report_push_recorder,
        )
    except (CASConflict, WorkerError) as exc:
        reason = (
            "Codex already finished, but its report could not be safely published. "
            "The worker will not rerun Codex automatically.\n"
            f"{type(exc).__name__}: {exc}"
        )
        try:
            pending_report.save_worker_pending_report(
                bridge_root=bridge_root,
                project_id=project_id,
                command_id=command_id,
                run_id=run_id,
                claim_generation=claim_generation,
                source=str(meta.get("source", "")),
                kind=str(meta.get("kind", "EXECUTE")),
                previous_status=previous_status,
                outcome=outcome,
                target_status=target_status,
                report_text=report_text,
                reason=reason,
                last_execution_error=last_execution_error,
                evidence_mode=("typed" if wal_writer is not None else "legacy"),
            )
        finally:
            refresh_active_run_health(bridge_root)
        raise WorkerError(reason) from exc
    if pending_metadata_path is not None:
        try:
            pending_evidence = pending_report.load_evidence(
                bridge_root,
                pending_metadata_path,
            )
            pending_report.mark_evidence_status(
                pending_evidence,
                status="reconciled",
                reason="canonical report publication completed; retained exact report evidence",
            )
        except (OSError, pending_report.PendingReportError):
            # Canonical publication already succeeded.  Keep the WAL/report
            # evidence for startup inspection if this local acknowledgement is
            # interrupted; never turn an acknowledgement failure into replay.
            print(
                f"[{now_iso()}] {project_id}: typed pending-report acknowledgement deferred",
                file=sys.stderr,
            )
    append_wal(
        WalEventKind.REPORT_PUBLISHED,
        artifact_digest=final_report_digest,
        required=False,
    )

    if (
        phased_runtime is not None
        and outcome == "SUCCESS"
        and final_state.get("status") in {"REPORT_READY", "FINAL_REPORT_READY"}
    ):
        try:
            pending_publication, recovery_unresolved, conflict = (
                _phased_compaction_gates(
                    bridge_root,
                    project_id,
                    command_id,
                    run_id,
                )
            )
            phased_task.safe_compact_successful_run(
                run_dir,
                outcome=outcome,
                canonical_publication_succeeded=True,
                pending_publication=pending_publication,
                recovery_unresolved=recovery_unresolved,
                conflict=conflict,
                bridge_root=bridge_root,
            )
        except (OSError, phased_task.PhasedTaskError) as exc:
            # Canonical publication is already complete.  Retain all evidence
            # and leave it for the existing bounded run pruning if compaction
            # itself cannot be completed safely.
            print(
                f"[{now_iso()}] phased evidence compaction deferred for "
                f"{project_id}#{command_id:03d}: {type(exc).__name__}",
                file=sys.stderr,
            )

    if final_state.get("status") == "RECOVERY_REQUIRED":
        emit_bridge_alert(
            bridge_root,
            config,
            {
                "timestamp": now_iso(),
                "project_id": project_id,
                "command_id": command_id,
                "run_id": run_id,
                "alert_kind": "recovery_required",
                "current_bridge_status": "RECOVERY_REQUIRED",
                "original_status": "CODEX_RUNNING",
                "generation": _state_int(
                    final_state.get("generation"),
                    protocol_core.recovery_generation(claim_generation),
                ),
                "generation_before": claim_generation,
                "claim_generation": claim_generation,
                "interruption_recovery_type": "run converted to RECOVERY_REQUIRED",
                "recovery_reason_safe": str(
                    final_state.get(
                        "last_execution_error",
                        final_state.get("recovery_reason", "manual recovery required"),
                    )
                ),
                "codex_terminated": (
                    bool(run_result and run_result.process_exited_at)
                    if run_result is not None
                    else None
                ),
                "worker_is_alive": True,
                "journal_saved": False,
                "pending_report_saved": False,
                "remote_recovery_cas_success": True,
                "remote_publish_pending": False,
                "worktree_dirty": None,
                "local_commit_created": None,
                "unpushed_commits_present": None,
                "external_side_effects_unknown": True,
                "lease_expires_at": (claimed_state.get("active_run") or {}).get(
                    "lease_expires_at"
                ),
                "claimed_at": (claimed_state.get("active_run") or {}).get(
                    "claimed_at"
                ),
            },
        )

    print(
        f"[{now_iso()}] {project_id}: command {command_id:03d} "
        f"-> {final_state.get('status')} event=state_finalize_complete "
        f"command_id={command_id} run_id={run_id}"
    )
    _record_worker_health(
        bridge_root,
        last_seen_project=project_id,
        last_seen_state=str(final_state.get("status", "")),
    )
    refresh_active_run_health(bridge_root)
    prune_run_directories(
        run_dir.parent,
        run_dir,
        int(config.get("codex_log_retention_runs", 20)),
    )
    if (
        on_run_settled is not None
        and outcome == "SUCCESS"
        and final_state.get("status") in {"REPORT_READY", "FINAL_REPORT_READY"}
    ):
        on_run_settled(
            WorkerRunSettlement(
                project_id=project_id,
                command_id=command_id,
                run_id=run_id,
                claim_generation=claim_generation,
                outcome=outcome,
                final_state=dict(final_state),
            )
        )
    return True


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path)
    projects = config.get("projects")
    if not isinstance(projects, dict):
        raise WorkerError("Config must contain a 'projects' object.")
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-Agent-Bridge local Codex worker")
    parser.add_argument(
        "--config",
        default="worker/config.local.json",
        help="Config path relative to bridge repo (default: worker/config.local.json)",
    )
    parser.add_argument("--state-root", type=Path, help="Explicit private State Git root")
    parser.add_argument("--once", action="store_true", help="Poll once, then exit")
    parser.add_argument("--project", help="Only process this configured project id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bridge_root = state_roots.resolve_state_root(args.state_root, for_write=True)

    ensure_tool("git")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = bridge_root / config_path
    config = load_config(config_path)
    state_roots.require_split_runtime_policy(bridge_root, config)
    codex_command = str(config.get("codex_command", "codex"))
    if shutil.which(codex_command) is None and not Path(codex_command).exists():
        raise WorkerError(f"Required Codex command not found: {codex_command}")
    codex_args_from_config(config)

    poll_seconds = max(5, int(config.get("poll_seconds", 30)))
    lock_path = bridge_root / "worker" / "runtime" / "worker.lock"
    coordinator: WorkerCoordinator | None = None
    supervisor_gateway = SupervisorPublicationGateway(bridge_root)

    try:
        with WorkerInstanceLock(lock_path):
            coordinator = WorkerCoordinator(bridge_root, config)
            while True:
                try:
                    processed_any = coordinator.reap()
                    sync_to_remote(bridge_root)
                    # Reconcile locally durable interruption evidence before
                    # normal command processing can claim anything new.
                    processed_any = bool(
                        reconcile_pending_recoveries(
                            bridge_root,
                            project_id=args.project,
                        )
                    ) or processed_any
                    processed_any = bool(
                        reconcile_pending_reports(
                            bridge_root,
                            project_id=args.project,
                            config=config,
                        )
                    ) or processed_any
                    gateway_results = supervisor_gateway.poll(project_id=args.project)
                    gateway_processed = any(
                        result.outcome in {"published", "already_applied"}
                        for result in gateway_results
                    )
                    for result in gateway_results:
                        print(
                            f"[{now_iso()}] supervisor_gateway "
                            f"request={result.request_id} outcome={result.outcome} "
                            f"project={result.project_id or 'unknown'} "
                            f"command={result.command_id or 0} reason={result.reason}"
                        )
                    processed_any = gateway_processed or processed_any
                    projects: dict[str, Any] = config["projects"]
                    for project_id, project_cfg in projects.items():
                        if args.project and project_id != args.project:
                            continue
                        if (
                            not isinstance(project_cfg, dict)
                            or not project_cfg.get("enabled", True)
                        ):
                            continue
                        coordinator.submit(project_id, project_cfg, config)
                    if args.once:
                        processed_any = coordinator.reap(wait=True) or processed_any
                        if not processed_any:
                            print(f"[{now_iso()}] No pending command.")
                        return 0
                except KeyboardInterrupt:
                    return 130
                except Exception as exc:
                    print(
                        f"[{now_iso()}] ERROR: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    if args.once:
                        return 1

                time.sleep(poll_seconds)
    except WorkerError as exc:
        print(f"[{now_iso()}] ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if coordinator is not None:
            coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
