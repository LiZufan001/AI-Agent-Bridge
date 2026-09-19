#!/usr/bin/env python3
"""Bounded, invocation-owned execution for local maintenance commands.

This module is an operator boundary.  It is not used by the Worker execution
path.  On Windows an inert Python shim is assigned to a fresh Job Object before
it may start the requested command.  Every descendant therefore inherits the
same non-breakaway, kill-on-close ownership boundary.
"""

from __future__ import annotations

import argparse
import base64
import collections
import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 1
MIB = 1024 * 1024
DEFAULT_SOFT_PRIVATE_BYTES = 512 * MIB
DEFAULT_HARD_PRIVATE_BYTES = 1024 * MIB
DEFAULT_HARD_WORKING_SET_BYTES = 1536 * MIB
_SECRET_ARG_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[-_]?key|authorization|credential)"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    data = _canonical(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _deadline_utc(seconds: float) -> str:
    return datetime.fromtimestamp(
        time.time() + seconds, tz=timezone.utc
    ).isoformat(timespec="microseconds")


def read_bounded_bytes(path: Path, max_bytes: int, *, tail: bool = False) -> bytes:
    """Read at most ``max_bytes`` after proving the file size first."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    size = path.stat().st_size
    if size > max_bytes and not tail:
        raise ValueError("file exceeds bounded read limit")
    with path.open("rb") as stream:
        if tail and size > max_bytes:
            stream.seek(size - max_bytes)
        data = stream.read(max_bytes + 1)
        if not tail and len(data) > max_bytes:
            raise ValueError("file grew beyond bounded read limit")
        return data[:max_bytes]


@dataclass(frozen=True, slots=True)
class MaintenanceLimits:
    timeout_seconds: float = 60.0
    poll_seconds: float = 0.05
    cleanup_seconds: float = 5.0
    soft_private_bytes: int = DEFAULT_SOFT_PRIVATE_BYTES
    hard_private_bytes: int = DEFAULT_HARD_PRIVATE_BYTES
    hard_working_set_bytes: int = DEFAULT_HARD_WORKING_SET_BYTES
    stdout_bytes: int = 256 * 1024
    stderr_bytes: int = 256 * 1024
    checkpoint_count: int = 32

    def checked(self) -> "MaintenanceLimits":
        if not (0 < self.poll_seconds <= self.timeout_seconds <= 24 * 60 * 60):
            raise ValueError("invalid maintenance timeout")
        if not (0 < self.cleanup_seconds <= 60):
            raise ValueError("invalid cleanup timeout")
        if not (
            0 < self.soft_private_bytes < self.hard_private_bytes
            and self.hard_working_set_bytes > 0
        ):
            raise ValueError("invalid memory limits")
        if not (1024 <= self.stdout_bytes <= 16 * MIB):
            raise ValueError("invalid stdout limit")
        if not (1024 <= self.stderr_bytes <= 16 * MIB):
            raise ValueError("invalid stderr limit")
        if not (1 <= self.checkpoint_count <= 256):
            raise ValueError("invalid checkpoint retention")
        return self


@dataclass(slots=True)
class _BoundedStream:
    limit: int
    total_bytes: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    tail: bytearray = field(default_factory=bytearray)

    def add(self, block: bytes) -> None:
        self.total_bytes += len(block)
        self.digest.update(block)
        if len(block) >= self.limit:
            self.tail[:] = block[-self.limit :]
            return
        overflow = len(self.tail) + len(block) - self.limit
        if overflow > 0:
            del self.tail[:overflow]
        self.tail.extend(block)

    def evidence(self) -> dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "retained_bytes": len(self.tail),
            "truncated": self.total_bytes > len(self.tail),
            "sha256": self.digest.hexdigest(),
            "tail": bytes(self.tail).decode("utf-8", errors="replace"),
        }


class _PipeReader(threading.Thread):
    def __init__(self, stream: Any, capture: _BoundedStream) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.capture = capture

    def run(self) -> None:
        try:
            while True:
                block = self.stream.read(64 * 1024)
                if not block:
                    return
                self.capture.add(block)
        finally:
            self.stream.close()


class _OwnedBoundary:
    """Small platform boundary used only for the exact launched root."""

    def __init__(self) -> None:
        self._job: Any = None
        self._process_group: int | None = None

    def create(self, root_pid: int, hard_private_bytes: int) -> None:
        if os.name != "nt":
            self._process_group = root_pid
            return
        self._job = _WindowsJob(hard_private_bytes)
        self._job.assign(root_pid)

    def process_ids(self, root_pid: int) -> tuple[int, ...]:
        if os.name == "nt":
            return self._job.process_ids() if self._job is not None else ()
        try:
            os.kill(root_pid, 0)
        except OSError:
            return ()
        return (root_pid,)

    def memory(self, root_pid: int) -> tuple[int, int]:
        if os.name == "nt":
            private = 0
            working_set = 0
            for pid in self.process_ids(root_pid):
                sample = _windows_process_memory(pid)
                private += sample[0]
                working_set += sample[1]
            return private, working_set
        return (0, 0)

    def terminate(self, root_pid: int) -> None:
        if os.name == "nt":
            if self._job is not None:
                self._job.terminate()
            return
        try:
            os.killpg(root_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JobObjectExtendedLimitInformation = 9
    _JobObjectBasicProcessIdList = 3

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
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
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD
    ]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    class _WindowsJob:
        def __init__(self, hard_private_bytes: int) -> None:
            self.handle = _kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            info.JobMemoryLimit = hard_private_bytes
            if not _kernel32.SetInformationJobObject(
                self.handle,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                error = ctypes.get_last_error()
                self.close()
                raise OSError(error, "SetInformationJobObject failed")

        def assign(self, pid: int) -> None:
            process = _kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | 0x0100 | 0x0001, False, pid
            )
            if not process:
                raise OSError(ctypes.get_last_error(), "OpenProcess failed")
            try:
                if not _kernel32.AssignProcessToJobObject(self.handle, process):
                    raise OSError(
                        ctypes.get_last_error(), "AssignProcessToJobObject failed"
                    )
            finally:
                _kernel32.CloseHandle(process)

        def process_ids(self) -> tuple[int, ...]:
            capacity = 32
            while capacity <= 4096:
                array_type = ctypes.c_size_t * capacity
                class _PID_LIST(ctypes.Structure):
                    _fields_ = [
                        ("assigned", wintypes.DWORD),
                        ("count", wintypes.DWORD),
                        ("pids", array_type),
                    ]
                value = _PID_LIST()
                if _kernel32.QueryInformationJobObject(
                    self.handle,
                    _JobObjectBasicProcessIdList,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                    None,
                ):
                    return tuple(int(value.pids[index]) for index in range(value.count))
                if ctypes.get_last_error() != 234:
                    raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
                capacity *= 2
            raise RuntimeError("owned process tree exceeds PID evidence limit")

        def terminate(self) -> None:
            if not _kernel32.TerminateJobObject(self.handle, 125):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

        def close(self) -> None:
            if self.handle:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

    def _windows_process_memory(pid: int) -> tuple[int, int]:
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid
        )
        if not handle:
            return (0, 0)
        try:
            counters = _PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not _psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            ):
                return (0, 0)
            return (int(counters.PrivateUsage), int(counters.WorkingSetSize))
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_process_identity(pid: int) -> tuple[str, int] | None:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            unix_seconds = ticks / 10_000_000 - 11_644_473_600
            timestamp = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat(
                timespec="microseconds"
            )
            return timestamp, ticks
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_process_is_active(
        pid: int, creation_ticks: int | None = None
    ) -> bool:
        identity = _windows_process_identity(pid)
        if identity is None or (
            creation_ticks is not None and identity[1] != creation_ticks
        ):
            return False
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | 0x00100000, False, pid
        )
        if not handle:
            return False
        try:
            return _kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            _kernel32.CloseHandle(handle)

else:
    class _WindowsJob:  # pragma: no cover - import compatibility
        pass

    def _windows_process_memory(pid: int) -> tuple[int, int]:
        return (0, 0)

    def _windows_process_identity(pid: int) -> tuple[str, int] | None:
        return None

    def _windows_process_is_active(
        pid: int, creation_ticks: int | None = None
    ) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


MetricSampler = Callable[[tuple[int, ...]], tuple[int, int]]
CancelCheck = Callable[[], bool]


def _safe_argv(argv: Sequence[str], sensitive_indices: frozenset[int]) -> list[str]:
    safe: list[str] = []
    redact_next = False
    for index, value in enumerate(argv):
        text = str(value)
        secret = index in sensitive_indices or redact_next
        redact_next = bool(_SECRET_ARG_RE.search(text)) and "=" not in text
        if secret or (_SECRET_ARG_RE.search(text) and "=" in text):
            safe.append("<redacted>")
        else:
            safe.append(text[:512] + ("<truncated>" if len(text) > 512 else ""))
    return safe


def run_maintenance_command(
    argv: Sequence[str],
    *,
    command_class: str,
    cwd: Path | None = None,
    limits: MaintenanceLimits | None = None,
    cancel_check: CancelCheck | None = None,
    metric_sampler: MetricSampler | None = None,
    sensitive_indices: frozenset[int] = frozenset(),
    stdin_data: bytes | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one command in an exact, bounded ownership boundary."""

    checked = (limits or MaintenanceLimits()).checked()
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("argv must contain non-empty strings")
    if stdin_data is not None and len(stdin_data) > 64 * 1024:
        raise ValueError("maintenance stdin exceeds 64 KiB")
    overrides = dict(env_overrides or {})
    if len(overrides) > 32 or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or len(key) > 128
        or len(value) > 4096
        for key, value in overrides.items()
    ):
        raise ValueError("maintenance environment overrides invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", command_class):
        raise ValueError("invalid command class")
    execution_id = str(uuid.uuid4())
    safe_argv = _safe_argv(argv, sensitive_indices)
    argv_digest = hashlib.sha256(_canonical({"argv": list(argv)})).hexdigest()
    started_at = _utc_now()
    stdout = _BoundedStream(checked.stdout_bytes)
    stderr = _BoundedStream(checked.stderr_bytes)
    checkpoints: collections.deque[dict[str, Any]] = collections.deque(
        maxlen=checked.checkpoint_count
    )
    first_failure: dict[str, Any] | None = None
    boundary = _OwnedBoundary()
    process: subprocess.Popen[bytes] | None = None
    peak_private = 0
    peak_working_set = 0
    warning_emitted = False
    residual_tree_observed = False
    output_drain_failed = False
    observed_processes: dict[tuple[int, int], str] = {}
    exact_active_after_close: list[dict[str, Any]] = []
    launch_time = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="bridge-maintenance-") as temporary:
        root = Path(temporary)
        spec = root / "shim.json"
        gate = root / "start.gate"
        parent_identity = _windows_process_identity(os.getpid())
        _write_json(
            spec,
            {
                "schema_version": SCHEMA_VERSION,
                "argv": list(argv),
                "cwd": str(cwd) if cwd is not None else None,
                "gate": str(gate),
                "deadline_monotonic": launch_time + checked.timeout_seconds,
                "parent_pid": os.getpid(),
                "parent_creation_ticks": (
                    parent_identity[1] if parent_identity is not None else None
                ),
                "stdin_base64": (
                    base64.b64encode(stdin_data).decode("ascii")
                    if stdin_data is not None else None
                ),
                "env_overrides": overrides,
            },
        )
        shim = [sys.executable, str(Path(__file__).resolve()), "--owned-shim", str(spec)]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        start_new_session = os.name != "nt"
        process = subprocess.Popen(
            shim,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        root_pid = process.pid
        identity = _windows_process_identity(root_pid)
        root_created_at = identity[0] if identity is not None else _utc_now()
        stdout_reader = _PipeReader(process.stdout, stdout)
        stderr_reader = _PipeReader(process.stderr, stderr)
        stdout_reader.start()
        stderr_reader.start()
        termination_reason: str | None = None
        cleanup_error: str | None = None
        try:
            boundary.create(root_pid, checked.hard_private_bytes)
            gate.touch(exist_ok=False)
            while process.poll() is None:
                now = time.monotonic()
                pids = boundary.process_ids(root_pid)
                private, working_set = (
                    metric_sampler(pids) if metric_sampler is not None
                    else boundary.memory(root_pid)
                )
                peak_private = max(peak_private, private)
                peak_working_set = max(peak_working_set, working_set)
                process_identities = []
                for pid in pids:
                    observed_identity = _windows_process_identity(pid)
                    if observed_identity is not None:
                        observed_processes[(pid, observed_identity[1])] = (
                            observed_identity[0]
                        )
                    process_identities.append(
                        {
                            "pid": pid,
                            "created_at": (
                                observed_identity[0] if observed_identity else None
                            ),
                            "creation_ticks": (
                                observed_identity[1] if observed_identity else None
                            ),
                        }
                    )
                checkpoints.append(
                    {
                        "elapsed_ms": int((now - launch_time) * 1000),
                        "active_pids": list(pids),
                        "active_processes": process_identities,
                        "private_bytes": private,
                        "working_set_bytes": working_set,
                    }
                )
                if private >= checked.soft_private_bytes:
                    warning_emitted = True
                if private >= checked.hard_private_bytes:
                    termination_reason = "PRIVATE_BYTES_LIMIT"
                elif working_set >= checked.hard_working_set_bytes:
                    termination_reason = "WORKING_SET_LIMIT"
                elif cancel_check is not None and cancel_check():
                    termination_reason = "CANCELLED"
                elif now - launch_time >= checked.timeout_seconds:
                    termination_reason = "TIMEOUT"
                if termination_reason is not None:
                    first_failure = {
                        "reason": termination_reason,
                        "observed_at": _utc_now(),
                        "private_bytes": private,
                        "working_set_bytes": working_set,
                        "active_pids": list(pids),
                    }
                    boundary.terminate(root_pid)
                    break
                time.sleep(checked.poll_seconds)
            try:
                process.wait(timeout=checked.cleanup_seconds)
            except subprocess.TimeoutExpired:
                if termination_reason is None:
                    termination_reason = "CLEANUP_TIMEOUT"
                    first_failure = {
                        "reason": termination_reason,
                        "observed_at": _utc_now(),
                    }
                boundary.terminate(root_pid)
                process.wait(timeout=checked.cleanup_seconds)
        except BaseException as exc:
            if first_failure is None:
                first_failure = {
                    "reason": "WRAPPER_EXCEPTION",
                    "exception_class": type(exc).__name__,
                    "observed_at": _utc_now(),
                }
            try:
                boundary.terminate(root_pid)
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=checked.cleanup_seconds)
            except BaseException as cleanup_exc:
                cleanup_error = type(cleanup_exc).__name__
            raise
        finally:
            active = boundary.process_ids(root_pid)
            drain_deadline = time.monotonic() + checked.cleanup_seconds
            while active and time.monotonic() < drain_deadline:
                time.sleep(min(checked.poll_seconds, 0.05))
                active = boundary.process_ids(root_pid)
            if active:
                residual_tree_observed = True
                boundary.terminate(root_pid)
                termination_deadline = time.monotonic() + checked.cleanup_seconds
                while active and time.monotonic() < termination_deadline:
                    time.sleep(min(checked.poll_seconds, 0.05))
                    active = boundary.process_ids(root_pid)
            boundary.close()
            exact_deadline = time.monotonic() + checked.cleanup_seconds
            while True:
                exact_active_after_close = [
                    {
                        "pid": pid,
                        "created_at": observed_processes[(pid, creation_ticks)],
                        "creation_ticks": creation_ticks,
                    }
                    for pid, creation_ticks in observed_processes
                    if _windows_process_is_active(pid, creation_ticks)
                ]
                if not exact_active_after_close or time.monotonic() >= exact_deadline:
                    break
                time.sleep(min(checked.poll_seconds, 0.05))
            if exact_active_after_close:
                residual_tree_observed = True
                cleanup_error = cleanup_error or "EXACT_PROCESS_DRAIN_TIMEOUT"
            stdout_reader.join(timeout=checked.cleanup_seconds)
            stderr_reader.join(timeout=checked.cleanup_seconds)
            if stdout_reader.is_alive() or stderr_reader.is_alive():
                output_drain_failed = True
                cleanup_error = "OUTPUT_DRAIN_TIMEOUT"
        exit_code = process.returncode
    completed_at = _utc_now()
    active_children = max(len(active), len(exact_active_after_close))
    result_code = "SUCCESS"
    if active_children or residual_tree_observed:
        result_code = "OPERATOR_CHILD_LEAK"
    elif termination_reason is not None:
        result_code = termination_reason
    elif output_drain_failed:
        result_code = "OUTPUT_DRAIN_TIMEOUT"
    elif exit_code != 0:
        result_code = "CHILD_FAILED"
    success = result_code == "SUCCESS"
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": execution_id,
        "command_class": command_class,
        "argv_sha256": argv_digest,
        "safe_argv": safe_argv,
        "root_pid": root_pid,
        "root_created_at": root_created_at,
        "parent_pid": os.getpid(),
        "started_at": started_at,
        "completed_at": completed_at,
        "exit_code": exit_code,
        "result": result_code,
        "success": success,
        "first_failure": first_failure,
        "cleanup_error": cleanup_error,
        "operator_owned_active_children": active_children,
        "operator_owned_orphans": active_children,
        "exact_active_after_close": exact_active_after_close,
        "memory": {
            "metric": "sum of exact Job PIDs: PrivateUsage and WorkingSetSize",
            "soft_private_bytes": checked.soft_private_bytes,
            "hard_private_bytes": checked.hard_private_bytes,
            "hard_working_set_bytes": checked.hard_working_set_bytes,
            "soft_warning_observed": warning_emitted,
            "peak_private_bytes": peak_private,
            "peak_working_set_bytes": peak_working_set,
        },
        "stdout": stdout.evidence(),
        "stderr": stderr.evidence(),
        "checkpoints": list(checkpoints),
        "checkpoint_retention_limit": checked.checkpoint_count,
    }


def _parent_identity_alive(pid: int, creation_ticks: int | None) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_active(pid, creation_ticks)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _limits_payload(limits: MaintenanceLimits) -> dict[str, Any]:
    return {
        name: getattr(limits, name)
        for name in MaintenanceLimits.__dataclass_fields__
    }


_BROKER_TERMINAL_CAUSES = frozenset(
    {
        "TIMEOUT",
        "CANCELLED",
        "PRIVATE_BYTES_LIMIT",
        "WORKING_SET_LIMIT",
    }
)


def _trusted_broker_terminal_cause(
    broker: Mapping[str, Any],
) -> str | None:
    """Return only an explicitly corroborated operator terminal cause."""

    if broker.get("success") is not False:
        return None
    result = broker.get("result")
    if not isinstance(result, str) or result not in _BROKER_TERMINAL_CAUSES:
        return None
    first_failure = broker.get("first_failure")
    if not isinstance(first_failure, Mapping):
        return None
    if first_failure.get("reason") != result:
        return None
    return result


def _missing_wrapper_result(
    broker: Mapping[str, Any],
    cleanup_failure: str | None,
) -> tuple[str, str]:
    """Classify a missing wrapper result without widening broker propagation."""

    if cleanup_failure is not None:
        return "OPERATOR_CHILD_LEAK", "cleanup-failure"
    if _trusted_broker_terminal_cause(broker) is not None:
        return str(broker["result"]), "broker-terminal-fallback"
    return "ELEVATED_WRAPPER_FAILED", "wrapper-result-missing"


def run_elevated_maintenance_command(
    argv: Sequence[str],
    *,
    command_class: str,
    evidence_directory: Path,
    cwd: Path | None = None,
    limits: MaintenanceLimits | None = None,
    cancel_check: CancelCheck | None = None,
    sensitive_indices: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """Run through a self-terminating elevated wrapper with an exact PID handle.

    The wrapper owns the command's Job Object.  It observes both an absolute
    deadline and the exact parent PID/creation identity, so parent disappearance
    propagates cancellation even when the parent cannot execute ``finally``.
    """

    if os.name != "nt":
        raise OSError("elevation boundary is Windows-only")
    checked = (limits or MaintenanceLimits()).checked()
    evidence_directory.mkdir(parents=True, exist_ok=True)
    execution_id = str(uuid.uuid4())
    spec_path = evidence_directory / f"{execution_id}.elevated-spec.json"
    result_path = evidence_directory / f"{execution_id}.elevated-result.json"
    owner_path = evidence_directory / f"{execution_id}.owner.json"
    cancel_path = evidence_directory / f"{execution_id}.cancel"
    started_at = _utc_now()
    deadline = _deadline_utc(checked.timeout_seconds)
    safe_argv = _safe_argv(argv, sensitive_indices)
    _write_json(
        spec_path,
        {
            "schema_version": SCHEMA_VERSION,
            "execution_id": execution_id,
            "argv": list(argv),
            "safe_argv": safe_argv,
            "sensitive_indices": sorted(sensitive_indices),
            "command_class": command_class,
            "cwd": str(cwd) if cwd is not None else None,
            "limits": _limits_payload(checked),
            "deadline_utc": deadline,
            "cancel_path": str(cancel_path),
            "result_path": str(result_path),
            "owner_path": str(owner_path),
        },
    )
    cleanup_failure: str | None = None
    try:
        broker = run_maintenance_command(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--elevation-broker",
                str(spec_path),
            ],
            command_class="elevation-broker",
            limits=checked,
            cancel_check=cancel_check,
        )
        owner = (
            json.loads(read_bounded_bytes(owner_path, 64 * 1024))
            if owner_path.exists() else None
        )
        if owner is not None and _parent_identity_alive(
            int(owner["elevated_root_pid"]),
            int(owner["elevated_root_creation_ticks"]),
        ):
            cleanup_deadline = time.monotonic() + checked.cleanup_seconds
            while time.monotonic() < cleanup_deadline and _parent_identity_alive(
                int(owner["elevated_root_pid"]),
                int(owner["elevated_root_creation_ticks"]),
            ):
                time.sleep(checked.poll_seconds)
            if _parent_identity_alive(
                int(owner["elevated_root_pid"]),
                int(owner["elevated_root_creation_ticks"]),
            ):
                cleanup_failure = "ELEVATED_WRAPPER_CLEANUP_TIMEOUT"
                _terminate_exact_process(
                    int(owner["elevated_root_pid"]),
                    int(owner["elevated_root_creation_ticks"]),
                )
        if not result_path.exists():
            result_code, result_source = _missing_wrapper_result(
                broker, cleanup_failure
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "execution_id": execution_id,
                "command_class": command_class,
                "safe_argv": safe_argv,
                "root_pid": owner.get("elevated_root_pid") if owner else None,
                "started_at": started_at,
                "completed_at": _utc_now(),
                "result": result_code,
                "success": False,
                "cleanup_error": cleanup_failure,
                "operator_owned_active_children": 1 if cleanup_failure else 0,
                "operator_owned_orphans": 1 if cleanup_failure else 0,
                "elevated_owner": owner,
                "elevation_broker": broker,
                "wrapper_result_present": False,
                "result_source": result_source,
                "broker_terminal_cause": _trusted_broker_terminal_cause(broker),
            }
        result = json.loads(read_bounded_bytes(result_path, 2 * MIB))
        result["elevated_owner"] = owner
        result["elevation_broker"] = broker
        result["wrapper_result_present"] = True
        result["result_source"] = "wrapper"
        if not broker["success"] and result.get("success"):
            result["success"] = False
            result["result"] = "ELEVATION_BROKER_FAILED"
            result["result_source"] = "broker-failure"
        if cleanup_failure is not None:
            result["success"] = False
            result["result"] = "OPERATOR_CHILD_LEAK"
            result["result_source"] = "cleanup-failure"
            result["cleanup_error"] = cleanup_failure
            result["operator_owned_active_children"] = max(
                1, int(result.get("operator_owned_active_children", 0))
            )
            result["operator_owned_orphans"] = max(
                1, int(result.get("operator_owned_orphans", 0))
            )
        return result
    finally:
        try:
            spec_path.unlink(missing_ok=True)
            cancel_path.unlink(missing_ok=True)
        except OSError:
            pass


if os.name == "nt":
    _SEE_MASK_NOCLOSEPROCESS = 0x00000040
    _SW_HIDE = 0
    _WAIT_TIMEOUT = 0x00000102

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL
    _kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessId.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

    def _shell_execute_elevated(
        executable: str, parameters: Sequence[str], cwd: Path | None
    ) -> tuple[int, int]:
        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = executable
        info.lpParameters = subprocess.list2cmdline(list(parameters))
        info.lpDirectory = str(cwd) if cwd is not None else None
        info.nShow = _SW_HIDE
        if not _shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
            raise OSError(ctypes.get_last_error(), "ShellExecuteExW elevation failed")
        return int(info.hProcess), int(_kernel32.GetProcessId(info.hProcess))

    def _wait_process_handle(handle: int, seconds: float) -> int | None:
        milliseconds = max(0, min(int(seconds * 1000), 0xFFFFFFFE))
        result = _kernel32.WaitForSingleObject(handle, milliseconds)
        if result == _WAIT_TIMEOUT:
            return None
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        return int(exit_code.value)

    def _terminate_process_handle(handle: int, exit_code: int) -> None:
        if not _kernel32.TerminateProcess(handle, exit_code):
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed")

    def _terminate_exact_process(pid: int, creation_ticks: int) -> None:
        identity = _windows_process_identity(pid)
        if identity is None or identity[1] != creation_ticks:
            return
        handle = _kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess terminate failed")
        try:
            _terminate_process_handle(handle, 125)
            _wait_process_handle(handle, 5.0)
        finally:
            _kernel32.CloseHandle(handle)

else:
    def _shell_execute_elevated(
        executable: str, parameters: Sequence[str], cwd: Path | None
    ) -> tuple[int, int]:
        raise OSError("Windows-only")

    def _wait_process_handle(handle: int, seconds: float) -> int | None:
        raise OSError("Windows-only")

    def _terminate_process_handle(handle: int, exit_code: int) -> None:
        raise OSError("Windows-only")

    def _terminate_exact_process(pid: int, creation_ticks: int) -> None:
        raise OSError("Windows-only")


def _elevation_broker(spec_path: Path) -> int:
    """Cancellable ordinary broker for the otherwise blocking UAC launch."""

    spec = json.loads(read_bounded_bytes(spec_path, MIB))
    deadline = datetime.fromisoformat(str(spec["deadline_utc"])).astimezone(timezone.utc)
    parent_identity = _windows_process_identity(os.getpid())
    spec["parent_pid"] = os.getpid()
    spec["parent_creation_ticks"] = parent_identity[1] if parent_identity else None
    _write_json(spec_path, spec)
    process_handle: int | None = None
    try:
        process_handle, elevated_pid = _shell_execute_elevated(
            sys.executable,
            [str(Path(__file__).resolve()), "--elevated-wrapper", str(spec_path)],
            Path(spec["cwd"]) if spec.get("cwd") else None,
        )
        elevated_identity = _windows_process_identity(elevated_pid)
        _write_json(
            Path(spec["owner_path"]),
            {
                "schema_version": SCHEMA_VERSION,
                "execution_id": spec["execution_id"],
                "command_class": spec["command_class"],
                "argv_sha256": hashlib.sha256(
                    _canonical({"argv": list(spec["argv"])})
                ).hexdigest(),
                "safe_argv": spec["safe_argv"],
                "parent_pid": os.getpid(),
                "parent_creation_ticks": parent_identity[1] if parent_identity else None,
                "elevated_root_pid": elevated_pid,
                "elevated_root_created_at": (
                    elevated_identity[0] if elevated_identity else None
                ),
                "elevated_root_creation_ticks": (
                    elevated_identity[1] if elevated_identity else None
                ),
                "started_at": _utc_now(),
                "deadline_utc": spec["deadline_utc"],
            },
        )
        while _wait_process_handle(process_handle, 0) is None:
            if datetime.now(timezone.utc) >= deadline:
                Path(spec["cancel_path"]).touch(exist_ok=True)
                break
            time.sleep(float(spec["limits"]["poll_seconds"]))
        if _wait_process_handle(
            process_handle, float(spec["limits"]["cleanup_seconds"])
        ) is None:
            _terminate_process_handle(process_handle, 125)
        final_exit = _wait_process_handle(
            process_handle, float(spec["limits"]["cleanup_seconds"])
        )
        return 125 if final_exit is None else final_exit
    finally:
        if process_handle is not None:
            _kernel32.CloseHandle(process_handle)


def _elevated_wrapper(spec_path: Path) -> int:
    try:
        spec = json.loads(read_bounded_bytes(spec_path, MIB))
        deadline = datetime.fromisoformat(str(spec["deadline_utc"])).astimezone(timezone.utc)
        cancel_path = Path(spec["cancel_path"])
        parent_pid = int(spec["parent_pid"])
        parent_ticks = spec.get("parent_creation_ticks")
        limits = MaintenanceLimits(**spec["limits"]).checked()
        result_path = Path(spec["result_path"])
        result = run_maintenance_command(
            spec["argv"],
            command_class=str(spec["command_class"]),
            cwd=Path(spec["cwd"]) if spec.get("cwd") else None,
            limits=limits,
            sensitive_indices=frozenset(
                int(item) for item in spec.get("sensitive_indices", [])
            ),
            cancel_check=lambda: (
                cancel_path.exists()
                or datetime.now(timezone.utc) >= deadline
                or not _parent_identity_alive(parent_pid, parent_ticks)
            ),
        )
        _write_json(result_path, result)
        return 0 if result["success"] else 125
    except BaseException as exc:
        try:
            _write_json(
                Path(json.loads(read_bounded_bytes(spec_path, MIB))["result_path"]),
                {
                    "schema_version": SCHEMA_VERSION,
                    "success": False,
                    "result": "ELEVATED_WRAPPER_EXCEPTION",
                    "exception_class": type(exc).__name__,
                    "operator_owned_active_children": 0,
                    "operator_owned_orphans": 0,
                },
            )
        except BaseException:
            pass
        return 125


def completed_process(result: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
    """Project bounded evidence into the legacy adapter return shape."""

    return subprocess.CompletedProcess(
        args=list(result["safe_argv"]),
        returncode=int(result["exit_code"] if result["exit_code"] is not None else 125),
        stdout=str(result["stdout"]["tail"]),
        stderr=str(result["stderr"]["tail"]),
    )


def _owned_shim(spec_path: Path) -> int:
    raw = read_bounded_bytes(spec_path, 1024 * 1024)
    spec = json.loads(raw)
    gate = Path(spec["gate"])
    deadline = float(spec["deadline_monotonic"])
    while not gate.exists():
        if time.monotonic() >= deadline:
            return 124
        if not _parent_identity_alive(
            int(spec["parent_pid"]), spec.get("parent_creation_ticks")
        ):
            return 125
        time.sleep(0.01)
    argv = spec["argv"]
    cwd = spec.get("cwd")
    stdin_value = spec.get("stdin_base64")
    stdin_data = base64.b64decode(stdin_value, validate=True) if stdin_value else None
    environment = dict(os.environ)
    environment.update(spec.get("env_overrides") or {})
    if not isinstance(argv, list) or not argv:
        return 126
    child = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        cwd=cwd,
        env=environment,
        close_fds=False if os.name == "nt" else True,
    )
    if child.stdin is not None:
        try:
            child.stdin.write(stdin_data)
            child.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            child.stdin.close()
    return child.wait()


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owned-shim", type=Path)
    parser.add_argument("--elevated-wrapper", type=Path)
    parser.add_argument("--elevation-broker", type=Path)
    args = parser.parse_args()
    if args.owned_shim is not None:
        return _owned_shim(args.owned_shim)
    if args.elevated_wrapper is not None:
        return _elevated_wrapper(args.elevated_wrapper)
    if args.elevation_broker is not None:
        return _elevation_broker(args.elevation_broker)
    if args.owned_shim is None:
        parser.error("this module is an internal operator boundary")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
