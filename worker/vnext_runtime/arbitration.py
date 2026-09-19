"""Local host-resource arbitration for concurrent Protocol-v2 runs.

The arbiter is deliberately local and advisory.  It prevents unsafe overlap
before a canonical Protocol-v2 CAS claim, but it never replaces that claim.
Reservations are kept in ignored runtime evidence and are bound to the exact
project/run/admission identity that acquired them.
"""

from __future__ import annotations

import json
import ntpath
import os
import socket
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit


class ResourceArbitrationError(RuntimeError):
    """Raised when local resource identity or registry state is unsafe."""


class ResourceIdentityError(ResourceArbitrationError):
    """Raised when a resource cannot be identified conservatively."""


def _windows_mode(windows: bool | None) -> bool:
    return os.name == "nt" if windows is None else bool(windows)


def canonicalize_path(value: str | Path, *, windows: bool | None = None) -> Path:
    """Resolve a path and return its conservative canonical filesystem name.

    ``realpath`` resolves existing symlinks/junctions.  For a not-yet-created
    leaf, the nearest existing parent is resolved before appending the missing
    suffix, so aliases in the known portion of the path cannot bypass a
    reservation.  Invalid/empty paths fail closed.
    """

    is_windows = _windows_mode(windows)
    if isinstance(value, Path):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raise ResourceIdentityError("resource path must be text or pathlib.Path")
    raw = os.path.expandvars(os.path.expanduser(raw)).strip()
    if not raw:
        raise ResourceIdentityError("resource path is empty")
    try:
        absolute = os.path.abspath(raw)
        candidate = Path(os.path.realpath(absolute))
    except (OSError, ValueError, RuntimeError) as exc:
        raise ResourceIdentityError(f"unable to canonicalize resource path: {raw!r}") from exc
    if is_windows:
        # Windows identity is case-insensitive and accepts either separator.
        candidate = Path(ntpath.normpath(str(candidate).replace("/", "\\")))
    else:
        candidate = Path(os.path.normpath(str(candidate)))
    if not candidate.is_absolute() and not is_windows:
        raise ResourceIdentityError(f"canonical resource path is not absolute: {raw!r}")
    return candidate


def path_identity(value: str | Path, *, windows: bool | None = None) -> str:
    """Return the stable comparison key used by path conflict checks."""

    canonical = canonicalize_path(value, windows=windows)
    text = str(canonical).replace("/", "\\" if _windows_mode(windows) else "/")
    return ntpath.normcase(text) if _windows_mode(windows) else os.path.normcase(text)


def paths_conflict(left: str | Path, right: str | Path, *, windows: bool | None = None) -> bool:
    """Return whether two paths are equal or ancestor/descendant targets."""

    left_key = path_identity(left, windows=windows)
    right_key = path_identity(right, windows=windows)
    if left_key == right_key:
        return True
    separator = "\\" if _windows_mode(windows) else os.sep
    return right_key.startswith(left_key.rstrip("\\/") + separator) or left_key.startswith(
        right_key.rstrip("\\/") + separator
    )


def _normalize_remote(remote: str) -> str:
    value = remote.strip()
    if not value:
        return ""
    # Normalize the common scp-like Git form before URL parsing.
    if ":" in value and "://" not in value and not Path(value).exists():
        user_host, path = value.split(":", 1)
        if "@" in user_host:
            _user, host = user_host.rsplit("@", 1)
        else:
            host = user_host
        value = f"ssh://{host}/{path}"
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = (parsed.hostname or parsed.netloc).casefold()
        path = parsed.path.replace("\\", "/").rstrip("/")
        if path.casefold().endswith(".git"):
            path = path[:-4]
        return f"{parsed.scheme.casefold()}://{host}{path}".casefold()
    try:
        local = canonicalize_path(value)
    except ResourceIdentityError:
        return value.casefold().rstrip("/")
    return str(local).replace("\\", "/").rstrip("/").casefold()


@dataclass(frozen=True, slots=True)
class GitTargetIdentity:
    remote: str
    branch: str

    def __post_init__(self) -> None:
        if not self.remote or not self.branch:
            raise ValueError("GitTargetIdentity requires remote and branch")

    @property
    def key(self) -> tuple[str, str]:
        return self.remote, self.branch.casefold()

    def to_json(self) -> dict[str, str]:
        return {"remote": self.remote, "branch": self.branch}


def git_target_identity(workdir: Path) -> GitTargetIdentity | None:
    """Read a bounded origin/branch identity; unknown Git facts fail closed."""

    def read(*args: str) -> str | None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["git", *args],
            cwd=str(workdir),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            shell=False,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    branch = read("branch", "--show-current")
    remote = read("config", "--get", "remote.origin.url")
    if not branch or not remote:
        return None
    normalized = _normalize_remote(remote)
    if not normalized:
        return None
    return GitTargetIdentity(normalized, branch)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None
        self._thread_lock: threading.RLock | None = None

    def __enter__(self) -> "_FileLock":
        key = str(self.path.resolve())
        with _PROCESS_LOCKS_GUARD:
            self._thread_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
        self._thread_lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self.handle.close()
            self.handle = None
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            if self.handle is not None:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
        finally:
            self.handle = None
            if self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_lock = None


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    reservation_id: str
    project_id: str
    admission_id: str
    run_id: str
    worker_host: str
    worker_pid: int
    paths: tuple[str, ...]
    git_target: GitTargetIdentity | None
    acquired_at: str
    _arbiter: "HostResourceArbiter"

    def close(self) -> bool:
        return self._arbiter.release(self)

    dispose = close

    def __enter__(self) -> "ResourceReservation":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    reservation: ResourceReservation | None
    reason: str | None = None

    @property
    def admitted(self) -> bool:
        return self.reservation is not None


class HostResourceArbiter:
    """Atomic, fail-conservative local project/path/Git reservation registry."""

    def __init__(
        self,
        bridge_root: Path,
        *,
        max_parallel_runs: int = 1,
        registry_path: Path | None = None,
        lock_path: Path | None = None,
        worker_host: str | None = None,
        worker_pid: int | None = None,
        windows: bool | None = None,
    ) -> None:
        if isinstance(max_parallel_runs, bool) or not isinstance(max_parallel_runs, int) or max_parallel_runs < 1:
            raise ValueError("max_parallel_runs must be an integer >= 1")
        self.bridge_root = bridge_root
        self.max_parallel_runs = max_parallel_runs
        runtime = bridge_root / "worker" / "runtime"
        self.registry_path = registry_path or runtime / "resource-registry.json"
        self.lock_path = lock_path or runtime / "resource-registry.lock"
        self.worker_host = worker_host or socket.gethostname() or "unknown"
        self.worker_pid = os.getpid() if worker_pid is None else worker_pid
        self.windows = windows
        self.last_reason: str | None = None

    def effective_paths(self, workdir: Path, exclusive_paths: Any = None) -> tuple[str, ...]:
        values = [workdir] if exclusive_paths is None else exclusive_paths
        if not isinstance(values, (list, tuple)) or not values:
            raise ResourceIdentityError("exclusive_paths must be a non-empty list")
        if workdir not in values and str(workdir) not in {str(item) for item in values}:
            values = [workdir, *values]
        keys: list[str] = []
        for value in values:
            key = path_identity(value, windows=self.windows)
            if key not in keys:
                keys.append(key)
        return tuple(sorted(keys))

    def reserve(
        self,
        project_id: str,
        *,
        workdir: Path,
        exclusive_paths: Any = None,
        run_id: str | None = None,
        admission_id: str | None = None,
        git_target: GitTargetIdentity | None = None,
    ) -> ResourceReservation | None:
        decision = self.check_and_reserve(
            project_id,
            workdir=workdir,
            exclusive_paths=exclusive_paths,
            run_id=run_id,
            admission_id=admission_id,
            git_target=git_target,
        )
        return decision.reservation

    try_reserve = reserve

    def check_and_reserve(
        self,
        project_id: str,
        *,
        workdir: Path,
        exclusive_paths: Any = None,
        run_id: str | None = None,
        admission_id: str | None = None,
        git_target: GitTargetIdentity | None = None,
    ) -> AdmissionDecision:
        try:
            paths = self.effective_paths(workdir, exclusive_paths)
        except ResourceArbitrationError as exc:
            self.last_reason = f"resource_identity:{type(exc).__name__}"
            return AdmissionDecision(None, self.last_reason)
        reservation_id = uuid.uuid4().hex
        admission = admission_id or reservation_id
        run = run_id or f"admission-{admission}"
        candidate = ResourceReservation(
            reservation_id=reservation_id,
            project_id=project_id,
            admission_id=admission,
            run_id=run,
            worker_host=self.worker_host,
            worker_pid=self.worker_pid,
            paths=paths,
            git_target=git_target,
            acquired_at=_now(),
            _arbiter=self,
        )
        try:
            with _FileLock(self.lock_path):
                reservations = self._load_locked()
                reservations = self._remove_safe_stale(reservations)
                reason = self._conflict_reason(candidate, reservations)
                if reason is not None:
                    self.last_reason = reason
                    self._write_locked(reservations)
                    return AdmissionDecision(None, reason)
                reservations.append(self._to_record(candidate))
                self._write_locked(reservations)
        except ResourceArbitrationError:
            self.last_reason = "resource_registry_unavailable"
            return AdmissionDecision(None, self.last_reason)
        self.last_reason = None
        return AdmissionDecision(candidate)

    def release(self, reservation: ResourceReservation) -> bool:
        if reservation._arbiter is not self:
            return False
        with _FileLock(self.lock_path):
            reservations = self._load_locked()
            remaining = [
                item for item in reservations
                if not self._record_matches(item, reservation)
            ]
            changed = len(remaining) != len(reservations)
            if changed:
                self._write_locked(remaining)
            return changed

    def active_reservations(self) -> tuple[dict[str, Any], ...]:
        with _FileLock(self.lock_path):
            reservations = self._remove_safe_stale(self._load_locked())
            self._write_locked(reservations)
            return tuple(reservations)

    def _conflict_reason(
        self, candidate: ResourceReservation, reservations: list[dict[str, Any]]
    ) -> str | None:
        if len(reservations) >= self.max_parallel_runs:
            return "slot_exhausted"
        for record in reservations:
            if record.get("project_id") == candidate.project_id:
                return "project_already_reserved"
            for left in candidate.paths:
                for right in record.get("paths", []):
                    try:
                        if paths_conflict(left, right, windows=self.windows):
                            return "exclusive_path_conflict"
                    except ResourceArbitrationError:
                        return "resource_identity_unknown"
            other_git = record.get("git_target")
            candidate_git = candidate.git_target
            if candidate_git is not None and isinstance(other_git, Mapping):
                if (str(other_git.get("remote", "")), str(other_git.get("branch", "")).casefold()) == candidate_git.key:
                    return "git_target_conflict"
        return None

    def _remove_safe_stale(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record in records:
            if self._safe_stale(record):
                continue
            result.append(record)
        return result

    def _safe_stale(self, record: Mapping[str, Any]) -> bool:
        # Unknown or foreign-host evidence is never deleted automatically.
        if record.get("worker_host") != self.worker_host:
            return False
        pid = record.get("worker_pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
        return False

    @staticmethod
    def _to_record(reservation: ResourceReservation) -> dict[str, Any]:
        return {
            "reservation_id": reservation.reservation_id,
            "project_id": reservation.project_id,
            "admission_id": reservation.admission_id,
            "run_id": reservation.run_id,
            "worker_host": reservation.worker_host,
            "worker_pid": reservation.worker_pid,
            "paths": list(reservation.paths),
            "git_target": reservation.git_target.to_json() if reservation.git_target else None,
            "acquired_at": reservation.acquired_at,
        }

    @staticmethod
    def _record_matches(record: Mapping[str, Any], reservation: ResourceReservation) -> bool:
        return (
            record.get("reservation_id") == reservation.reservation_id
            and record.get("project_id") == reservation.project_id
            and record.get("admission_id") == reservation.admission_id
            and record.get("run_id") == reservation.run_id
            and record.get("worker_host") == reservation.worker_host
            and record.get("worker_pid") == reservation.worker_pid
        )

    def _load_locked(self) -> list[dict[str, Any]]:
        if not self.registry_path.exists():
            return []
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResourceArbitrationError("resource registry is unreadable") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ResourceArbitrationError("resource registry has invalid shape")
        return [dict(item) for item in value]

    def _write_locked(self, records: list[dict[str, Any]]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_name(
            f".{self.registry_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.replace(temporary, self.registry_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "AdmissionDecision",
    "GitTargetIdentity",
    "HostResourceArbiter",
    "ResourceArbitrationError",
    "ResourceIdentityError",
    "ResourceReservation",
    "canonicalize_path",
    "git_target_identity",
    "path_identity",
    "paths_conflict",
]
