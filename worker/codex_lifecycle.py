#!/usr/bin/env python3
"""Bounded Codex subprocess lifecycle management.

The Worker must never use stdout/stderr pipe EOF as a completion signal.  This
module redirects both streams to per-run files, treats a stable strict final
marker as an independent business-result signal, and owns an OS process-tree
boundary for cleanup.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Mapping, Protocol


MarkerParser = Callable[[str], dict[str, Any] | None]
EventLogger = Callable[[str, dict[str, Any]], None]
GuardCheck = Callable[[], tuple[bool, str]]


class ProcessLauncher(Protocol):
    """Narrow process-creation seam for an already-prepared run boundary."""

    @property
    def is_contained(self) -> bool:
        """Prove that the owner-installed contained launch path is selected."""

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
        """Return the lifecycle root or raise before creating a process."""

MARKER_VALID = "VALID"
MARKER_PROCESS_EXITED_WITHOUT_MARKER = "PROCESS_EXITED_WITHOUT_MARKER"
MARKER_PROCESS_TERMINATED_WITHOUT_MARKER = "PROCESS_TERMINATED_WITHOUT_MARKER"
MARKER_INVALID = "INVALID_FINAL_MARKER"
MARKER_CAPTURE_FAILED = "MARKER_CAPTURE_FAILED"


class ProcessBoundaryError(RuntimeError):
    """A precise per-run process boundary could not be established."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CodexRunResult:
    exit_code: int
    final_message: str
    stdout_tail: str
    stderr_tail: str
    marker: dict[str, Any] | None
    launched_at: str
    final_marker_detected_at: str | None
    process_exited_at: str | None
    wrapper_pid: int
    stdout_log_path: Path
    stderr_log_path: Path
    process_scope: str
    codex_root_pid: int | None = None
    forced_cleanup: bool = False
    forced_cleanup_after_final: bool = False
    cleanup_error: str | None = None
    timed_out: bool = False
    network_interrupted: bool = False
    runtime_error: str | None = None
    contract_error: str | None = None
    marker_result: str = MARKER_PROCESS_EXITED_WITHOUT_MARKER
    termination_reason: str | None = None
    log_threshold_exceeded: bool = False
    log_threshold_diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class _MarkerProbe:
    state: str
    fingerprint: tuple[int, int, str] | None = None
    text: str = ""
    marker: dict[str, Any] | None = None
    error: str | None = None


class _PosixProcessGroup:
    name = "posix_process_group"

    def assign(self, process: subprocess.Popen[Any]) -> None:
        self.pgid = process.pid

    def active_process_count(self, process: subprocess.Popen[Any]) -> int:
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return 1
        return 1

    def terminate(self) -> None:
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        return None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("Priority", wintypes.DWORD),
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

    class _JobObjectBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
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
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _WindowsPerRunJob:
        """A nested, per-run Job Object beneath the launcher's lifetime Job."""

        name = "windows_per_run_job"

        def __init__(self) -> None:
            self.handle = _kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
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
            except BaseException:
                self.close()
                raise

        def assign(self, process: subprocess.Popen[Any]) -> None:
            # Windows 8+ supports nested jobs.  The launcher Job remains the
            # outer kill-on-close boundary; this Job scopes only this Codex run.
            if not _kernel32.AssignProcessToJobObject(self.handle, process._handle):
                code = ctypes.get_last_error()
                raise ProcessBoundaryError(
                    "Could not assign Codex wrapper to the per-run Windows Job "
                    f"(WinError {code}); refusing an unbounded execution"
                )

        def active_process_count(self, process: subprocess.Popen[Any]) -> int:
            info = _JobObjectBasicAccountingInformation()
            if not _kernel32.QueryInformationJobObject(
                self.handle,
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(info.ActiveProcesses)

        def terminate(self) -> None:
            if not _kernel32.TerminateJobObject(self.handle, 0xE0000001):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            if getattr(self, "handle", None):
                _kernel32.CloseHandle(self.handle)
                self.handle = None


def _create_process_scope() -> Any:
    if os.name == "nt":
        return _WindowsPerRunJob()
    return _PosixProcessGroup()


def _popen_options() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        }
    return {"start_new_session": True}


def _read_tail(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _probe_marker(
    path: Path,
    parser: MarkerParser | None,
    max_bytes: int,
) -> _MarkerProbe:
    if parser is None:
        return _MarkerProbe("PARSER_DISABLED")
    try:
        stat = path.stat()
    except FileNotFoundError:
        return _MarkerProbe("ABSENT")
    except OSError as exc:
        return _MarkerProbe(
            MARKER_CAPTURE_FAILED,
            error=f"could not stat final-message file: {type(exc).__name__}",
        )
    if stat.st_size <= 0:
        return _MarkerProbe("EMPTY")
    if stat.st_size > max_bytes:
        return _MarkerProbe(
            MARKER_CAPTURE_FAILED,
            error=(
                f"final-message file exceeded {max_bytes} bytes "
                f"({stat.st_size} bytes)"
            ),
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _MarkerProbe(
            MARKER_CAPTURE_FAILED,
            error=f"could not read final-message file: {type(exc).__name__}",
        )
    text = raw.decode("utf-8", errors="replace").strip()
    marker = parser(text)
    if marker is None:
        return _MarkerProbe(MARKER_INVALID, text=text)
    fingerprint = (len(raw), stat.st_mtime_ns, hashlib.sha256(raw).hexdigest())
    return _MarkerProbe(
        "VALID_CANDIDATE",
        fingerprint=fingerprint,
        text=text,
        marker=marker,
    )


def _bounded_cleanup(
    process: subprocess.Popen[Any],
    scope: Any,
    timeout_seconds: float,
) -> str | None:
    errors: list[str] = []
    try:
        scope.terminate()
    except BaseException as exc:
        errors.append(f"tree termination failed: {type(exc).__name__}: {exc}")
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        return_code = process.poll()
        try:
            active = scope.active_process_count(process)
            if active == 0 and return_code is not None:
                return "; ".join(errors) or None
        except BaseException as exc:
            errors.append(f"tree query failed: {type(exc).__name__}: {exc}")
            break
        time.sleep(0.05)
    try:
        active = scope.active_process_count(process)
    except BaseException:
        active = -1
    errors.append(f"process tree did not drain within {timeout_seconds:.3f}s (active={active})")
    return "; ".join(dict.fromkeys(errors))


def run_codex_process(
    *,
    args: list[str],
    workdir: Path,
    output_file: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
    prompt: str,
    execution_timeout_seconds: float,
    final_grace_timeout_seconds: float,
    cleanup_timeout_seconds: float,
    marker_stable_seconds: float,
    poll_interval_seconds: float,
    max_log_bytes: int,
    max_final_message_bytes: int,
    marker_parser: MarkerParser | None,
    stop_check: Callable[[], bool] | None = None,
    guard_check: GuardCheck | None = None,
    guard_interval_seconds: float = 10.0,
    event_logger: EventLogger | None = None,
    process_launcher: ProcessLauncher | None = None,
) -> CodexRunResult:
    """Run Codex without PIPE EOF dependencies and always return boundedly."""

    if process_launcher is not None and (
        getattr(process_launcher, "is_contained", False) is not True
        or not callable(getattr(process_launcher, "launch", None))
    ):
        raise ValueError("process launcher is not an accepted contained boundary")

    emit = event_logger or (lambda _event, _fields: None)
    for path in (output_file, stdout_log_path, stderr_log_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"Per-run output path already exists: {path}")

    scope = _create_process_scope()
    process: subprocess.Popen[Any] | None = None
    launched_at = _now_iso()
    detected_at: str | None = None
    process_exited_at: str | None = None
    marker: dict[str, Any] | None = None
    final_message = ""
    forced_cleanup = False
    forced_cleanup_after_final = False
    cleanup_error: str | None = None
    timed_out = False
    network_interrupted = False
    runtime_error: str | None = None
    marker_result = MARKER_PROCESS_EXITED_WITHOUT_MARKER
    termination_reason: str | None = None
    log_threshold_exceeded = False
    log_threshold_diagnostic: str | None = None
    exit_observed = False
    codex_root_pid: int | None = None
    stable_candidate: tuple[int, int, str] | None = None
    stable_since: float | None = None
    root_exit_seen_at: float | None = None

    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as prompt_file, \
            stdout_log_path.open("xb") as stdout_log, \
            stderr_log_path.open("xb") as stderr_log:
            prompt_file.write(prompt)
            prompt_file.seek(0)
            gate_file = output_file.with_name("launch.gate")
            codex_pid_file = output_file.with_name("codex-root.pid")
            for control_path in (gate_file, codex_pid_file):
                if control_path.exists():
                    raise FileExistsError(
                        f"Per-run control path already exists: {control_path}"
                    )
            gate_script = Path(__file__).with_name("codex_process_gate.py")
            gated_args = [
                os.fspath(Path(sys.executable)),
                os.fspath(gate_script),
                os.fspath(gate_file),
                os.fspath(codex_pid_file),
                "--",
                *args,
            ]
            command_digest = hashlib.sha256(
                "\0".join(gated_args).encode("utf-8", errors="replace")
            ).hexdigest()
            emit(
                "process_create_intent",
                {
                    "command_digest": command_digest,
                    "process_scope": scope.name,
                    "launched_at": launched_at,
                    "contained": process_launcher is not None,
                },
            )
            launch_options = _popen_options()
            if process_launcher is None:
                process = subprocess.Popen(
                    gated_args,
                    cwd=str(workdir),
                    stdin=prompt_file,
                    stdout=stdout_log,
                    stderr=stderr_log,
                    shell=False,
                    **launch_options,
                )
            else:
                process = process_launcher.launch(
                    gated_args,
                    cwd=workdir,
                    stdin=prompt_file,
                    stdout=stdout_log,
                    stderr=stderr_log,
                    options=launch_options,
                )
            try:
                scope.assign(process)
            except BaseException:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except BaseException:
                    pass
                raise
            gate_file.write_bytes(b"go")

            emit(
                "process_launch",
                {
                    "wrapper_pid": process.pid,
                    "launched_at": launched_at,
                    "process_scope": scope.name,
                    "stdout_log_path": str(stdout_log_path),
                    "stderr_log_path": str(stderr_log_path),
                },
            )
            started = time.monotonic()
            execution_deadline = started + max(0.1, execution_timeout_seconds)
            grace_deadline: float | None = None
            next_guard_check = started + max(0.1, guard_interval_seconds)
            post_exit_settle = max(0.25, marker_stable_seconds * 2)

            while True:
                now = time.monotonic()
                if codex_root_pid is None and codex_pid_file.exists():
                    try:
                        codex_root_pid = int(
                            codex_pid_file.read_text(encoding="ascii").strip()
                        )
                    except (OSError, ValueError):
                        pass
                    else:
                        emit(
                            "process_created",
                            {
                                "wrapper_pid": process.pid,
                                "codex_root_pid": codex_root_pid,
                                "process_scope": scope.name,
                                "contained": process_launcher is not None,
                            },
                        )
                        emit("codex_root_detected", {"codex_root_pid": codex_root_pid})
                return_code = process.poll()
                if return_code is not None and not exit_observed:
                    exit_observed = True
                    process_exited_at = _now_iso()
                    root_exit_seen_at = now
                    emit(
                        "process_exit",
                        {"exit_code": return_code, "process_exited_at": process_exited_at},
                    )

                probe = _probe_marker(
                    output_file, marker_parser, max_final_message_bytes
                )
                if probe.state != "VALID_CANDIDATE":
                    stable_candidate = None
                    stable_since = None
                else:
                    assert probe.fingerprint is not None
                    assert probe.marker is not None
                    fingerprint = probe.fingerprint
                    if fingerprint != stable_candidate:
                        stable_candidate = fingerprint
                        stable_since = now
                    elif (
                        detected_at is None
                        and stable_since is not None
                        and now - stable_since >= marker_stable_seconds
                    ):
                        marker = probe.marker
                        final_message = probe.text
                        marker_result = MARKER_VALID
                        detected_at = _now_iso()
                        grace_deadline = now + max(0.1, final_grace_timeout_seconds)
                        emit(
                            "final_marker_detected",
                            {
                                "final_marker_detected_at": detected_at,
                                "marker_status": marker.get("status", "unknown"),
                            },
                        )
                        emit(
                            "graceful_cleanup_start",
                            {"grace_timeout_seconds": final_grace_timeout_seconds},
                        )

                try:
                    active_count = scope.active_process_count(process)
                except BaseException as exc:
                    runtime_error = f"process scope query failed: {type(exc).__name__}: {exc}"
                    active_count = 1

                if not log_threshold_exceeded:
                    oversized: list[str] = []
                    for log_path in (stdout_log_path, stderr_log_path):
                        try:
                            log_size = log_path.stat().st_size
                        except OSError:
                            continue
                        if log_size > max_log_bytes:
                            oversized.append(f"{log_path.name}={log_size}")
                    if oversized:
                        log_threshold_exceeded = True
                        log_threshold_diagnostic = (
                            f"log reporting threshold {max_log_bytes} bytes exceeded: "
                            + ", ".join(oversized)
                            + "; execution continued"
                        )
                        emit(
                            "log_threshold_exceeded",
                            {
                                "threshold_bytes": max_log_bytes,
                                "logs": ",".join(oversized),
                                "action": "execution_continued",
                            },
                        )

                if detected_at is not None and active_count == 0:
                    break

                if detected_at is not None and grace_deadline is not None and now >= grace_deadline:
                    forced_cleanup = True
                    forced_cleanup_after_final = True
                    termination_reason = "final_grace_expired"
                    emit(
                        "forced_cleanup_start",
                        {"reason": "final_grace_expired", "active_processes": active_count},
                    )
                    cleanup_error = _bounded_cleanup(
                        process, scope, cleanup_timeout_seconds
                    )
                    break

                if detected_at is None and return_code is not None:
                    if root_exit_seen_at is not None and now - root_exit_seen_at >= post_exit_settle:
                        if active_count > 0:
                            forced_cleanup = True
                            termination_reason = "root_exited_with_descendants"
                            emit(
                                "forced_cleanup_start",
                                {"reason": "root_exited_with_descendants", "active_processes": active_count},
                            )
                            cleanup_error = _bounded_cleanup(
                                process, scope, cleanup_timeout_seconds
                            )
                        break

                if detected_at is None and stop_check is not None and stop_check():
                    termination_reason = "owner_stop"
                    runtime_error = "Owner requested this exact run to stop"
                    forced_cleanup = True
                    emit("owner_stop_accepted", {})
                    cleanup_error = _bounded_cleanup(process, scope, cleanup_timeout_seconds)
                    break

                if now >= execution_deadline and detected_at is None:
                    timed_out = True
                    runtime_error = (
                        f"Codex execution timed out after {execution_timeout_seconds:.3f} seconds"
                    )
                    forced_cleanup = True
                    termination_reason = "execution_timeout"
                    emit(
                        "forced_cleanup_start",
                        {"reason": "execution_timeout", "active_processes": active_count},
                    )
                    cleanup_error = _bounded_cleanup(
                        process, scope, cleanup_timeout_seconds
                    )
                    break

                if (
                    detected_at is None
                    and guard_check is not None
                    and now >= next_guard_check
                ):
                    allowed, reason = guard_check()
                    next_guard_check = now + max(0.1, guard_interval_seconds)
                    if not allowed:
                        network_interrupted = True
                        runtime_error = f"network guard became unsafe: {reason}"
                        forced_cleanup = True
                        termination_reason = "network_guard"
                        emit(
                            "forced_cleanup_start",
                            {"reason": "network_guard", "active_processes": active_count},
                        )
                        cleanup_error = _bounded_cleanup(
                            process, scope, cleanup_timeout_seconds
                        )
                        break

                time.sleep(max(0.02, poll_interval_seconds))
    finally:
        if process is not None:
            process.poll()
        scope.close()

    assert process is not None
    process.poll()
    if process.returncode is not None and not exit_observed:
        process_exited_at = _now_iso()
        emit(
            "process_exit",
            {"exit_code": process.returncode, "process_exited_at": process_exited_at},
        )
    exit_code = process.returncode if process.returncode is not None else 125
    final_probe = _probe_marker(output_file, marker_parser, max_final_message_bytes)
    if termination_reason == "owner_stop":
        marker = None
        final_message = ""
        marker_result = MARKER_PROCESS_TERMINATED_WITHOUT_MARKER
    elif marker is None and final_probe.state == "VALID_CANDIDATE":
        # Once the process boundary is drained, a valid final file cannot still
        # be a partial write. Accept it even if the normal stability poll lost a
        # race with process exit or bounded cleanup.
        assert final_probe.marker is not None
        marker = final_probe.marker
        final_message = final_probe.text
        marker_result = MARKER_VALID
        detected_at = detected_at or _now_iso()
        emit(
            "final_marker_detected",
            {
                "final_marker_detected_at": detected_at,
                "marker_status": marker.get("status", "unknown"),
                "detection_phase": "post_process_drain",
            },
        )
    elif marker is None:
        if final_probe.state == MARKER_CAPTURE_FAILED:
            marker_result = MARKER_CAPTURE_FAILED
            runtime_error = final_probe.error or "final-message capture failed"
        elif final_probe.state == MARKER_INVALID:
            marker_result = MARKER_INVALID
            final_message = final_probe.text
        elif termination_reason in {"execution_timeout", "network_guard"}:
            marker_result = MARKER_PROCESS_TERMINATED_WITHOUT_MARKER
        else:
            marker_result = MARKER_PROCESS_EXITED_WITHOUT_MARKER

    if not log_threshold_exceeded:
        oversized: list[str] = []
        for log_path in (stdout_log_path, stderr_log_path):
            try:
                log_size = log_path.stat().st_size
            except OSError:
                continue
            if log_size > max_log_bytes:
                oversized.append(f"{log_path.name}={log_size}")
        if oversized:
            log_threshold_exceeded = True
            log_threshold_diagnostic = (
                f"log reporting threshold {max_log_bytes} bytes exceeded: "
                + ", ".join(oversized)
                + "; execution continued"
            )
            emit(
                "log_threshold_exceeded",
                {
                    "threshold_bytes": max_log_bytes,
                    "logs": ",".join(oversized),
                    "action": "execution_continued",
                    "detection_phase": "final",
                },
            )
    return CodexRunResult(
        exit_code=exit_code,
        final_message=final_message,
        stdout_tail=_read_tail(stdout_log_path, 16_384),
        stderr_tail=_read_tail(stderr_log_path, 16_384),
        marker=marker,
        launched_at=launched_at,
        final_marker_detected_at=detected_at,
        process_exited_at=process_exited_at,
        wrapper_pid=process.pid,
        codex_root_pid=codex_root_pid,
        stdout_log_path=stdout_log_path,
        stderr_log_path=stderr_log_path,
        process_scope=scope.name,
        forced_cleanup=forced_cleanup,
        forced_cleanup_after_final=forced_cleanup_after_final,
        cleanup_error=cleanup_error,
        timed_out=timed_out,
        network_interrupted=network_interrupted,
        runtime_error=runtime_error,
        marker_result=marker_result,
        termination_reason=termination_reason,
        log_threshold_exceeded=log_threshold_exceeded,
        log_threshold_diagnostic=log_threshold_diagnostic,
    )
