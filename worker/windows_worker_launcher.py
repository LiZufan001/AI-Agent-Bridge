#!/usr/bin/env python3
"""Windowless Task Scheduler launcher for the local Bridge Worker.

The launcher keeps the scheduled process console-free while preserving the
Worker's normal stdout/stderr in a small append-only local log. A dedicated
Worker restart exit code allows tracked Worker implementation updates pulled
from GitHub to take effect without Task Scheduler/manual intervention.
"""

from __future__ import annotations

import state_roots

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path

import worker_health
from vnext_runtime.adoption import AdoptionPolicy
from vnext_runtime.handoff import (
    HANDOFF_EVIDENCE_FILENAME,
    HandoffConsumeResult,
    HandoffEvidenceStore,
    LauncherHandoffActions,
    LauncherHandoffConsumer,
    OuterControllerHandoff,
)
from vnext_runtime.launcher_actions import (
    LauncherActionError,
    LauncherOwnedHandoffActions,
    create_launcher_action_controller as _create_launcher_action_controller,
)

WORKER_RESTART_CODE = 75
# This is a diagnostic threshold, not a process-lifetime stop condition.
RESTART_DIAGNOSTIC_THRESHOLD = 5
# Keep the old name import-compatible for local tooling; it is no longer a cap.
MAX_CONSECUTIVE_RESTARTS = RESTART_DIAGNOSTIC_THRESHOLD
BASE_RESTART_DELAY_SECONDS = 1.0
MAX_RESTART_DELAY_SECONDS = 300.0
HEALTHY_WORKER_RESET_SECONDS = 60.0


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def retry_at(delay_seconds: float) -> str:
    return (
        datetime.now(timezone.utc).astimezone()
        + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="seconds")


def restart_delay(consecutive_failures: int, worker_lifetime_seconds: float) -> float:
    """Return bounded exponential backoff without ever making the launcher exit."""
    if worker_lifetime_seconds >= HEALTHY_WORKER_RESET_SECONDS:
        consecutive_failures = 1
    consecutive_failures = max(1, consecutive_failures)
    return min(
        MAX_RESTART_DELAY_SECONDS,
        BASE_RESTART_DELAY_SECONDS * (2 ** min(consecutive_failures - 1, 12)),
    )


def update_launcher_health(log_file: Path, **updates: Any) -> None:
    """Health evidence must never turn a healthy launcher into a failed one."""
    try:
        worker_health.update_launcher_health(log_file, **updates)
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windowless AI-Agent-Bridge Worker launcher")
    parser.add_argument("--worker-script", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument(
        "--allow-controlled-adoption",
        action="store_true",
        help="Arm operator-bound manual adoption actions in this outer Launcher.",
    )
    return parser.parse_args()


def resolve_worker_entrypoint(requested: Path) -> Path:
    """Prefer the hardened wrapper without requiring Task Scheduler rewiring."""
    requested = requested.resolve()
    if requested.name.lower() == "bridge_worker.py":
        hardened = requested.with_name("bridge_worker_hardened.py")
        if hardened.is_file():
            return hardened
    return requested


class _NoopLifetimeScope:
    """Non-Windows fallback preserving the launcher's existing behavior."""

    def attach_current_process(self) -> None:
        return None

    def close(self, *, graceful: bool = False) -> None:
        return None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _WindowsJobLifetimeScope:
        """Bind this launcher and all descendants to a kernel-owned Job Object."""

        def __init__(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle
            try:
                info = _JobObjectExtendedLimitInformation()
                info.BasicLimitInformation.LimitFlags = (
                    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                if not _kernel32.SetInformationJobObject(
                    self.handle,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not _kernel32.AssignProcessToJobObject(
                    self.handle,
                    _kernel32.GetCurrentProcess(),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                _kernel32.CloseHandle(self.handle)
                self.handle = None
                raise

        def attach_current_process(self) -> None:
            """The launcher is already attached; descendants inherit this Job."""
            return None

        def close(self, *, graceful: bool = False) -> None:
            """Keep the handle alive until process exit so the kernel cleans up."""
            return None


def create_lifetime_scope() -> Any:
    """Create the Windows kernel lifetime binding, or the legacy fallback."""
    if os.name == "nt":
        return _WindowsJobLifetimeScope()
    return _NoopLifetimeScope()


def create_handoff_controller(runtime_root: Path) -> OuterControllerHandoff:
    """Create the durable handoff authority owned by this Launcher boundary.

    The Worker receives no handoff authority from this helper.  The returned
    controller only records/validates an already-authorized identity and never
    runs a command or mutates Git.  The caller chooses a local runtime path;
    no canonical ``projects/**`` file is used for this evidence.
    """

    root = Path(runtime_root).resolve()
    return OuterControllerHandoff(
        HandoffEvidenceStore(root / HANDOFF_EVIDENCE_FILENAME)
    )


def create_handoff_consumer(
    runtime_root: Path,
    *,
    actions: LauncherHandoffActions | None = None,
) -> LauncherHandoffConsumer:
    """Create the persistent Launcher-side consumer for one runtime root.

    The default action set is fail-closed.  A separately authorized outer
    integration must provide infrastructure callbacks; this factory never
    gives the Worker a way to promote, restart, roll back, or invoke Codex.
    """

    return LauncherHandoffConsumer(
        create_handoff_controller(runtime_root),
        actions=actions,
    )


def _controlled_adoption_policy(config_path: Path) -> AdoptionPolicy | None:
    """Read only the explicit adoption flags; malformed config fails closed."""

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        policy = AdoptionPolicy.from_mapping(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    # This bootstrap is controlled-only.  A local operator may later arm the
    # controlled flag, but an unattended flag must never accidentally arm the
    # Launcher-owned action set.
    if not policy.controlled_adoption_enabled or policy.unattended_adoption_enabled:
        return None
    return policy


def create_launcher_action_controller(
    *,
    repository_root: Path,
    runtime_root: Path,
    worker_script: Path,
    config_path: Path,
    state_root: Path | None = None,
    log_file: Path,
    policy: AdoptionPolicy | None,
    expected_remote: str | None = None,
) -> LauncherOwnedHandoffActions | None:
    """Construct concrete actions only after explicit controlled enablement."""

    if policy is None:
        return None
    try:
        return _create_launcher_action_controller(
            repository_root=repository_root,
            runtime_root=runtime_root,
            worker_script=worker_script,
            state_root=state_root,
            config_path=config_path,
            log_file=log_file,
            adoption_policy=policy,
            expected_remote=expected_remote,
        )
    except LauncherActionError:
        # A bad local action configuration must leave the existing fail-closed
        # consumer in place and must not stop ordinary Worker supervision.
        return None


def _handoff_evidence_present(consumer: LauncherHandoffConsumer) -> bool:
    """Detect startup recovery evidence without treating corrupt data as absent."""

    if not isinstance(consumer, LauncherHandoffConsumer):
        return False
    try:
        return consumer.controller.store.path.exists()
    except (AttributeError, OSError):
        return False


def _consume_handoff_until_terminal(
    consumer: LauncherHandoffConsumer,
    *,
    worker_exit_code: int,
) -> HandoffConsumeResult:
    """Poll one durable handoff until a terminal result or bounded block."""

    result = consumer.try_consume_after_worker_exit(worker_exit_code=worker_exit_code)
    if result.status.value == "NO_HANDOFF" or result.terminal:
        return result
    started = time.monotonic()
    while time.monotonic() - started < 300.0:
        time.sleep(0.25)
        result = consumer.try_consume_after_worker_exit(worker_exit_code=worker_exit_code)
        if result.status.value == "NO_HANDOFF" or result.terminal:
            return result
        if result.status.value == "BLOCKED":
            return result
    return HandoffConsumeResult(
        status=result.status,
        phase=result.phase,
        transition_seq=result.transition_seq,
        reason="handoff_consume_timeout",
        actions=result.actions,
    )


def _record_handoff_result(log: Any, result: HandoffConsumeResult) -> None:
    """Record only bounded consumer status, phase and reason in launcher logs."""

    phase = result.phase.value if result.phase is not None else None
    reason = result.reason or "none"
    log.write(
        f"[{now_iso()}] handoff_consume status={result.status.value} "
        f"phase={phase} transition_seq={result.transition_seq} reason={reason}\n"
    )


def main() -> int:
    args = parse_args()
    worker_script = resolve_worker_entrypoint(args.worker_script)
    config_path = args.config.resolve()
    log_file = args.log_file.resolve()
    if not worker_script.is_file():
        raise FileNotFoundError(f"Worker script does not exist: {worker_script}")
    if not config_path.is_file():
        raise FileNotFoundError(f"Worker config does not exist: {config_path}")

    state_root = state_roots.resolve_state_root(args.state_root, for_write=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    deployment = state_roots.require_split_runtime_policy(state_root, config)
    runtime_root = state_root / "worker/runtime"
    action_controller = create_launcher_action_controller(
        repository_root=worker_script.parent.parent,
        runtime_root=runtime_root,
        worker_script=worker_script,
        state_root=state_root,
        config_path=config_path,
        log_file=log_file,
        policy=(
            _controlled_adoption_policy(config_path)
            if getattr(args, "allow_controlled_adoption", False)
            else None
        ),
        expected_remote=(
            f"https://github.com/{deployment['repository']}.git"
            if deployment is not None
            else None
        ),
    )
    # This object is deliberately created before the first Worker and lives
    # across every Worker replacement in the loop.  Without explicit
    # controlled adoption enablement the action set remains fail-closed.
    if action_controller is None:
        handoff_consumer = create_handoff_consumer(runtime_root)
    else:
        handoff_consumer = create_handoff_consumer(
            runtime_root,
            actions=action_controller.as_actions(),
        )

    log_file.parent.mkdir(parents=True, exist_ok=True)
    lifetime_scope = create_lifetime_scope()
    lifetime_scope.attach_current_process()
    consecutive_failures = 0
    restart_count = 0

    pending_process: Any | None = None
    try:
        with log_file.open("a", encoding="utf-8", buffering=1) as log:
            launcher_started_at = now_iso()
            log.write(f"[{now_iso()}] launcher_start pid={os.getpid()}\n")
            log.write(
                f"[{now_iso()}] process_lifetime_scope={type(lifetime_scope).__name__}\n"
            )
            update_launcher_health(
                log_file,
                launcher_started_at=launcher_started_at,
                last_poll_at=launcher_started_at,
                launcher_pid=os.getpid(),
                worker_pid=None,
                last_exit_code=None,
                restart_count=restart_count,
                consecutive_failures=consecutive_failures,
                next_retry_at=None,
            )

            # A new Launcher must resume durable non-terminal evidence before
            # starting an ordinary Worker.  This is the crash/restart gap that
            # the old fail-closed wiring could not safely cross.
            # A disabled production policy must leave historical handoff
            # evidence untouched.  In particular, the stale attempt-020 file
            # is not consumed or reinterpreted merely because this Launcher
            # gained the new wiring.  An explicitly controlled policy is the
            # only gate that permits the outer consumer to inspect it.
            if action_controller is not None and _handoff_evidence_present(handoff_consumer):
                startup_result = _consume_handoff_until_terminal(
                    handoff_consumer,
                    worker_exit_code=WORKER_RESTART_CODE,
                )
                _record_handoff_result(log, startup_result)
                if not startup_result.terminal:
                    log.write(
                        f"[{now_iso()}] handoff_restart_suppressed reason=non_terminal_handoff\n"
                    )
                    return 1
                if action_controller is not None:
                    pending_process = action_controller.take_replacement_process()

            while True:
                if pending_process is None:
                    # Resolve again because a pulled update may replace the
                    # hardened entrypoint.
                    worker_script = resolve_worker_entrypoint(args.worker_script)
                    command = [
                        sys.executable,
                        "-u",
                        str(worker_script),
                        "--config",
                        str(config_path),
                    ]
                    log.write(f"[{now_iso()}] worker_command={command!r}\n")
                    try:
                        process = subprocess.Popen(
                            command,
                            cwd=str(worker_script.parent.parent),
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            text=True,
                            env={**os.environ, "PYTHONUNBUFFERED": "1", state_roots.STATE_ENV: str(state_root)},
                            creationflags=(
                                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                            ),
                        )
                    except Exception as exc:
                        restart_count += 1
                        consecutive_failures += 1
                        delay_seconds = restart_delay(consecutive_failures, 0.0)
                        log.write(
                            f"[{now_iso()}] worker_start_error type={type(exc).__name__} "
                            f"backoff_seconds={delay_seconds:g}\n"
                        )
                        update_launcher_health(
                            log_file,
                            worker_pid=None,
                            last_exit_code=None,
                            last_worker_exit_at=now_iso(),
                            restart_count=restart_count,
                            consecutive_failures=consecutive_failures,
                            next_retry_at=retry_at(delay_seconds),
                            last_failure_kind="worker_start_error",
                        )
                        if consecutive_failures >= RESTART_DIAGNOSTIC_THRESHOLD:
                            log.write(
                                f"[{now_iso()}] worker_restart_failure_threshold "
                                f"count={consecutive_failures} action=continue "
                                f"backoff_seconds={delay_seconds:g}\n"
                            )
                        time.sleep(delay_seconds)
                        continue
                else:
                    process = pending_process
                    pending_process = None

                worker_started_at = now_iso()
                worker_started_monotonic = time.monotonic()
                log.write(f"[{worker_started_at}] worker_started pid={process.pid}\n")
                update_launcher_health(
                    log_file,
                    worker_started_at=worker_started_at,
                    worker_pid=process.pid,
                    next_retry_at=None,
                    restart_count=restart_count,
                    consecutive_failures=consecutive_failures,
                )
                try:
                    while True:
                        try:
                            exit_code = process.wait(timeout=15)
                            break
                        except subprocess.TimeoutExpired:
                            update_launcher_health(log_file, launcher_pid=os.getpid(),
                                                   worker_pid=process.pid, last_poll_at=now_iso())
                except Exception as exc:
                    # A wait failure is itself a worker failure; keep the watchdog alive.
                    exit_code = 1
                    log.write(
                        f"[{now_iso()}] worker_wait_error type={type(exc).__name__}\n"
                    )
                worker_lifetime_seconds = time.monotonic() - worker_started_monotonic
                log.write(f"[{now_iso()}] worker_exit code={exit_code}\n")
                update_launcher_health(
                    log_file,
                    worker_pid=None,
                    last_exit_code=exit_code,
                    last_worker_exit_at=now_iso(),
                )

                if action_controller is not None:
                    action_controller.bind_worker_exit(process, exit_code)
                # Keep the consumer boundary live for every Worker exit.  With
                # no concrete controller this is the existing fail-closed
                # action set: no handoff is a no-op, while present evidence is
                # blocked rather than interpreted or replayed.
                handoff_result = _consume_handoff_until_terminal(
                    handoff_consumer,
                    worker_exit_code=exit_code,
                )
                if handoff_result.status.value != "NO_HANDOFF":
                    _record_handoff_result(log, handoff_result)
                    if not handoff_result.terminal:
                        # Never restart a Worker around an ambiguous or
                        # unconfigured non-terminal handoff.  A separately
                        # authorized outer integration must make the evidence
                        # transition before the normal watchdog can continue.
                        log.write(
                            f"[{now_iso()}] handoff_restart_suppressed reason=non_terminal_handoff\n"
                        )
                        return 1
                    if action_controller is not None:
                        pending_process = action_controller.take_replacement_process()
                        if pending_process is not None:
                            continue

                if exit_code == 0:
                    return exit_code
                restart_count += 1
                if worker_lifetime_seconds >= HEALTHY_WORKER_RESET_SECONDS:
                    consecutive_failures = 1
                else:
                    consecutive_failures += 1
                delay_seconds = restart_delay(
                    consecutive_failures,
                    worker_lifetime_seconds,
                )
                reason = (
                    "controlled_restart"
                    if exit_code == WORKER_RESTART_CODE
                    else "worker_nonzero_exit"
                )
                if consecutive_failures >= RESTART_DIAGNOSTIC_THRESHOLD:
                    log.write(
                        f"[{now_iso()}] worker_restart_failure_threshold "
                        f"count={consecutive_failures} action=continue "
                        f"backoff_seconds={delay_seconds:g}\n"
                    )
                log.write(
                    f"[{now_iso()}] worker_restart_requested "
                    f"count={restart_count} consecutive_failures={consecutive_failures} "
                    f"reason={reason} backoff_seconds={delay_seconds:g}\n"
                )
                update_launcher_health(
                    log_file,
                    restart_count=restart_count,
                    consecutive_failures=consecutive_failures,
                    next_retry_at=retry_at(delay_seconds),
                    last_failure_kind=reason,
                )
                time.sleep(delay_seconds)
    finally:
        if action_controller is not None:
            action_controller.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # pythonw has no console; make startup failures diagnosable in the same log.
        try:
            log_path = parse_args().log_file.resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                log.write(f"[{now_iso()}] launcher_error\n")
                traceback.print_exc(file=log)
        finally:
            raise
