"""Read-only reconciliation for one exact Git push effect.

The module deliberately contains no Git write operation.  A caller records a
fully specified intent before the write and later passes that same intent to
``GitPushReconciler``.  The reconciler reads exactly one remote ref and
returns a typed observation; it never retries or repairs the remote.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Callable, Protocol

from .models import RunIdentity
from .recovery_evidence import (
    PushReconciliation,
    PushResolutionState,
)
from .recovery_wal import DurableRunWal, DurableWalError


_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,190}$")
_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class GitEffectError(ValueError):
    """A Git effect intent or read-only observation is not exact."""


def _safe_remote(value: str) -> str:
    if not isinstance(value, str) or _REMOTE_RE.fullmatch(value) is None:
        raise GitEffectError("Git remote name is not safe")
    return value


def _safe_ref(value: str) -> str:
    if not isinstance(value, str) or _REF_RE.fullmatch(value) is None:
        raise GitEffectError("Git target ref is not safe")
    if ".." in value or "//" in value or value.endswith("/"):
        raise GitEffectError("Git target ref is not canonical")
    return value


def _safe_object(value: str, label: str) -> str:
    if not isinstance(value, str) or _OBJECT_RE.fullmatch(value) is None:
        raise GitEffectError(f"{label} is not an exact Git object")
    return value


def target_id_for_ref(remote: str, target_ref: str) -> str:
    """Return the canonical, credential-free identity of one remote ref."""

    remote = _safe_remote(remote)
    target_ref = _safe_ref(target_ref)
    return f"{remote}:{target_ref}"


def split_target_id(target_id: str) -> tuple[str, str]:
    """Parse a canonical target id without accepting arbitrary ref syntax."""

    if not isinstance(target_id, str) or _SAFE_ID_RE.fullmatch(target_id) is None:
        raise GitEffectError("Git target identity is not safe")
    remote, separator, target_ref = target_id.partition(":")
    if not separator:
        raise GitEffectError("Git target identity lacks a remote/ref separator")
    return _safe_remote(remote), _safe_ref(target_ref)


@dataclass(frozen=True, slots=True)
class GitPushIntent:
    """Exact identity and precondition for one Git ref write."""

    identity: RunIdentity
    effect_id: str
    remote: str
    target_ref: str
    expected_object: str
    precondition_object: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RunIdentity):
            raise GitEffectError("Git push intent identity is not exact")
        if not isinstance(self.effect_id, str) or not self.effect_id.strip():
            raise GitEffectError("Git push effect_id is not exact")
        if "\x00" in self.effect_id or "\r" in self.effect_id or "\n" in self.effect_id:
            raise GitEffectError("Git push effect_id is not safe")
        object.__setattr__(self, "remote", _safe_remote(self.remote))
        object.__setattr__(self, "target_ref", _safe_ref(self.target_ref))
        object.__setattr__(
            self,
            "expected_object",
            _safe_object(self.expected_object, "expected_object"),
        )
        object.__setattr__(
            self,
            "precondition_object",
            _safe_object(self.precondition_object, "precondition_object"),
        )

    @property
    def target_id(self) -> str:
        return target_id_for_ref(self.remote, self.target_ref)


class RemoteRefReader(Protocol):
    """The only observation dependency accepted by the reconciler."""

    def read_ref(self, remote: str, target_ref: str) -> str | None:
        """Return the exact object, ``None`` for an absent ref, or raise."""


RemoteRefCallback = Callable[[str, str], str | None]


class PushEffectRecorder(Protocol):
    """Write-boundary hook that records intent/confirmation in exact-run WAL."""

    def record_push_intent(
        self,
        *,
        remote: str,
        target_ref: str,
        expected_object: str,
        precondition_object: str,
    ) -> GitPushIntent:
        """Persist intent before the external write and return its identity."""

    def record_push_confirmed(
        self,
        reconciliation: PushReconciliation,
    ) -> object:
        """Persist confirmation only after exact remote observation."""


class WalPushEffectRecorder:
    """Adapt a durable run WAL to the Git write-boundary recorder contract."""

    __slots__ = ("wal", "effect_prefix", "_next_effect")

    def __init__(self, wal: DurableRunWal, *, effect_prefix: str = "git-push") -> None:
        if not isinstance(wal, DurableRunWal):
            raise TypeError("wal must be a DurableRunWal")
        if not isinstance(effect_prefix, str) or not effect_prefix.strip():
            raise GitEffectError("Git effect prefix is not exact")
        if "\x00" in effect_prefix or "\r" in effect_prefix or "\n" in effect_prefix:
            raise GitEffectError("Git effect prefix is not safe")
        self.wal = wal
        self.effect_prefix = effect_prefix
        self._next_effect = 1

    def record_push_intent(
        self,
        *,
        remote: str,
        target_ref: str,
        expected_object: str,
        precondition_object: str,
    ) -> GitPushIntent:
        used = {
            event.effect_id
            for event in self.wal.events
            if event.effect_id is not None
        }
        effect_id = f"{self.effect_prefix}-{self._next_effect}"
        while effect_id in used:
            self._next_effect += 1
            effect_id = f"{self.effect_prefix}-{self._next_effect}"
        self._next_effect += 1
        intent = GitPushIntent(
            identity=self.wal.identity,
            effect_id=effect_id,
            remote=remote,
            target_ref=target_ref,
            expected_object=expected_object,
            precondition_object=precondition_object,
        )
        self.wal.record_push_intent(
            effect_id=intent.effect_id,
            remote=intent.remote,
            target_ref=intent.target_ref,
            expected_object=intent.expected_object,
            precondition_object=intent.precondition_object,
        )
        return intent

    def record_push_confirmed(self, reconciliation: PushReconciliation) -> object:
        try:
            return self.wal.record_push_confirmed(reconciliation)
        except DurableWalError:
            raise


class CallbackRemoteRefReader:
    """Adapter used by deterministic tests and injected recovery runtimes."""

    __slots__ = ("_callback",)

    def __init__(self, callback: RemoteRefCallback) -> None:
        if not callable(callback):
            raise TypeError("remote ref callback must be callable")
        self._callback = callback

    def read_ref(self, remote: str, target_ref: str) -> str | None:
        _safe_remote(remote)
        _safe_ref(target_ref)
        value = self._callback(remote, target_ref)
        if value is not None:
            _safe_object(value, "observed_object")
        return value


class GitLsRemoteRefReader:
    """Read one ref through ``git ls-remote --refs`` and never mutate Git."""

    __slots__ = ("repository", "timeout")

    def __init__(self, repository: Path, *, timeout: int = 30) -> None:
        if not isinstance(repository, Path):
            raise TypeError("repository must be a pathlib.Path")
        if isinstance(timeout, bool) or not 1 <= timeout <= 300:
            raise GitEffectError("Git remote read timeout is outside the bound")
        self.repository = repository
        self.timeout = timeout

    def read_ref(self, remote: str, target_ref: str) -> str | None:
        remote = _safe_remote(remote)
        target_ref = _safe_ref(target_ref)
        # Keep the import local so this read-only effect boundary cannot create
        # a module-level dependency on the mutation/publishing implementation.
        from git_store import run_process

        try:
            result = run_process(
                ["git", "ls-remote", "--refs", remote, target_ref],
                cwd=self.repository,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise GitEffectError("Git remote ref observation failed") from exc
        if result.returncode != 0:
            raise GitEffectError("Git remote ref observation returned an error")
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != target_ref:
            raise GitEffectError("Git remote ref observation was not exact")
        return _safe_object(rows[0][0], "observed_object")


class GitPushReconciler:
    """Classify one exact push using only a read-only remote observation."""

    __slots__ = ("reader",)

    def __init__(self, reader: RemoteRefReader | RemoteRefCallback) -> None:
        if callable(reader) and not hasattr(reader, "read_ref"):
            reader = CallbackRemoteRefReader(reader)
        if not hasattr(reader, "read_ref"):
            raise TypeError("reader must implement read_ref")
        self.reader = reader

    def reconcile(self, intent: GitPushIntent) -> PushReconciliation:
        if not isinstance(intent, GitPushIntent):
            raise TypeError("intent must be a GitPushIntent")
        try:
            observed = self.reader.read_ref(intent.remote, intent.target_ref)
            if observed is not None:
                observed = _safe_object(observed, "observed_object")
        except Exception:
            # An observer failure is an unknown effect, never permission to
            # retry the write or to infer that it did not happen.
            return PushReconciliation(
                effect_id=intent.effect_id,
                target_id=intent.target_id,
                expected_object=intent.expected_object,
                precondition_object=intent.precondition_object,
                state=PushResolutionState.UNKNOWN,
            )

        if observed == intent.expected_object:
            state = PushResolutionState.EXACT_APPLIED
        elif observed is None and set(intent.precondition_object) == {"0"}:
            # A missing ref is an exact match for the zero-object precondition.
            observed = intent.precondition_object
            state = PushResolutionState.EXACT_NOT_APPLIED
        elif observed == intent.precondition_object:
            state = PushResolutionState.EXACT_NOT_APPLIED
        else:
            state = PushResolutionState.AMBIGUOUS
        return PushReconciliation(
            effect_id=intent.effect_id,
            target_id=intent.target_id,
            expected_object=intent.expected_object,
            precondition_object=intent.precondition_object,
            state=state,
            observed_object=observed,
        )


def reconcile_push_intent(
    intent: GitPushIntent,
    reader: RemoteRefReader | RemoteRefCallback,
) -> PushReconciliation:
    """Convenience entry point for one read-only, exact effect check."""

    return GitPushReconciler(reader).reconcile(intent)


__all__ = [
    "CallbackRemoteRefReader",
    "GitEffectError",
    "GitLsRemoteRefReader",
    "GitPushIntent",
    "GitPushReconciler",
    "PushEffectRecorder",
    "RemoteRefCallback",
    "RemoteRefReader",
    "WalPushEffectRecorder",
    "reconcile_push_intent",
    "split_target_id",
    "target_id_for_ref",
]
