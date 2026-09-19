"""Durable, exact-run-bound append-only recovery evidence.

The writer stores only the already-defined :class:`RecoveryWalEvent` facts in
a small JSON-lines file below a run directory.  Every record repeats the full
RunIdentity, is fsync'ed before the append returns, and is rejected if its
identity, sequence, schema, or integrity digest does not match.  This module
never calls the classifier, executor, Git, network, or canonical state writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, cast

from .models import RunIdentity
from .recovery_evidence import (
    RECOVERY_WAL_SCHEMA_VERSION,
    PushReconciliation,
    PushResolutionState,
    RecoveryEvidenceError,
    RecoveryWal,
    RecoveryWalEvent,
    WalEventKind,
)


WAL_FILE_SCHEMA_VERSION = 1
DEFAULT_MAX_WAL_BYTES = 256 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FIELD_RE = re.compile(r"^[^\x00\r\n]{1,256}$")
_SENSITIVE_RE = re.compile(
    r"(?i)(authorization|cookie|api[_-]?key|token|password|secret|prompt|body|query)"
)
_IDENTITY_FIELDS = frozenset(
    {"project_id", "command_id", "run_id", "claim_generation"}
)
_EVENT_FIELDS = frozenset(
    {
        "sequence",
        "kind",
        "recorded_at",
        "effect_id",
        "target_id",
        "expected_object",
        "observed_object",
        "artifact_digest",
        "precondition_object",
    }
)
_LEGACY_EVENT_FIELDS = _EVENT_FIELDS - {"precondition_object"}
_HEADER_FIELDS = frozenset({"record", "schema_version", "identity"})
_RECORD_FIELDS = frozenset(
    {"record", "schema_version", "identity", "event", "record_digest"}
)


class DurableWalError(ValueError):
    """The exact-run WAL is malformed, too large, or cannot be persisted."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _identity_wire(identity: RunIdentity) -> dict[str, object]:
    return {
        "project_id": identity.project_id,
        "command_id": identity.command_id,
        "run_id": identity.run_id,
        "claim_generation": identity.claim_generation,
    }


def _identity_from_wire(value: object) -> RunIdentity:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_FIELDS:
        raise DurableWalError("WAL identity object is not exact")
    try:
        return RunIdentity(
            project_id=value["project_id"],
            command_id=value["command_id"],
            run_id=value["run_id"],
            claim_generation=value["claim_generation"],
        )
    except (TypeError, ValueError) as exc:
        raise DurableWalError("WAL identity is invalid") from exc


def _safe_field(value: object, label: str, *, digest: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SAFE_FIELD_RE.fullmatch(value) is None:
        raise DurableWalError(f"WAL {label} is outside the bounded field contract")
    if _SENSITIVE_RE.search(value):
        raise DurableWalError(f"WAL {label} contains prohibited sensitive wording")
    if digest and _DIGEST_RE.fullmatch(value) is None:
        raise DurableWalError(f"WAL {label} must be a lowercase SHA-256 digest")
    return value


def _event_wire(event: RecoveryWalEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "kind": event.kind.value,
        "recorded_at": event.recorded_at,
        "effect_id": event.effect_id,
        "target_id": event.target_id,
        "expected_object": event.expected_object,
        "observed_object": event.observed_object,
        "artifact_digest": event.artifact_digest,
        "precondition_object": event.precondition_object,
    }


def _event_from_wire(value: object) -> RecoveryWalEvent:
    if not isinstance(value, Mapping) or set(value) not in {
        _EVENT_FIELDS,
        _LEGACY_EVENT_FIELDS,
    }:
        raise DurableWalError("WAL event fields are not exact")
    try:
        return RecoveryWalEvent(
            sequence=value["sequence"],
            kind=value["kind"],
            recorded_at=value["recorded_at"],
            effect_id=value["effect_id"],
            target_id=value["target_id"],
            expected_object=value["expected_object"],
            observed_object=value["observed_object"],
            artifact_digest=value["artifact_digest"],
            precondition_object=value.get("precondition_object"),
        )
    except (TypeError, ValueError, RecoveryEvidenceError) as exc:
        raise DurableWalError("WAL event is malformed") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        flags = getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(path), os.O_RDONLY | flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class WalFileSnapshot:
    """Immutable view of one validated run WAL."""

    path: Path
    identity: RunIdentity
    events: tuple[RecoveryWalEvent, ...]
    file_digest: str
    size_bytes: int

    @property
    def wal(self) -> RecoveryWal:
        return RecoveryWal(
            schema_version=RECOVERY_WAL_SCHEMA_VERSION,
            identity=self.identity,
            events=self.events,
        )


class DurableRunWal:
    """Append exact-run evidence with monotonic sequence and crash-safe writes."""

    __slots__ = ("path", "identity", "max_bytes", "_lock", "_events")

    def __init__(
        self,
        path: Path,
        identity: RunIdentity,
        *,
        max_bytes: int = DEFAULT_MAX_WAL_BYTES,
    ) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if isinstance(max_bytes, bool) or not 4096 <= max_bytes <= 4 * 1024 * 1024:
            raise DurableWalError("max_bytes is outside the bounded WAL contract")
        self.path = path
        self.identity = identity
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._events: tuple[RecoveryWalEvent, ...] = ()
        self._load_or_initialize()

    @classmethod
    def open(
        cls,
        path: Path,
        identity: RunIdentity,
        *,
        max_bytes: int = DEFAULT_MAX_WAL_BYTES,
    ) -> "DurableRunWal":
        return cls(path, identity, max_bytes=max_bytes)

    @classmethod
    def read(
        cls,
        path: Path,
        *,
        identity: RunIdentity | None = None,
        max_bytes: int = DEFAULT_MAX_WAL_BYTES,
    ) -> WalFileSnapshot:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        if isinstance(max_bytes, bool) or not 4096 <= max_bytes <= 4 * 1024 * 1024:
            raise DurableWalError("max_bytes is outside the bounded WAL contract")
        actual_identity, events, raw = cls._read_file(path, max_bytes=max_bytes)
        if identity is not None and actual_identity != identity:
            raise DurableWalError("WAL identity does not match requested exact run")
        return WalFileSnapshot(
            path=path,
            identity=actual_identity,
            events=events,
            file_digest=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )

    @property
    def events(self) -> tuple[RecoveryWalEvent, ...]:
        with self._lock:
            return self._events

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return len(self._events) + 1

    @property
    def snapshot(self) -> WalFileSnapshot:
        return self.read(self.path, identity=self.identity, max_bytes=self.max_bytes)

    def append_event(self, event: RecoveryWalEvent) -> RecoveryWalEvent:
        """Append one already-validated event at exactly the next sequence."""

        if not isinstance(event, RecoveryWalEvent):
            raise TypeError("event must be a RecoveryWalEvent")
        with self._lock:
            expected = len(self._events) + 1
            if event.sequence != expected:
                raise DurableWalError(
                    f"WAL sequence must be exactly {expected}, got {event.sequence}"
                )
            self._validate_event_fields(event)
            candidate_events = (*self._events, event)
            self._validate_event_sequence(candidate_events)
            try:
                RecoveryWal(
                    schema_version=RECOVERY_WAL_SCHEMA_VERSION,
                    identity=self.identity,
                    events=candidate_events,
                )
            except RecoveryEvidenceError as exc:
                raise DurableWalError(
                    "WAL effect evidence is malformed or contradictory"
                ) from exc
            self._append_record(event)
            self._events = (*self._events, event)
            return event

    def append(
        self,
        kind: WalEventKind,
        *,
        recorded_at: str | None = None,
        effect_id: str | None = None,
        target_id: str | None = None,
        expected_object: str | None = None,
        observed_object: str | None = None,
        artifact_digest: str | None = None,
        precondition_object: str | None = None,
    ) -> RecoveryWalEvent:
        """Construct and append one bounded event without accepting a payload."""

        event = RecoveryWalEvent(
            sequence=self.next_sequence,
            kind=kind,
            recorded_at=recorded_at or _now_iso(),
            effect_id=effect_id,
            target_id=target_id,
            expected_object=expected_object,
            observed_object=observed_object,
            artifact_digest=artifact_digest,
            precondition_object=precondition_object,
        )
        return self.append_event(event)

    def record_push_intent(
        self,
        *,
        effect_id: str,
        remote: str,
        target_ref: str,
        expected_object: str,
        precondition_object: str,
        recorded_at: str | None = None,
    ) -> RecoveryWalEvent:
        """Record a Git intent only after its exact remote precondition exists."""

        _validate_git_push_fields(
            effect_id=effect_id,
            remote=remote,
            target_ref=target_ref,
            expected_object=expected_object,
            precondition_object=precondition_object,
        )
        return self.append(
            WalEventKind.PUSH_INTENT,
            recorded_at=recorded_at,
            effect_id=effect_id,
            target_id=f"{remote}:{target_ref}",
            expected_object=expected_object,
            precondition_object=precondition_object,
        )

    def record_push_confirmed(
        self,
        reconciliation: PushReconciliation,
        *,
        recorded_at: str | None = None,
    ) -> RecoveryWalEvent:
        """Persist confirmation only for an exact applied read-only result."""

        if not isinstance(reconciliation, PushReconciliation):
            raise TypeError("reconciliation must be a PushReconciliation")
        if reconciliation.state is not PushResolutionState.EXACT_APPLIED:
            raise DurableWalError("only an exact applied Git observation can confirm a push")
        effects = {
            effect.effect_id: effect
            for effect in RecoveryWal(
                schema_version=RECOVERY_WAL_SCHEMA_VERSION,
                identity=self.identity,
                events=self.events,
            ).push_effects()
        }
        effect = effects.get(reconciliation.effect_id)
        if effect is None or effect.confirmed:
            raise DurableWalError("Git confirmation has no unconfirmed exact intent")
        if (
            effect.target_id != reconciliation.target_id
            or effect.expected_object != reconciliation.expected_object
            or effect.precondition_object != reconciliation.precondition_object
        ):
            raise DurableWalError("Git confirmation does not bind the exact intent")
        return self.append(
            WalEventKind.PUSH_CONFIRMED,
            recorded_at=recorded_at,
            effect_id=reconciliation.effect_id,
            target_id=reconciliation.target_id,
            expected_object=reconciliation.expected_object,
            observed_object=reconciliation.observed_object,
            precondition_object=reconciliation.precondition_object,
        )

    def record_lifecycle(self, event: str, fields: Mapping[str, object]) -> RecoveryWalEvent | None:
        """Map safe lifecycle facts to the small WAL vocabulary."""

        if not isinstance(event, str) or not isinstance(fields, Mapping):
            return None
        kind_map = {
            "process_create_intent": WalEventKind.PROCESS_CREATE_INTENT,
            "process_created": WalEventKind.PROCESS_CREATED,
            "process_exit": WalEventKind.PROCESS_EXITED,
            "broker_started": WalEventKind.BROKER_STARTED,
            "broker_stopped": WalEventKind.BROKER_STOPPED,
            "containment_prepared": WalEventKind.CONTAINMENT_PREPARED,
            "containment_reconciled": WalEventKind.CONTAINMENT_RECONCILED,
            "containment_released": WalEventKind.CONTAINMENT_RELEASED,
            "project_egress_reconciled": WalEventKind.PROJECT_EGRESS_RECONCILED,
        }
        kind = kind_map.get(event)
        if kind is None:
            return None
        artifact_digest = fields.get("artifact_digest") or fields.get("command_digest")
        if artifact_digest is None and event == "process_created":
            artifact_digest = _pid_digest(fields.get("wrapper_pid"), fields.get("codex_root_pid"))
        if artifact_digest is None and event == "process_exit":
            artifact_digest = _pid_digest(fields.get("exit_code"), fields.get("process_exited_at"))
        appended = self.append(
            kind,
            recorded_at=(
                fields.get("recorded_at")
                if isinstance(fields.get("recorded_at"), str)
                else None
            ),
            artifact_digest=(
                artifact_digest if isinstance(artifact_digest, str) else None
            ),
            observed_object=(
                fields.get("project_egress_observed")
                if event == "project_egress_reconciled"
                and isinstance(fields.get("project_egress_observed"), str)
                else None
            ),
        )
        if (
            event == "containment_reconciled"
            and isinstance(fields.get("project_egress_observed"), str)
            and isinstance(fields.get("project_egress_artifact_digest"), str)
        ):
            self.append(
                WalEventKind.PROJECT_EGRESS_RECONCILED,
                artifact_digest=fields["project_egress_artifact_digest"],
                observed_object=fields["project_egress_observed"],
            )
        return appended

    def logger(self) -> Callable[[str, dict[str, object]], None]:
        """Return a lifecycle callback that records only recognized facts."""

        def callback(event: str, fields: dict[str, object]) -> None:
            self.record_lifecycle(event, fields)

        return callback

    def close(self) -> None:
        """The append writer has no background resource; validate before close."""

        self.snapshot

    def _load_or_initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            header = {
                "record": "header",
                "schema_version": WAL_FILE_SCHEMA_VERSION,
                "identity": _identity_wire(self.identity),
            }
            raw = (_canonical(header) + "\n").encode("utf-8")
            try:
                with self.path.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory(self.path.parent)
            except FileExistsError:
                pass
            else:
                return
        actual_identity, events, _raw = self._read_file(self.path, max_bytes=self.max_bytes)
        if actual_identity != self.identity:
            raise DurableWalError("existing WAL belongs to another exact run")
        self._events = events

    def _append_record(self, event: RecoveryWalEvent) -> None:
        event_wire = _event_wire(event)
        record_without_digest: dict[str, object] = {
            "record": "event",
            "schema_version": WAL_FILE_SCHEMA_VERSION,
            "identity": _identity_wire(self.identity),
            "event": event_wire,
        }
        record = {
            **record_without_digest,
            "record_digest": hashlib.sha256(
                _canonical(record_without_digest).encode("utf-8")
            ).hexdigest(),
        }
        raw = (_canonical(record) + "\n").encode("utf-8")
        current_size = self.path.stat().st_size
        if current_size + len(raw) > self.max_bytes:
            raise DurableWalError("WAL maximum size would be exceeded")
        with self.path.open("ab") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _read_file(
        cls, path: Path, *, max_bytes: int
    ) -> tuple[RunIdentity, tuple[RecoveryWalEvent, ...], bytes]:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise DurableWalError("unable to read exact-run WAL") from exc
        if not raw or len(raw) > max_bytes or not raw.endswith(b"\n"):
            raise DurableWalError("WAL is empty, oversized, or has a partial final record")
        lines = raw.splitlines()
        try:
            header = json.loads(lines[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DurableWalError("WAL header is not valid JSON") from exc
        if not isinstance(header, Mapping) or set(header) != _HEADER_FIELDS:
            raise DurableWalError("WAL header fields are not exact")
        if header.get("record") != "header" or header.get("schema_version") != WAL_FILE_SCHEMA_VERSION:
            raise DurableWalError("WAL header schema is unsupported")
        identity = _identity_from_wire(header.get("identity"))
        events: list[RecoveryWalEvent] = []
        for line in lines[1:]:
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DurableWalError("WAL event is not valid JSON") from exc
            if not isinstance(record, Mapping) or set(record) != _RECORD_FIELDS:
                raise DurableWalError("WAL event record fields are not exact")
            if record.get("record") != "event" or record.get("schema_version") != WAL_FILE_SCHEMA_VERSION:
                raise DurableWalError("WAL event record schema is unsupported")
            record_identity = _identity_from_wire(record.get("identity"))
            if record_identity != identity:
                raise DurableWalError("WAL event identity does not match the header")
            event_value = record.get("event")
            if not isinstance(event_value, Mapping) or set(event_value) not in {
                _EVENT_FIELDS,
                _LEGACY_EVENT_FIELDS,
            }:
                raise DurableWalError("WAL event object fields are not exact")
            digest_value = record.get("record_digest")
            if not isinstance(digest_value, str) or _DIGEST_RE.fullmatch(digest_value) is None:
                raise DurableWalError("WAL record digest is invalid")
            unsigned = {
                "record": "event",
                "schema_version": WAL_FILE_SCHEMA_VERSION,
                "identity": _identity_wire(identity),
                "event": dict(event_value),
            }
            expected_digest = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
            if digest_value != expected_digest:
                raise DurableWalError("WAL record digest mismatch")
            event = _event_from_wire(event_value)
            cls._validate_event_fields(event)
            events.append(event)
        try:
            RecoveryWal(
                schema_version=RECOVERY_WAL_SCHEMA_VERSION,
                identity=identity,
                events=tuple(events),
            )
        except RecoveryEvidenceError as exc:
            raise DurableWalError("WAL event sequence or effect evidence is contradictory") from exc
        cls._validate_event_sequence(tuple(events))
        return identity, tuple(events), raw

    @staticmethod
    def _validate_event_fields(event: RecoveryWalEvent) -> None:
        _safe_field(event.recorded_at, "recorded_at")
        for label in (
            "effect_id",
            "target_id",
            "expected_object",
            "observed_object",
            "precondition_object",
        ):
            _safe_field(getattr(event, label), label)
        _safe_field(event.artifact_digest, "artifact_digest", digest=True)

    @staticmethod
    def _validate_event_sequence(events: tuple[RecoveryWalEvent, ...]) -> None:
        """Reject duplicate or impossible lifecycle facts before persistence."""

        if not events:
            return
        seen: set[WalEventKind] = set()
        index = {event.kind: position for position, event in enumerate(events)}
        for event in events:
            if event.kind in {
                WalEventKind.RUN_CLAIMED,
                WalEventKind.PROCESS_CREATE_INTENT,
                WalEventKind.PROCESS_CREATED,
                WalEventKind.PROCESS_EXITED,
                WalEventKind.BROKER_STARTED,
                WalEventKind.BROKER_STOPPED,
                WalEventKind.CONTAINMENT_PREPARED,
                WalEventKind.CONTAINMENT_RECONCILED,
                WalEventKind.CONTAINMENT_RELEASED,
                WalEventKind.PROJECT_EGRESS_RECONCILED,
                WalEventKind.REPORT_MATERIALIZED,
                WalEventKind.REPORT_PUBLISH_INTENT,
                WalEventKind.REPORT_PUBLISHED,
            }:
                if event.kind in seen:
                    raise DurableWalError(
                        f"WAL contains a duplicate lifecycle event: {event.kind.value}"
                    )
                seen.add(event.kind)
        if events[0].kind is not WalEventKind.RUN_CLAIMED:
            raise DurableWalError("WAL must begin with RUN_CLAIMED")
        prerequisites = {
            WalEventKind.PROCESS_CREATE_INTENT: WalEventKind.RUN_CLAIMED,
            WalEventKind.PROCESS_CREATED: WalEventKind.PROCESS_CREATE_INTENT,
            WalEventKind.PROCESS_EXITED: WalEventKind.PROCESS_CREATED,
            WalEventKind.BROKER_STARTED: WalEventKind.RUN_CLAIMED,
            WalEventKind.BROKER_STOPPED: WalEventKind.BROKER_STARTED,
            WalEventKind.CONTAINMENT_PREPARED: WalEventKind.BROKER_STARTED,
            WalEventKind.CONTAINMENT_RECONCILED: WalEventKind.CONTAINMENT_PREPARED,
            WalEventKind.CONTAINMENT_RELEASED: WalEventKind.CONTAINMENT_PREPARED,
            WalEventKind.PROJECT_EGRESS_RECONCILED: WalEventKind.CONTAINMENT_PREPARED,
            WalEventKind.REPORT_MATERIALIZED: WalEventKind.RUN_CLAIMED,
            WalEventKind.REPORT_PUBLISH_INTENT: WalEventKind.REPORT_MATERIALIZED,
            WalEventKind.REPORT_PUBLISHED: WalEventKind.REPORT_PUBLISH_INTENT,
        }
        for kind, prerequisite in prerequisites.items():
            if kind in index and (
                prerequisite not in index or index[prerequisite] >= index[kind]
            ):
                raise DurableWalError(
                    f"WAL event {kind.value} lacks prior {prerequisite.value}"
                )


def _pid_digest(first: object, second: object) -> str:
    raw = f"{first!r}:{second!r}".encode("ascii", errors="replace")
    return hashlib.sha256(raw).hexdigest()


_GIT_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_GIT_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,190}$")
_GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _validate_git_push_fields(
    *,
    effect_id: str,
    remote: str,
    target_ref: str,
    expected_object: str,
    precondition_object: str,
) -> None:
    if not isinstance(effect_id, str) or _SAFE_FIELD_RE.fullmatch(effect_id) is None:
        raise DurableWalError("Git effect_id is not safe")
    if _GIT_REMOTE_RE.fullmatch(remote) is None:
        raise DurableWalError("Git remote name is not safe")
    if _GIT_REF_RE.fullmatch(target_ref) is None or ".." in target_ref or "//" in target_ref:
        raise DurableWalError("Git target ref is not canonical")
    if _GIT_OBJECT_RE.fullmatch(expected_object) is None:
        raise DurableWalError("Git expected object is not exact")
    if _GIT_OBJECT_RE.fullmatch(precondition_object) is None:
        raise DurableWalError("Git precondition object is not exact")


RunWalWriter = DurableRunWal


__all__ = [
    "DEFAULT_MAX_WAL_BYTES",
    "DurableRunWal",
    "DurableWalError",
    "RunWalWriter",
    "WAL_FILE_SCHEMA_VERSION",
    "WalFileSnapshot",
]
