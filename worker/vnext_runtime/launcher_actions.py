"""Concrete, outer-Launcher-owned actions for self-maintenance handoff.

This module is the intentionally small infrastructure boundary that was
missing from the first handoff implementation.  It is not a second Worker or
protocol engine: it verifies an already-authorized identity, performs a
bounded set of fixed Git/process operations, and returns evidence to the
durable handoff consumer.

The action object is constructed only by the Launcher.  It never receives a
command body, never calls Codex, and never writes Protocol-v2 state.  Git
operations are explicit non-force operations and all local receipts are
integrity checked so a Launcher restart can continue a mechanically proven
step without replaying the initiating command.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import worker_health

from .adoption import (
    AdoptionGate,
    AdoptionMode,
    AdoptionPolicy,
    ConflictCategory,
    HealthObservation,
    protected_path_category,
    validate_sha,
)
from .handoff import (
    ForwardRollbackObservation,
    HandoffConsumerBlockedError,
    HandoffConflictError,
    HandoffIntegrityError,
    HandoffIdentity,
    LauncherHandoffActions,
    WorkerRestartObservation,
)


ACTION_RECEIPT_SCHEMA_VERSION = 1
ACTION_RECEIPT_FILENAME = "adoption-action-receipt.json"
MAX_ACTION_RECEIPT_BYTES = 16 * 1024
MAX_GIT_PATHS = 4096
MAX_GIT_OUTPUT_BYTES = 512 * 1024
DEFAULT_REMOTE_NAME = "origin"
DEFAULT_MAIN_BRANCH = "main"
DEFAULT_WORKER_START_TIMEOUT_SECONDS = 30.0
DEFAULT_WORKER_HEALTH_POLL_SECONDS = 0.25
DEFAULT_ROLLBACK_READY_TIMEOUT_SECONDS = 30.0
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,255}$")
_REMOTE_SECRET_RE = re.compile(
    r"(?i)(?:https?://[^/@\s]+:[^/@\s]+@|(?:token|secret|password|api[_-]?key)=|ghp_|github_pat_|-----begin)"
)


class LauncherActionError(HandoffConsumerBlockedError):
    """Raised when a concrete outer action cannot prove its precondition."""


class FaultInjection(str, Enum):
    """Finite test-only failure hooks; no arbitrary command hook is exposed."""

    NONE = "none"
    PROBATION_FAILURE = "probation_failure"
    ROLLBACK_FAILURE = "rollback_failure"


# A descriptive alias makes the test/acceptance purpose clear without making
# the public configuration surface larger.
Phase85FaultInjection = FaultInjection


def _configured_fault_injection(
    config_path: Path,
    policy: AdoptionPolicy,
) -> tuple[FaultInjection, int | None]:
    """Read one bounded, controlled-only probation-failure arm from config."""

    none = (FaultInjection.NONE, None)
    if (
        not isinstance(policy, AdoptionPolicy)
        or not policy.controlled_adoption_enabled
        or policy.unattended_adoption_enabled
    ):
        return none
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return none
        raw = payload.get("launcher_actions", {})
        if not isinstance(raw, Mapping):
            return none
        if set(raw) - {"fault_injection", "fault_injection_command_id"}:
            return none
        raw_fault = raw.get("fault_injection", FaultInjection.NONE.value)
        fault = FaultInjection(raw_fault)
        if fault is FaultInjection.NONE:
            return none
        if fault is not FaultInjection.PROBATION_FAILURE:
            return none
        command_id = raw.get("fault_injection_command_id")
        if (
            isinstance(command_id, bool)
            or not isinstance(command_id, int)
            or command_id <= 0
        ):
            return none
        return fault, command_id
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return none


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _safe_token(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise LauncherActionError(f"{label}_invalid")
    text = value.strip()
    if not text or "\x00" in text or "\r" in text or "\n" in text:
        raise LauncherActionError(f"{label}_invalid")
    if _SAFE_TOKEN_RE.fullmatch(text) is None:
        raise LauncherActionError(f"{label}_unsafe")
    return text


def _safe_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _iso(parsed)


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_remote(value: str) -> str:
    """Normalize remote identity without retaining credentials."""

    text = value.strip()
    if not text or "\x00" in text or "\r" in text or "\n" in text:
        raise LauncherActionError("remote_identity_invalid")
    if _REMOTE_SECRET_RE.search(text):
        raise LauncherActionError("remote_identity_unsafe")
    if text.startswith("git@") and ":" in text:
        host, path = text[4:].split(":", 1)
        normalized = f"ssh://{host}/{path}"
    elif re.fullmatch(r"[A-Za-z]:[\\/].*", text):
        normalized = str(Path(text).expanduser().resolve())
    else:
        parsed = urlsplit(text)
        if parsed.scheme:
            if parsed.username or parsed.password:
                raise LauncherActionError("remote_identity_unsafe")
            if parsed.scheme in {"file", "ssh", "git", "http", "https"}:
                normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            else:
                raise LauncherActionError("remote_identity_unsupported")
        else:
            # Local bare repositories are useful in disposable acceptance
            # fixtures.  Resolve them only when they are actually local.
            normalized = str(Path(text).expanduser().resolve())
    normalized = normalized.rstrip("/")
    if normalized.casefold().endswith(".git"):
        normalized = normalized[:-4]
    return normalized.casefold()


@dataclass(frozen=True, slots=True)
class LauncherActionConfig:
    """Fixed infrastructure inputs for one Launcher lifetime."""

    repository_root: Path
    runtime_root: Path
    worker_script: Path
    config_path: Path
    log_file: Path
    adoption_policy: AdoptionPolicy = field(default_factory=AdoptionPolicy)
    remote_name: str = DEFAULT_REMOTE_NAME
    expected_remote: str | None = None
    require_remote: bool = True
    launcher_identity: str | None = None
    worker_identity: str | None = None
    worker_start_timeout_seconds: float = DEFAULT_WORKER_START_TIMEOUT_SECONDS
    worker_health_poll_seconds: float = DEFAULT_WORKER_HEALTH_POLL_SECONDS
    rollback_ready_timeout_seconds: float = DEFAULT_ROLLBACK_READY_TIMEOUT_SECONDS
    fault_injection: FaultInjection = FaultInjection.NONE
    fault_injection_command_id: int | None = None

    def __post_init__(self) -> None:
        try:
            root = Path(self.repository_root).expanduser().resolve(strict=True)
            runtime = Path(self.runtime_root).expanduser().resolve()
            script = Path(self.worker_script).expanduser().resolve(strict=True)
            config = Path(self.config_path).expanduser().resolve(strict=True)
            log = Path(self.log_file).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise LauncherActionError("launcher_action_path_invalid") from exc
        if script != root / "worker" / script.name:
            raise LauncherActionError("worker_script_outside_repository")
        if runtime != root / "worker" / "runtime":
            raise LauncherActionError("runtime_boundary_invalid")
        if not isinstance(self.adoption_policy, AdoptionPolicy):
            raise LauncherActionError("adoption_policy_invalid")
        remote_name = _safe_token(self.remote_name, "remote_name")
        if "/" in remote_name or ":" in remote_name or " " in remote_name:
            raise LauncherActionError("remote_name_invalid")
        expected_remote = (
            _normalize_remote(self.expected_remote)
            if self.expected_remote is not None
            else None
        )
        for name, value in (
            ("worker_start_timeout_seconds", self.worker_start_timeout_seconds),
            ("worker_health_poll_seconds", self.worker_health_poll_seconds),
            ("rollback_ready_timeout_seconds", self.rollback_ready_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise LauncherActionError(f"{name}_invalid")
        try:
            fault = self.fault_injection
            if not isinstance(fault, FaultInjection):
                fault = FaultInjection(fault)
        except (TypeError, ValueError) as exc:
            raise LauncherActionError("fault_injection_invalid") from exc
        if fault is FaultInjection.PROBATION_FAILURE:
            if (
                not self.adoption_policy.controlled_adoption_enabled
                or self.adoption_policy.unattended_adoption_enabled
            ):
                raise LauncherActionError("fault_injection_controlled_only")
            command_id = self.fault_injection_command_id
            if (
                isinstance(command_id, bool)
                or not isinstance(command_id, int)
                or command_id <= 0
            ):
                raise LauncherActionError("fault_injection_command_id_invalid")
        elif self.fault_injection_command_id is not None:
            raise LauncherActionError("fault_injection_command_id_unexpected")
        for name in ("launcher_identity", "worker_identity"):
            value = getattr(self, name)
            if value is not None:
                _safe_token(value, name)
        object.__setattr__(self, "repository_root", root)
        object.__setattr__(self, "runtime_root", runtime)
        object.__setattr__(self, "worker_script", script)
        object.__setattr__(self, "config_path", config)
        object.__setattr__(self, "log_file", log)
        object.__setattr__(self, "remote_name", remote_name)
        object.__setattr__(self, "expected_remote", expected_remote)
        object.__setattr__(self, "fault_injection", fault)
        object.__setattr__(self, "fault_injection_command_id", self.fault_injection_command_id)


class _GitFailure(LauncherActionError):
    """Internal fixed-message Git failure; command output is never surfaced."""


class _Git:
    """Narrow Git adapter with no shell and no arbitrary argument surface."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _execute(self, args: Sequence[str], *, binary: bool = False) -> tuple[int, bytes | str]:
        if not args or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise _GitFailure("git_arguments_invalid")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                timeout=30,
                check=False,
                shell=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _GitFailure("git_operation_failed") from exc
        output = completed.stdout
        if isinstance(output, bytes) and len(output) > MAX_GIT_OUTPUT_BYTES:
            raise _GitFailure("git_output_too_large")
        if isinstance(output, str) and len(output.encode("utf-8", errors="replace")) > MAX_GIT_OUTPUT_BYTES:
            raise _GitFailure("git_output_too_large")
        return completed.returncode, output

    def run(self, *args: str) -> str:
        code, output = self._execute(args)
        if code != 0 or not isinstance(output, str):
            raise _GitFailure("git_operation_failed")
        return output.strip()

    def run_bytes(self, *args: str) -> bytes:
        code, output = self._execute(args, binary=True)
        if code != 0 or not isinstance(output, bytes):
            raise _GitFailure("git_operation_failed")
        return output

    def status(self, *args: str) -> tuple[int, str]:
        code, output = self._execute(args)
        if not isinstance(output, str):
            raise _GitFailure("git_operation_failed")
        return code, output.strip()

    def sha(self, ref: str) -> str:
        value = self.run("rev-parse", "--verify", f"{validate_sha(ref, 'commit')}^{{commit}}")
        return validate_sha(value, "resolved_commit")

    def head(self) -> str:
        value = self.run("rev-parse", "--verify", "HEAD^{commit}")
        return validate_sha(value, "head")

    def branch(self) -> str:
        return self.run("branch", "--show-current")

    def clean(self) -> bool:
        return not bool(self.run("status", "--porcelain=v1", "--untracked-files=all"))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        first = validate_sha(ancestor, "ancestor")
        second = validate_sha(descendant, "descendant")
        code, _ = self.status("merge-base", "--is-ancestor", first, second)
        if code not in (0, 1):
            raise _GitFailure("git_ancestry_check_failed")
        return code == 0

    def parents(self, commit: str) -> tuple[str, ...]:
        value = self.run("rev-list", "--parents", "-n", "1", validate_sha(commit, "commit"))
        values = value.split()
        if not values:
            raise _GitFailure("git_parent_read_failed")
        return tuple(validate_sha(item, "parent") for item in values[1:])

    def changed_paths(self, base: str, head: str) -> tuple[str, ...]:
        output = self.run_bytes(
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            validate_sha(base, "diff_base"),
            validate_sha(head, "diff_head"),
            "--",
        )
        raw = tuple(item for item in output.split(b"\x00") if item)
        if len(raw) > MAX_GIT_PATHS:
            raise _GitFailure("git_changed_path_set_too_large")
        paths: list[str] = []
        for item in raw:
            try:
                path = item.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _GitFailure("git_changed_path_invalid") from exc
            path = path.replace("\\", "/")
            if not path or path.startswith("/") or "\x00" in path or ".." in path.split("/"):
                raise _GitFailure("git_changed_path_invalid")
            paths.append(path)
        return tuple(sorted(set(paths)))

    def path_unchanged(self, base: str, head: str, path: str) -> bool:
        """Compare one already-validated repository-relative path."""

        if not isinstance(path, str) or not path or path.startswith("/") or "\x00" in path:
            raise _GitFailure("git_path_invalid")
        if ".." in path.replace("\\", "/").split("/"):
            raise _GitFailure("git_path_invalid")
        code, _ = self.status(
            "diff",
            "--quiet",
            validate_sha(base, "path_base"),
            validate_sha(head, "path_head"),
            "--",
            path,
        )
        if code not in (0, 1):
            raise _GitFailure("git_path_compare_failed")
        return code == 0

    def remote_url(self, remote: str) -> str | None:
        code, output = self.status("remote", "get-url", remote)
        if code != 0:
            return None
        if not output:
            raise _GitFailure("remote_identity_empty")
        return _normalize_remote(output)

    def remote_head(self, remote: str, branch: str) -> str | None:
        output = self.run("ls-remote", "--refs", remote, f"refs/heads/{branch}")
        if not output:
            return None
        rows = output.splitlines()
        if len(rows) != 1:
            raise _GitFailure("remote_head_ambiguous")
        fields = rows[0].split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
            raise _GitFailure("remote_head_invalid")
        return validate_sha(fields[0], "remote_head")

    def push_main(self, remote: str, branch: str) -> None:
        # Explicit refspec, no force option.  A concurrent remote advance is
        # rejected by Git and is consequently a fail-closed action failure.
        code, _ = self.status(
            "push",
            "--porcelain",
            remote,
            f"HEAD:refs/heads/{branch}",
        )
        if code != 0:
            raise _GitFailure("git_forward_push_failed")

    def merge_ff_only(self, commit: str) -> None:
        code, _ = self.status(
            "merge",
            "--ff-only",
            validate_sha(commit, "adoption_commit"),
        )
        if code != 0:
            raise _GitFailure("git_forward_merge_failed")

    def revert(self, commit: str, *, mainline: int | None = None) -> None:
        args = ["revert", "--no-edit"]
        if mainline is not None:
            args.extend(["-m", str(mainline)])
        args.append(validate_sha(commit, "rollback_target"))
        code, _ = self.status(*args)
        if code != 0:
            # An unsuccessful revert can leave Git's revert sequencer active.
            # Abort only that failed forward-revert transaction; this never
            # rewinds a ref and leaves the repository in a known clean state.
            abort_code, _ = self.status("revert", "--abort")
            if abort_code != 0:
                raise _GitFailure("git_rollback_ambiguous")
            raise _GitFailure("git_forward_rollback_failed")

    def subject(self, commit: str) -> str:
        return self.run("show", "-s", "--format=%s", validate_sha(commit, "commit"))


@dataclass(frozen=True, slots=True)
class _ActionReceipt:
    attempt_id: str
    identity_digest: str
    operation: str
    commit_sha: str


class _ActionReceiptStore:
    """Small atomic receipt for a side effect before handoff evidence commit."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @staticmethod
    def _digest(payload: Mapping[str, object]) -> str:
        return sha256(_canonical_json(payload)).hexdigest()

    def write(self, receipt: _ActionReceipt) -> None:
        payload: dict[str, object] = {
            "schema_version": ACTION_RECEIPT_SCHEMA_VERSION,
            "attempt_id": receipt.attempt_id,
            "identity_digest": receipt.identity_digest,
            "operation": receipt.operation,
            "commit_sha": receipt.commit_sha,
        }
        envelope = {
            "schema_version": ACTION_RECEIPT_SCHEMA_VERSION,
            "payload": payload,
            "payload_sha256": self._digest(payload),
        }
        encoded = _canonical_json(envelope) + b"\n"
        if len(encoded) > MAX_ACTION_RECEIPT_BYTES:
            raise LauncherActionError("action_receipt_too_large")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise LauncherActionError("action_receipt_write_failed") from exc
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def read(self) -> _ActionReceipt | None:
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > MAX_ACTION_RECEIPT_BYTES:
                raise LauncherActionError("action_receipt_too_large")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except LauncherActionError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LauncherActionError("action_receipt_unreadable") from exc
        if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "payload", "payload_sha256"}:
            raise LauncherActionError("action_receipt_invalid")
        payload = raw.get("payload")
        digest = raw.get("payload_sha256")
        if (
            raw.get("schema_version") != ACTION_RECEIPT_SCHEMA_VERSION
            or not isinstance(payload, Mapping)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or self._digest(payload) != digest
            or set(payload) != {"schema_version", "attempt_id", "identity_digest", "operation", "commit_sha"}
            or payload.get("schema_version") != ACTION_RECEIPT_SCHEMA_VERSION
        ):
            raise LauncherActionError("action_receipt_invalid")
        try:
            return _ActionReceipt(
                attempt_id=_safe_token(payload["attempt_id"], "receipt_attempt_id"),
                identity_digest=_safe_token(payload["identity_digest"], "receipt_identity_digest"),
                operation=_safe_token(payload["operation"], "receipt_operation"),
                commit_sha=validate_sha(payload["commit_sha"], "receipt_commit_sha"),
            )
        except (LauncherActionError, ValueError) as exc:
            raise LauncherActionError("action_receipt_invalid") from exc


class LauncherOwnedHandoffActions:
    """Concrete action controller retained by one outer Launcher lifetime."""

    def __init__(
        self,
        config: LauncherActionConfig,
        *,
        clock: Callable[[], datetime] = _now,
        process_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(config, LauncherActionConfig):
            raise LauncherActionError("launcher_action_config_invalid")
        self.config = config
        self._clock = clock
        self._git = _Git(config.repository_root)
        # Receipts are scoped by attempt.  Keeping old terminal-attempt
        # receipts out of the current slot is what makes an explicit clean
        # re-adoption possible without treating stale evidence as current.
        self._receipt = _ActionReceiptStore(config.runtime_root / ACTION_RECEIPT_FILENAME)
        self._receipt_attempt_id: str | None = None
        self._process_factory = process_factory or subprocess.Popen
        self._previous_process: Any | None = None
        self._previous_exit_code: int | None = None
        self._replacement_process: Any | None = None
        self._replacement_target: str | None = None
        self._replacement_kind: str | None = None
        self._replacement_handed_off = False
        self._log_handle: Any | None = None
        self._rollback_started = False

    def as_actions(self) -> LauncherHandoffActions:
        """Return the typed callback set accepted by the handoff consumer."""

        return LauncherHandoffActions(
            validate_identity=self.validate_identity,
            worker_stopped=self.worker_stopped,
            active_run_ids=self.active_run_ids,
            ensure_adopted=self.ensure_adopted,
            ensure_worker_restarted=self.ensure_worker_restarted,
            observe_health=self.observe_health,
            ensure_forward_rollback=self.ensure_forward_rollback,
        )

    def bind_worker_exit(self, process: Any | None, exit_code: int | None) -> None:
        """Bind the exact Worker process already waited by the Launcher."""

        self._previous_process = process
        self._previous_exit_code = exit_code

    def take_replacement_process(self) -> Any | None:
        """Transfer a verified replacement process to the Launcher loop."""

        process = self._replacement_process
        if process is None or self._replacement_handed_off:
            return None
        self._replacement_handed_off = True
        return process

    def close(self) -> None:
        """Clean up only an action-owned process that was not handed off."""

        process = self._replacement_process
        if process is not None and not self._replacement_handed_off:
            self._terminate_process(process)
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def _time(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise LauncherActionError("launcher_action_clock_invalid")
        return value.astimezone(timezone.utc)

    def _identity_digest(self, identity: HandoffIdentity) -> str:
        return sha256(_canonical_json(identity.to_payload())).hexdigest()

    def _bind_receipt(self, identity: HandoffIdentity) -> None:
        if self._receipt_attempt_id == identity.attempt_id:
            return
        attempt_digest = sha256(identity.attempt_id.encode("utf-8")).hexdigest()
        self._receipt = _ActionReceiptStore(
            self.config.runtime_root
            / f"adoption-action-receipt-{attempt_digest}.json"
        )
        self._receipt_attempt_id = identity.attempt_id

    def _configured_identity(self, identity: HandoffIdentity, name: str) -> str:
        configured = getattr(self.config, name)
        expected = getattr(identity, f"expected_{name}")
        if configured is not None and configured != expected:
            raise HandoffConflictError(f"{name}_mismatch")
        return configured or expected

    def _remote_preflight(self, identity: HandoffIdentity) -> str | None:
        remote = self._git.remote_url(self.config.remote_name)
        if remote is None:
            if self.config.require_remote:
                raise LauncherActionError("remote_identity_unavailable")
            return None
        if self.config.expected_remote is not None and remote != self.config.expected_remote:
            raise HandoffConflictError("stable_remote_identity_mismatch")
        return remote

    def _validate_candidate_delta(self, identity: HandoffIdentity) -> None:
        paths = self._git.changed_paths(identity.previous_stable_sha, identity.accepted_candidate_sha)
        for path in paths:
            category = protected_path_category(path)
            if category in {
                ConflictCategory.PROTOCOL_AUTHORITY,
                ConflictCategory.PROJECT_CONTROL_PLANE,
                ConflictCategory.PROTECTED_PATH,
                ConflictCategory.RECOVERY_IDENTITY,
            }:
                raise HandoffConflictError("candidate_protected_path_mismatch")

    def _post_adoption_head_is_safe(self, identity: HandoffIdentity, head: str) -> bool:
        if not self._git.is_ancestor(identity.reconciled_adoption_sha, head):
            return False
        # After the forward adoption only append-only control-plane changes
        # may have landed before a restart/probation retry.  Source drift is
        # ambiguous and therefore blocks.
        for path in self._git.changed_paths(identity.reconciled_adoption_sha, head):
            if not path.casefold().startswith("projects/"):
                return False
        return True

    def _post_rollback_head_is_safe(
        self,
        rollback_sha: str,
        head: str,
    ) -> bool:
        """Allow only append-only control-plane history after a rollback."""

        if not self._git.is_ancestor(rollback_sha, head):
            return False
        return all(
            path.casefold().startswith("projects/")
            for path in self._git.changed_paths(rollback_sha, head)
        )

    def _validate_repository_identity(self, identity: HandoffIdentity) -> tuple[str, str | None]:
        self._bind_receipt(identity)
        if identity.mode is not AdoptionMode.MANUAL:
            raise HandoffConsumerBlockedError("unattended_adoption_disabled")
        AdoptionGate(self.config.adoption_policy).authorize(
            identity.mode,
            explicit_controlled_mode=True,
        )
        self._configured_identity(identity, "launcher_identity")
        self._configured_identity(identity, "worker_identity")
        if self._git.branch() != DEFAULT_MAIN_BRANCH:
            raise HandoffConflictError("stable_branch_mismatch")
        if not self._git.clean():
            raise LauncherActionError("stable_worktree_dirty")
        previous = self._git.sha(identity.previous_stable_sha)
        latest = self._git.sha(identity.latest_main_sha)
        candidate = self._git.sha(identity.accepted_candidate_sha)
        target = self._git.sha(identity.reconciled_adoption_sha)
        if previous != identity.previous_stable_sha or latest != identity.latest_main_sha or candidate != identity.accepted_candidate_sha or target != identity.reconciled_adoption_sha:
            raise HandoffConflictError("handoff_commit_identity_mismatch")
        if not self._git.is_ancestor(previous, latest):
            raise HandoffConflictError("stable_history_not_ancestral")
        if not self._git.is_ancestor(latest, target):
            raise HandoffConflictError("adoption_not_forward_from_latest_main")
        if not self._git.is_ancestor(candidate, target):
            raise HandoffConflictError("candidate_not_in_adoption_tree")
        self._validate_candidate_delta(identity)
        target_paths = self._git.changed_paths(latest, target)
        for path in target_paths:
            category = protected_path_category(path)
            if category in {
                ConflictCategory.PROTOCOL_AUTHORITY,
                ConflictCategory.PROJECT_CONTROL_PLANE,
                ConflictCategory.PROTECTED_PATH,
                ConflictCategory.RECOVERY_IDENTITY,
            }:
                raise HandoffConflictError("adoption_protected_path_mismatch")
        head = self._git.head()
        rollback_receipt = self._receipt_for(identity, "rollback")
        rollback_sha = (
            rollback_receipt.commit_sha
            if rollback_receipt is not None
            else self._existing_rollback_commit(identity)
        )
        local_head_safe = head in {
            identity.latest_main_sha,
            identity.reconciled_adoption_sha,
        } or self._post_adoption_head_is_safe(identity, head)
        if rollback_sha is not None:
            local_head_safe = local_head_safe or head == rollback_sha or self._post_rollback_head_is_safe(rollback_sha, head)
        if not local_head_safe:
            raise HandoffConflictError("current_main_identity_mismatch")
        remote = self._remote_preflight(identity)
        if remote is not None:
            remote_head = self._git.remote_head(self.config.remote_name, DEFAULT_MAIN_BRANCH)
            if remote_head is None:
                raise HandoffConflictError("stable_remote_main_missing")
            remote_head_safe = remote_head in {
                identity.latest_main_sha,
                identity.reconciled_adoption_sha,
            } or self._post_adoption_head_is_safe(identity, remote_head)
            if rollback_sha is not None:
                remote_head_safe = remote_head_safe or remote_head == rollback_sha or self._post_rollback_head_is_safe(rollback_sha, remote_head)
            if not remote_head_safe:
                raise HandoffConflictError("stable_remote_main_identity_mismatch")
        return head, remote

    def validate_identity(self, identity: HandoffIdentity) -> None:
        """Perform all read-only Stable/Candidate/current-main checks."""

        if not isinstance(identity, HandoffIdentity):
            raise HandoffIntegrityError("handoff_identity_invalid")
        self._validate_repository_identity(identity)

    def worker_stopped(self, identity: HandoffIdentity) -> bool:
        """Use the Launcher wait proof; never wait for or kill the initiator."""

        if self._previous_process is None:
            # This is the safe Launcher-restart case: its Job boundary already
            # ended the previous Worker before this Launcher instance started.
            return self._replacement_process is None
        try:
            observed = self._previous_process.poll()
        except Exception as exc:
            raise LauncherActionError("worker_stop_observation_failed") from exc
        if observed is None:
            return False
        if self._previous_exit_code is not None and observed != self._previous_exit_code:
            raise HandoffConflictError("worker_exit_identity_mismatch")
        return True

    def active_run_ids(self, identity: HandoffIdentity) -> tuple[str, ...]:
        """Read the bounded Worker health projection and require zero runs."""

        path = worker_health.worker_health_path(
            self.config.repository_root,
            runtime_root=self.config.runtime_root,
        )
        data = worker_health.read_health(path)
        if not data or not path.is_file():
            raise LauncherActionError("worker_health_unavailable")
        raw_active = data.get("active_runs")
        raw_count = data.get("active_run_count")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise LauncherActionError("active_run_count_invalid")
        if not isinstance(raw_active, list):
            raise LauncherActionError("active_run_evidence_invalid")
        if len(raw_active) > 128 or raw_count != len(raw_active):
            raise HandoffConflictError("active_run_evidence_inconsistent")
        ids: list[str] = []
        for item in raw_active:
            if not isinstance(item, Mapping):
                raise LauncherActionError("active_run_evidence_invalid")
            value = item.get("run_id")
            if not isinstance(value, str):
                raise LauncherActionError("active_run_identity_invalid")
            ids.append(_safe_token(value, "active_run_id"))
        if len(set(ids)) != len(ids):
            raise HandoffConflictError("active_run_evidence_duplicate")
        return tuple(sorted(ids))

    def _ensure_remote_at(
        self,
        identity: HandoffIdentity,
        expected: str,
        *,
        expected_base: str | None = None,
    ) -> None:
        expected = validate_sha(expected, "expected_remote_head")
        base = validate_sha(
            expected_base or identity.latest_main_sha,
            "expected_remote_base",
        )
        if not self.config.require_remote and self._git.remote_url(self.config.remote_name) is None:
            return
        remote_head = self._git.remote_head(self.config.remote_name, DEFAULT_MAIN_BRANCH)
        if remote_head == expected:
            return
        if remote_head != base:
            raise HandoffConflictError("remote_main_moved_before_forward_update")
        self._git.push_main(self.config.remote_name, DEFAULT_MAIN_BRANCH)
        if self._git.remote_head(self.config.remote_name, DEFAULT_MAIN_BRANCH) != expected:
            raise HandoffConflictError("remote_main_forward_update_unverified")

    def _receipt_for(self, identity: HandoffIdentity, operation: str) -> _ActionReceipt | None:
        receipt = self._receipt.read()
        if receipt is None:
            return None
        if receipt.attempt_id != identity.attempt_id or receipt.identity_digest != self._identity_digest(identity):
            raise HandoffConflictError("action_receipt_identity_mismatch")
        if receipt.operation != operation:
            # One bounded receipt slot advances from the forward adoption
            # side effect to the later rollback side effect.  The handoff
            # evidence remains the authoritative phase history; a prior
            # operation receipt is therefore not an identity conflict.
            if {receipt.operation, operation} != {"adopt", "rollback"}:
                raise HandoffConflictError("action_receipt_operation_mismatch")
            return None
        return receipt

    def ensure_adopted(self, identity: HandoffIdentity) -> str:
        """Forward-advance main to the already-reconciled adoption commit."""

        head, _ = self._validate_repository_identity(identity)
        target = identity.reconciled_adoption_sha
        receipt = self._receipt_for(identity, "adopt")
        if receipt is not None and receipt.commit_sha != target:
            raise HandoffConflictError("adoption_receipt_commit_mismatch")
        if head == target:
            self._ensure_remote_at(identity, target)
            if receipt is None:
                self._receipt.write(_ActionReceipt(identity.attempt_id, self._identity_digest(identity), "adopt", target))
            return target
        if head != identity.latest_main_sha:
            raise HandoffConflictError("current_main_changed_before_adoption")
        self._git.merge_ff_only(target)
        if self._git.head() != target:
            raise HandoffConflictError("adoption_head_unverified")
        self._receipt.write(_ActionReceipt(identity.attempt_id, self._identity_digest(identity), "adopt", target))
        self._ensure_remote_at(identity, target)
        return target

    def _worker_health_observation(self, identity: HandoffIdentity) -> HealthObservation:
        path = worker_health.worker_health_path(
            self.config.repository_root,
            runtime_root=self.config.runtime_root,
        )
        data = worker_health.read_health(path)
        process = self._replacement_process
        process_alive = False
        process_pid: int | None = None
        if process is not None:
            try:
                process_alive = process.poll() is None
                process_pid = int(process.pid)
            except (AttributeError, TypeError, ValueError, OSError):
                process_alive = False
        raw_pid = data.get("worker_pid")
        pid_matches = isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid == process_pid
        started = _safe_time(data.get("worker_started_at") or data.get("last_worker_start"))
        heartbeat = _safe_time(
            data.get("last_poll_at")
            or data.get("last_successful_poll_at")
            or data.get("last_successful_fetch_at")
        )
        # A prior worker's fresh file is not an observation of this replacement.
        # Keep the existing restart deadline/probation gates; wait for the exact
        # owned PID instead of treating the old PID as a new unhealthy worker.
        if not pid_matches:
            started = None
            heartbeat = None
        no_failure = not any(
            data.get(name)
            for name in ("last_failure_kind", "last_failure_stage", "last_failure_project")
        )
        base_ready = bool(path.is_file() and process_alive and pid_matches and started)
        explicit_worker = data.get("worker_healthy")
        explicit_protocol = data.get("protocol_ready")
        explicit_recovery = data.get("recovery_clear")
        worker_healthy = base_ready and (bool(explicit_worker) if isinstance(explicit_worker, bool) else bool(heartbeat and no_failure))
        protocol_ready = base_ready and (
            bool(explicit_protocol)
            if isinstance(explicit_protocol, bool)
            else bool(_safe_time(data.get("last_successful_fetch_at")) and data.get("poll_count", 0))
        )
        recovery_clear = base_ready and (
            bool(explicit_recovery) if isinstance(explicit_recovery, bool) else no_failure
        )
        command_id = getattr(identity, "initiating_command_id", None)
        probation_failure_armed = (
            self.config.fault_injection is FaultInjection.PROBATION_FAILURE
            and isinstance(command_id, int)
            and not isinstance(command_id, bool)
            and command_id == self.config.fault_injection_command_id
        )
        if probation_failure_armed and not self._rollback_started:
            worker_healthy = False
        return HealthObservation(
            observed_at=_iso(self._time()),
            exit_code=75,
            worker_started_at=started,
            heartbeat_at=heartbeat,
            launcher_alive=True,
            worker_healthy=worker_healthy,
            protocol_ready=protocol_ready,
            recovery_clear=recovery_clear,
        )

    def _start_worker(self, identity: HandoffIdentity, *, kind: str) -> WorkerRestartObservation:
        worker_sha = identity.reconciled_adoption_sha
        if kind == "rollback":
            receipt = self._receipt_for(identity, "rollback")
            if receipt is not None:
                worker_sha = receipt.commit_sha
        if self._replacement_process is not None:
            target = self._replacement_target
            if target != worker_sha:
                raise HandoffConflictError("replacement_target_mismatch")
            try:
                pid = int(self._replacement_process.pid)
            except (AttributeError, TypeError, ValueError) as exc:
                raise LauncherActionError("replacement_pid_invalid") from exc
            return WorkerRestartObservation(
                launcher_identity=self._configured_identity(identity, "launcher_identity"),
                worker_identity=self._configured_identity(identity, "worker_identity"),
                worker_sha=worker_sha,
            )
        self.config.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self._log_handle is None:
                self._log_handle = self.config.log_file.open("a", encoding="utf-8", buffering=1)
            environment = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "BRIDGE_HANDOFF_EVIDENCE_PATH": str(
                    self.config.runtime_root / "adoption-handoff.json"
                ),
                "BRIDGE_HANDOFF_ATTEMPT_ID": identity.attempt_id,
                "BRIDGE_HANDOFF_ALLOWED_PHASES": (
                    "ROLLED_BACK" if kind == "rollback" else "COMPLETED"
                ),
            }
            process = self._process_factory(
                [
                    sys.executable,
                    "-u",
                    str(self.config.worker_script),
                    "--config",
                    str(self.config.config_path),
                ],
                cwd=str(self.config.repository_root),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=environment,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
        except Exception as exc:
            raise LauncherActionError("replacement_worker_start_failed") from exc
        self._replacement_process = process
        self._replacement_target = worker_sha
        self._replacement_kind = kind
        return WorkerRestartObservation(
            launcher_identity=self._configured_identity(identity, "launcher_identity"),
            worker_identity=self._configured_identity(identity, "worker_identity"),
            worker_sha=worker_sha,
        )

    def ensure_worker_restarted(self, identity: HandoffIdentity) -> WorkerRestartObservation:
        """Start/verify exactly one gated replacement Worker process."""

        self._validate_repository_identity(identity)
        return self._start_worker(identity, kind="adopt")

    def observe_health(self, identity: HandoffIdentity) -> HealthObservation:
        """Return only bounded local health facts for the consumer gate."""

        return self._worker_health_observation(identity)

    def _terminate_process(self, process: Any) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        except Exception:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            except Exception as exc:
                raise LauncherActionError("replacement_worker_cleanup_failed") from exc

    def _existing_rollback_commit(self, identity: HandoffIdentity) -> str | None:
        receipt = self._receipt_for(identity, "rollback")
        if receipt is not None:
            if not self._git.is_ancestor(identity.reconciled_adoption_sha, receipt.commit_sha):
                raise HandoffConflictError("rollback_receipt_not_forward")
            return receipt.commit_sha
        head = self._git.head()
        if head == identity.reconciled_adoption_sha or not self._git.is_ancestor(identity.reconciled_adoption_sha, head):
            return None
        candidates: list[str] = []
        for commit in self._git.run(
            "rev-list",
            "--first-parent",
            f"{identity.reconciled_adoption_sha}..{head}",
        ).splitlines()[:128]:
            try:
                commit_sha = validate_sha(commit, "rollback_candidate")
                parents = self._git.parents(commit_sha)
                subject = self._git.subject(commit_sha)
            except (LauncherActionError, ValueError):
                continue
            if len(parents) != 1 or not subject.casefold().startswith("revert "):
                continue
            if not self._git.is_ancestor(identity.previous_stable_sha, commit_sha):
                continue
            # A discovered forward revert may preserve later projects/**
            # history, but it must leave no source delta relative to the
            # then-current main that was adopted.
            if any(
                not path.casefold().startswith("projects/")
                for path in self._git.changed_paths(identity.latest_main_sha, commit_sha)
            ):
                continue
            candidates.append(commit_sha)
        if len(candidates) > 1:
            raise HandoffConflictError("rollback_commit_ambiguous")
        return candidates[0] if candidates else None

    def _verify_known_good_runtime(self, identity: HandoffIdentity, rollback_sha: str) -> None:
        # A forward rollback is only accepted if the Worker source tree is
        # byte-for-byte the known-good tree.  Control-plane history is allowed
        # to differ and is separately protected by ancestry/push checks.
        adoption_paths = self._git.changed_paths(
            identity.latest_main_sha,
            identity.reconciled_adoption_sha,
        )
        rollback_delta = set(
            self._git.changed_paths(identity.reconciled_adoption_sha, rollback_sha)
        )
        source_adoption_paths = {
            path for path in adoption_paths if not path.casefold().startswith("projects/")
        }
        source_rollback_paths = {
            path for path in rollback_delta if not path.casefold().startswith("projects/")
        }
        if not source_rollback_paths.issubset(source_adoption_paths):
            raise HandoffConflictError("known_good_runtime_not_restored")
        if any(
            not self._git.path_unchanged(
                identity.latest_main_sha,
                rollback_sha,
                path,
            )
            for path in source_adoption_paths
        ):
            raise HandoffConflictError("known_good_runtime_not_restored")

    def _wait_for_known_good_ready(self, identity: HandoffIdentity) -> None:
        deadline = time.monotonic() + self.config.rollback_ready_timeout_seconds
        while True:
            observation = self._worker_health_observation(identity)
            started = _safe_time(observation.worker_started_at)
            heartbeat = _safe_time(observation.heartbeat_at)
            if (
                observation.launcher_alive
                and observation.worker_healthy
                and observation.protocol_ready
                and observation.recovery_clear
                and started is not None
                and heartbeat is not None
            ):
                started_at = datetime.fromisoformat(started)
                if self._time() >= started_at + timedelta(seconds=identity.probation_seconds):
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LauncherActionError("rollback_worker_not_ready")
            time.sleep(min(self.config.worker_health_poll_seconds, remaining))

    def ensure_forward_rollback(
        self,
        identity: HandoffIdentity,
        reason: str,
    ) -> ForwardRollbackObservation:
        """Create/verify one forward revert and restore a gated known-good Worker."""

        _safe_token(reason, "rollback_reason")
        self._validate_repository_identity(identity)
        if self.config.fault_injection is FaultInjection.ROLLBACK_FAILURE:
            raise LauncherActionError("fault_injection_rollback_failure")
        if self._replacement_process is not None and not self._replacement_handed_off:
            self._terminate_process(self._replacement_process)
        self._rollback_started = True
        rollback_sha = self._existing_rollback_commit(identity)
        if rollback_sha is None:
            current = self._git.head()
            if current != identity.reconciled_adoption_sha and not self._post_adoption_head_is_safe(identity, current):
                raise HandoffConflictError("rollback_current_head_not_adopted")
            self._ensure_remote_at(identity, current, expected_base=current)
            parents = self._git.parents(identity.reconciled_adoption_sha)
            if len(parents) not in (1, 2):
                raise LauncherActionError("rollback_target_parent_shape_invalid")
            self._git.revert(
                identity.reconciled_adoption_sha,
                mainline=1 if len(parents) == 2 else None,
            )
            rollback_sha = self._git.head()
            if rollback_sha in {identity.previous_stable_sha, identity.reconciled_adoption_sha}:
                raise HandoffConflictError("rollback_commit_not_new")
            self._receipt.write(
                _ActionReceipt(
                    identity.attempt_id,
                    self._identity_digest(identity),
                    "rollback",
                    rollback_sha,
                )
            )
        rollback_sha = validate_sha(rollback_sha, "rollback_commit_sha")
        self._verify_known_good_runtime(identity, rollback_sha)
        if not self._git.is_ancestor(identity.reconciled_adoption_sha, rollback_sha):
            raise HandoffConflictError("rollback_commit_not_forward")
        if self._receipt_for(identity, "rollback") is None:
            self._receipt.write(
                _ActionReceipt(
                    identity.attempt_id,
                    self._identity_digest(identity),
                    "rollback",
                    rollback_sha,
                )
            )
        rollback_parents = self._git.parents(rollback_sha)
        if len(rollback_parents) != 1:
            raise HandoffConflictError("rollback_commit_parent_shape_invalid")
        self._ensure_remote_at(
            identity,
            rollback_sha,
            expected_base=rollback_parents[0],
        )
        self._replacement_process = None
        self._replacement_handed_off = False
        self._start_worker(identity, kind="rollback")
        self._wait_for_known_good_ready(identity)
        return ForwardRollbackObservation(
            rollback_commit_sha=rollback_sha,
            forward_only_verified=True,
        )


def create_launcher_action_controller(
    *,
    repository_root: Path,
    runtime_root: Path,
    worker_script: Path,
    config_path: Path,
    log_file: Path,
    adoption_policy: AdoptionPolicy,
    expected_remote: str | None = None,
    require_remote: bool = True,
    launcher_identity: str | None = None,
    worker_identity: str | None = None,
    fault_injection: FaultInjection = FaultInjection.NONE,
    fault_injection_command_id: int | None = None,
) -> LauncherOwnedHandoffActions | None:
    """Build real actions only for an explicitly controlled handoff mode.

    ``None`` is the conservative production default.  Ordinary unattended
    operation therefore retains the old fail-closed consumer until an
    accepted authority mechanism enables one controlled handoff attempt.
    """

    if not isinstance(adoption_policy, AdoptionPolicy):
        raise LauncherActionError("adoption_policy_invalid")
    if not adoption_policy.controlled_adoption_enabled:
        return None
    try:
        selected_fault = (
            fault_injection
            if isinstance(fault_injection, FaultInjection)
            else FaultInjection(fault_injection)
        )
    except (TypeError, ValueError) as exc:
        raise LauncherActionError("fault_injection_invalid") from exc
    selected_command_id = fault_injection_command_id
    if selected_fault is FaultInjection.NONE and selected_command_id is None:
        selected_fault, selected_command_id = _configured_fault_injection(
            config_path,
            adoption_policy,
        )
    config = LauncherActionConfig(
        repository_root=repository_root,
        runtime_root=runtime_root,
        worker_script=worker_script,
        config_path=config_path,
        log_file=log_file,
        adoption_policy=adoption_policy,
        expected_remote=expected_remote,
        require_remote=require_remote,
        launcher_identity=launcher_identity,
        worker_identity=worker_identity,
        fault_injection=selected_fault,
        fault_injection_command_id=selected_command_id,
    )
    return LauncherOwnedHandoffActions(config)


__all__ = [
    "ACTION_RECEIPT_FILENAME",
    "FaultInjection",
    "LauncherActionConfig",
    "LauncherActionError",
    "LauncherOwnedHandoffActions",
    "Phase85FaultInjection",
    "create_launcher_action_controller",
]
