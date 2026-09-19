"""Durable outer-Launcher handoff evidence for controlled self-maintenance.

The Worker can request a drain and exit with the existing code 75, but it is
not allowed to own the authority that replaces it, validates its replacement,
or rolls back a failed replacement.  This module is therefore consumed by the
Launcher boundary.  It stores only a bounded, integrity-checked identity and
forward-only lifecycle receipt; it has no command body, executor call, Git
mutation, or Protocol-v2 state writer.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Callable, Iterator, Mapping, Sequence

from .adoption import (
    AdoptionMode,
    HealthGateContract,
    HealthGateStatus,
    HealthObservation,
    RollbackIdentity,
    validate_sha,
)


HANDOFF_SCHEMA_VERSION = 1
MAX_HANDOFF_BYTES = 64 * 1024
MAX_HANDOFF_RUN_IDS = 128
MAX_HANDOFF_TOKENS = 16
MAX_TOKEN_LENGTH = 256
HANDOFF_EVIDENCE_FILENAME = "adoption-handoff.json"
PROMOTION_RESTART_EXIT_CODE = 75
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,255}$")
_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:|cookie\s*:|(?:token|secret|password|api[_-]?key)\s*[=:]|"
    r"ghp_|github_pat_|sk-(?:proj-)?|-----begin)"
)
_HEALTH_CRITERIA = {
    "launcher_alive",
    "worker_healthy",
    "protocol_ready",
    "recovery_clear",
}


class HandoffError(RuntimeError):
    """Base class for fail-closed outer-controller handoff errors."""


class HandoffIntegrityError(HandoffError):
    """Raised for absent, corrupt, tampered, or schema-invalid evidence."""


class HandoffConflictError(HandoffError):
    """Raised when an evidence file belongs to another attempt/identity."""


class HandoffStaleError(HandoffError):
    """Raised when an unfinished handoff has passed its bounded deadline."""


class HandoffTransitionError(HandoffError):
    """Raised when a lifecycle transition is out of order or unsafe."""


class HandoffConsumerBlockedError(HandoffError):
    """Raised when the outer controller cannot prove a safe handoff action."""


class HandoffNotQuiescedError(HandoffTransitionError):
    """Raised without waiting when the Worker still owns active RunScopes."""


class HandoffHealthError(HandoffTransitionError):
    """Raised when the replacement Worker has not passed its health gate."""


class HandoffPhase(str, Enum):
    """Launcher-local lifecycle; none of these values is a Protocol state."""

    PREPARED = "PREPARED"
    DRAINING = "DRAINING"
    QUIESCED = "QUIESCED"
    ADOPTED = "ADOPTED"
    PROBATION = "PROBATION"
    COMPLETED = "COMPLETED"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_text(value: object, label: str, *, limit: int = MAX_TOKEN_LENGTH) -> str:
    if not isinstance(value, str):
        raise HandoffIntegrityError(f"{label}_invalid")
    text = value.strip()
    if not text or len(text) > limit or "\x00" in text or "\r" in text or "\n" in text:
        raise HandoffIntegrityError(f"{label}_invalid")
    if _SECRET_RE.search(text):
        raise HandoffIntegrityError(f"{label}_unsafe")
    return text


def _safe_token(value: object, label: str, *, limit: int = MAX_TOKEN_LENGTH) -> str:
    text = _safe_text(value, label, limit=limit)
    if _TOKEN_RE.fullmatch(text) is None:
        raise HandoffIntegrityError(f"{label}_unsafe")
    return text


def _parse_time(value: object, label: str) -> datetime:
    text = _safe_text(value, label, limit=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffIntegrityError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise HandoffIntegrityError(f"{label}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _optional_sha(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA_RE.fullmatch(value.strip()) is None:
        raise HandoffIntegrityError(f"{label}_invalid")
    return value.strip().lower()


def _positive_int(value: object, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandoffIntegrityError(f"{label}_invalid")
    if value < (0 if allow_zero else 1):
        raise HandoffIntegrityError(f"{label}_invalid")
    return value


def _sequence_values(value: object, label: str) -> tuple[object, ...]:
    """Normalize a bounded sequence without treating text as a collection."""

    if isinstance(value, (str, bytes, Mapping)):
        raise HandoffIntegrityError(f"{label}_shape_invalid")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise HandoffIntegrityError(f"{label}_shape_invalid") from exc


@dataclass(frozen=True, slots=True)
class HandoffIdentity:
    """Exact adoption identity owned by the outer Launcher."""

    attempt_id: str
    initiated_at: str
    previous_stable_sha: str
    latest_main_sha: str
    accepted_candidate_sha: str
    reconciled_adoption_sha: str
    initiating_project_id: str
    initiating_run_id: str
    initiating_command_id: int
    initiating_claim_generation: int
    expected_launcher_identity: str
    expected_worker_identity: str
    startup_contract: tuple[str, ...]
    health_deadline: str
    health_criteria: tuple[str, ...]
    probation_seconds: int
    rollback_identity: RollbackIdentity
    mode: AdoptionMode
    replay_initiating_command: bool = False

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "initiating_project_id",
            "initiating_run_id",
            "expected_launcher_identity",
            "expected_worker_identity",
        ):
            object.__setattr__(self, name, _safe_token(getattr(self, name), name))
        initiated = _parse_time(self.initiated_at, "initiated_at")
        object.__setattr__(self, "initiated_at", _iso(initiated))
        for name in (
            "previous_stable_sha",
            "latest_main_sha",
            "accepted_candidate_sha",
            "reconciled_adoption_sha",
        ):
            object.__setattr__(self, name, validate_sha(getattr(self, name), name))
        object.__setattr__(
            self,
            "initiating_command_id",
            _positive_int(self.initiating_command_id, "initiating_command_id"),
        )
        object.__setattr__(
            self,
            "initiating_claim_generation",
            _positive_int(
                self.initiating_claim_generation,
                "initiating_claim_generation",
                allow_zero=True,
            ),
        )
        contract = tuple(
            _safe_token(item, "startup_contract")
            for item in _sequence_values(self.startup_contract, "startup_contract")
        )
        if not contract or len(contract) > MAX_HANDOFF_TOKENS:
            raise HandoffIntegrityError("startup_contract_invalid")
        object.__setattr__(self, "startup_contract", tuple(dict.fromkeys(contract)))
        deadline = _parse_time(self.health_deadline, "health_deadline")
        if deadline < initiated:
            raise HandoffIntegrityError("health_deadline_before_initiated")
        object.__setattr__(self, "health_deadline", _iso(deadline))
        criteria = tuple(
            _safe_token(item, "health_criterion")
            for item in _sequence_values(self.health_criteria, "health_criteria")
        )
        if not criteria or len(criteria) > MAX_HANDOFF_TOKENS or any(item not in _HEALTH_CRITERIA for item in criteria):
            raise HandoffIntegrityError("health_criteria_invalid")
        object.__setattr__(self, "health_criteria", tuple(dict.fromkeys(criteria)))
        if (
            isinstance(self.probation_seconds, bool)
            or not isinstance(self.probation_seconds, int)
            or not 0 <= self.probation_seconds <= 86_400
        ):
            raise HandoffIntegrityError("probation_seconds_invalid")
        if not isinstance(self.rollback_identity, RollbackIdentity):
            raise HandoffIntegrityError("rollback_identity_invalid")
        if self.rollback_identity.known_good_sha != self.previous_stable_sha:
            raise HandoffIntegrityError("rollback_identity_does_not_bind_previous_stable")
        if not isinstance(self.mode, AdoptionMode):
            try:
                object.__setattr__(self, "mode", AdoptionMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise HandoffIntegrityError("adoption_mode_invalid") from exc
        if not isinstance(self.replay_initiating_command, bool) or self.replay_initiating_command:
            raise HandoffIntegrityError("initiating_command_replay_forbidden")

    def to_payload(self) -> dict[str, object]:
        """Return the fixed, command-free identity payload."""

        return {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "initiated_at": self.initiated_at,
            "previous_stable_sha": self.previous_stable_sha,
            "latest_main_sha": self.latest_main_sha,
            "accepted_candidate_sha": self.accepted_candidate_sha,
            "reconciled_adoption_sha": self.reconciled_adoption_sha,
            "initiating_project_id": self.initiating_project_id,
            "initiating_run_id": self.initiating_run_id,
            "initiating_command_id": self.initiating_command_id,
            "initiating_claim_generation": self.initiating_claim_generation,
            "expected_launcher_identity": self.expected_launcher_identity,
            "expected_worker_identity": self.expected_worker_identity,
            "startup_contract": list(self.startup_contract),
            "health_deadline": self.health_deadline,
            "health_criteria": list(self.health_criteria),
            "probation_seconds": self.probation_seconds,
            "rollback_identity": self.rollback_identity.to_dict(),
            "mode": self.mode.value,
            "replay_initiating_command": False,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "HandoffIdentity":
        required = {
            "schema_version",
            "attempt_id",
            "initiated_at",
            "previous_stable_sha",
            "latest_main_sha",
            "accepted_candidate_sha",
            "reconciled_adoption_sha",
            "initiating_project_id",
            "initiating_run_id",
            "initiating_command_id",
            "initiating_claim_generation",
            "expected_launcher_identity",
            "expected_worker_identity",
            "startup_contract",
            "health_deadline",
            "health_criteria",
            "probation_seconds",
            "rollback_identity",
            "mode",
            "replay_initiating_command",
        }
        if set(payload) != required or payload.get("schema_version") != HANDOFF_SCHEMA_VERSION:
            raise HandoffIntegrityError("handoff_identity_schema_invalid")
        startup = payload["startup_contract"]
        criteria = payload["health_criteria"]
        rollback = payload["rollback_identity"]
        if not isinstance(startup, (list, tuple)) or not isinstance(criteria, (list, tuple)) or not isinstance(rollback, Mapping):
            raise HandoffIntegrityError("handoff_identity_shape_invalid")
        try:
            rollback_identity = RollbackIdentity(
                known_good_sha=rollback.get("known_good_sha"),
                branch=rollback.get("branch", "main"),
                controller=rollback.get("controller", "outer_launcher"),
            )
            return cls(
                attempt_id=payload["attempt_id"],
                initiated_at=payload["initiated_at"],
                previous_stable_sha=payload["previous_stable_sha"],
                latest_main_sha=payload["latest_main_sha"],
                accepted_candidate_sha=payload["accepted_candidate_sha"],
                reconciled_adoption_sha=payload["reconciled_adoption_sha"],
                initiating_project_id=payload["initiating_project_id"],
                initiating_run_id=payload["initiating_run_id"],
                initiating_command_id=payload["initiating_command_id"],
                initiating_claim_generation=payload["initiating_claim_generation"],
                expected_launcher_identity=payload["expected_launcher_identity"],
                expected_worker_identity=payload["expected_worker_identity"],
                startup_contract=tuple(startup),
                health_deadline=payload["health_deadline"],
                health_criteria=tuple(criteria),
                probation_seconds=payload["probation_seconds"],
                rollback_identity=rollback_identity,
                mode=payload["mode"],
                replay_initiating_command=payload["replay_initiating_command"],
            )
        except (HandoffError, TypeError, ValueError) as exc:
            raise HandoffIntegrityError("handoff_identity_invalid") from exc


@dataclass(frozen=True, slots=True)
class HandoffEvidence:
    """One durable, bounded outer-controller handoff receipt."""

    identity: HandoffIdentity
    phase: HandoffPhase
    recorded_at: str
    transition_seq: int
    worker_boundary_quiesced: bool
    active_run_ids: tuple[str, ...]
    worker_exit_code: int | None = None
    restart_requested_at: str | None = None
    observed_launcher_identity: str | None = None
    observed_worker_identity: str | None = None
    observed_worker_sha: str | None = None
    failure_reason: str | None = None
    rollback_commit_sha: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, HandoffIdentity):
            raise HandoffIntegrityError("handoff_identity_invalid")
        if not isinstance(self.phase, HandoffPhase):
            try:
                object.__setattr__(self, "phase", HandoffPhase(self.phase))
            except (TypeError, ValueError) as exc:
                raise HandoffIntegrityError("handoff_phase_invalid") from exc
        object.__setattr__(self, "recorded_at", _iso(_parse_time(self.recorded_at, "recorded_at")))
        object.__setattr__(
            self,
            "transition_seq",
            _positive_int(self.transition_seq, "transition_seq", allow_zero=True),
        )
        if not isinstance(self.worker_boundary_quiesced, bool):
            raise HandoffIntegrityError("worker_boundary_quiesced_invalid")
        active = tuple(
            _safe_token(item, "active_run_id")
            for item in _sequence_values(self.active_run_ids, "active_run_ids")
        )
        if len(active) > MAX_HANDOFF_RUN_IDS or len(set(active)) != len(active):
            raise HandoffIntegrityError("active_run_ids_invalid")
        object.__setattr__(self, "active_run_ids", tuple(sorted(active)))
        if self.worker_exit_code is not None and (
            isinstance(self.worker_exit_code, bool) or not isinstance(self.worker_exit_code, int)
        ):
            raise HandoffIntegrityError("worker_exit_code_invalid")
        if self.restart_requested_at is not None:
            object.__setattr__(
                self,
                "restart_requested_at",
                _iso(_parse_time(self.restart_requested_at, "restart_requested_at")),
            )
        for name in ("observed_launcher_identity", "observed_worker_identity"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _safe_token(value, name))
        object.__setattr__(
            self,
            "observed_worker_sha",
            _optional_sha(self.observed_worker_sha, "observed_worker_sha"),
        )
        if self.failure_reason is not None:
            object.__setattr__(self, "failure_reason", _safe_token(self.failure_reason, "failure_reason"))
        object.__setattr__(
            self,
            "rollback_commit_sha",
            _optional_sha(self.rollback_commit_sha, "rollback_commit_sha"),
        )
        if self.phase in {
            HandoffPhase.QUIESCED,
            HandoffPhase.ADOPTED,
            HandoffPhase.PROBATION,
            HandoffPhase.COMPLETED,
            HandoffPhase.ROLLBACK_REQUIRED,
            HandoffPhase.ROLLED_BACK,
        } and not self.worker_boundary_quiesced:
            raise HandoffIntegrityError("handoff_transition_before_quiescence")
        if self.phase in {HandoffPhase.PREPARED, HandoffPhase.DRAINING} and self.worker_boundary_quiesced:
            raise HandoffIntegrityError("pre_quiescence_handoff_marked_quiesced")
        if self.worker_boundary_quiesced and self.active_run_ids:
            raise HandoffIntegrityError("quiesced_handoff_has_active_runs")
        if not self.worker_boundary_quiesced and self.active_run_ids:
            raise HandoffIntegrityError("non_quiesced_handoff_has_active_evidence")
        if self.phase is HandoffPhase.ADOPTED and self.restart_requested_at is None:
            raise HandoffIntegrityError("adopted_handoff_missing_restart_request")
        if self.phase in {HandoffPhase.ADOPTED, HandoffPhase.PROBATION, HandoffPhase.COMPLETED} and self.worker_exit_code != PROMOTION_RESTART_EXIT_CODE:
            raise HandoffIntegrityError("adopted_handoff_exit_code_invalid")
        if self.phase in {HandoffPhase.PROBATION, HandoffPhase.COMPLETED} and (
            self.restart_requested_at is None
            or self.observed_launcher_identity is None
            or self.observed_worker_identity is None
            or self.observed_worker_sha is None
        ):
            raise HandoffIntegrityError("probation_identity_incomplete")
        if self.phase in {HandoffPhase.PROBATION, HandoffPhase.COMPLETED} and self.observed_worker_sha != self.identity.reconciled_adoption_sha:
            raise HandoffIntegrityError("probation_tree_identity_mismatch")
        if self.phase in {
            HandoffPhase.ROLLBACK_REQUIRED,
            HandoffPhase.ROLLED_BACK,
            HandoffPhase.SUPERSEDED,
        } and self.failure_reason is None:
            raise HandoffIntegrityError("handoff_failure_reason_missing")
        if self.phase is HandoffPhase.ROLLED_BACK and self.rollback_commit_sha is None:
            raise HandoffIntegrityError("rollback_commit_missing")
        if self.phase not in {
            HandoffPhase.ROLLBACK_REQUIRED,
            HandoffPhase.ROLLED_BACK,
            HandoffPhase.SUPERSEDED,
        } and self.failure_reason is not None:
            raise HandoffIntegrityError("unexpected_handoff_failure_reason")
        if self.phase is not HandoffPhase.ROLLED_BACK and self.rollback_commit_sha is not None:
            raise HandoffIntegrityError("unexpected_rollback_commit")
        if self.phase is HandoffPhase.COMPLETED and self.failure_reason is not None:
            raise HandoffIntegrityError("completed_handoff_has_failure")
        if self.phase is HandoffPhase.PREPARED and self.transition_seq != 0:
            raise HandoffIntegrityError("prepared_handoff_sequence_invalid")
        if self.phase is not HandoffPhase.PREPARED and self.transition_seq == 0:
            raise HandoffIntegrityError("non_prepared_handoff_sequence_invalid")
        if self.phase in {
            HandoffPhase.PREPARED,
            HandoffPhase.DRAINING,
            HandoffPhase.QUIESCED,
        } and any(
            value is not None
            for value in (
                self.worker_exit_code,
                self.restart_requested_at,
                self.observed_launcher_identity,
                self.observed_worker_identity,
                self.observed_worker_sha,
                self.rollback_commit_sha,
            )
        ):
            raise HandoffIntegrityError("pre_restart_handoff_has_restart_evidence")
        if self.phase in {
            HandoffPhase.ADOPTED,
            HandoffPhase.PROBATION,
            HandoffPhase.COMPLETED,
            HandoffPhase.ROLLBACK_REQUIRED,
            HandoffPhase.ROLLED_BACK,
        } and self.restart_requested_at is None:
            raise HandoffIntegrityError("post_adoption_handoff_missing_restart_request")

    @property
    def claims_allowed(self) -> bool:
        """Ordinary claims resume only after the full replacement gate."""

        return self.phase is HandoffPhase.COMPLETED

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "identity": self.identity.to_payload(),
            "phase": self.phase.value,
            "recorded_at": self.recorded_at,
            "transition_seq": self.transition_seq,
            "worker_boundary_quiesced": self.worker_boundary_quiesced,
            "active_run_ids": list(self.active_run_ids),
            "worker_exit_code": self.worker_exit_code,
            "restart_requested_at": self.restart_requested_at,
            "observed_launcher_identity": self.observed_launcher_identity,
            "observed_worker_identity": self.observed_worker_identity,
            "observed_worker_sha": self.observed_worker_sha,
            "failure_reason": self.failure_reason,
            "rollback_commit_sha": self.rollback_commit_sha,
        }

    def integrity_sha256(self) -> str:
        return sha256(_canonical_json(self.to_payload())).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "HandoffEvidence":
        required = {
            "schema_version",
            "identity",
            "phase",
            "recorded_at",
            "transition_seq",
            "worker_boundary_quiesced",
            "active_run_ids",
            "worker_exit_code",
            "restart_requested_at",
            "observed_launcher_identity",
            "observed_worker_identity",
            "observed_worker_sha",
            "failure_reason",
            "rollback_commit_sha",
        }
        if set(payload) != required or payload.get("schema_version") != HANDOFF_SCHEMA_VERSION:
            raise HandoffIntegrityError("handoff_evidence_schema_invalid")
        identity_raw = payload["identity"]
        active_raw = payload["active_run_ids"]
        if not isinstance(identity_raw, Mapping) or not isinstance(active_raw, (list, tuple)):
            raise HandoffIntegrityError("handoff_evidence_shape_invalid")
        try:
            return cls(
                identity=HandoffIdentity.from_payload(identity_raw),
                phase=payload["phase"],
                recorded_at=payload["recorded_at"],
                transition_seq=payload["transition_seq"],
                worker_boundary_quiesced=payload["worker_boundary_quiesced"],
                active_run_ids=tuple(active_raw),
                worker_exit_code=payload["worker_exit_code"],
                restart_requested_at=payload["restart_requested_at"],
                observed_launcher_identity=payload["observed_launcher_identity"],
                observed_worker_identity=payload["observed_worker_identity"],
                observed_worker_sha=payload["observed_worker_sha"],
                failure_reason=payload["failure_reason"],
                rollback_commit_sha=payload["rollback_commit_sha"],
            )
        except (HandoffError, TypeError, ValueError) as exc:
            raise HandoffIntegrityError("handoff_evidence_invalid") from exc


class HandoffEvidenceStore:
    """Atomic local evidence store owned by the outer Launcher."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize Launcher consumers across threads and processes."""

        lock_path = self.path.with_name(f".{self.path.name}.lock")
        handle = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise HandoffIntegrityError("handoff_lock_unavailable") from exc
        try:
            yield
        finally:
            if handle is None:
                return
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise HandoffIntegrityError("handoff_lock_release_failed") from exc
            finally:
                handle.close()

    def write(self, evidence: HandoffEvidence) -> None:
        if not isinstance(evidence, HandoffEvidence):
            raise HandoffIntegrityError("handoff_evidence_object_invalid")
        payload = evidence.to_payload()
        encoded = _canonical_json(
            {
                "schema_version": HANDOFF_SCHEMA_VERSION,
                "payload": payload,
                "payload_sha256": evidence.integrity_sha256(),
            }
        ) + b"\n"
        if len(encoded) > MAX_HANDOFF_BYTES:
            raise HandoffIntegrityError("handoff_evidence_too_large")
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise HandoffIntegrityError("handoff_evidence_write_failed") from exc
        finally:
            try:
                temporary.unlink()
            except (FileNotFoundError, OSError):
                pass

    def read(self) -> HandoffEvidence:
        try:
            if self.path.stat().st_size > MAX_HANDOFF_BYTES:
                raise HandoffIntegrityError("handoff_evidence_too_large")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except HandoffIntegrityError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HandoffIntegrityError("handoff_evidence_unreadable") from exc
        if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "payload", "payload_sha256"}:
            raise HandoffIntegrityError("handoff_envelope_invalid")
        payload = raw.get("payload")
        expected = raw.get("payload_sha256")
        if (
            raw.get("schema_version") != HANDOFF_SCHEMA_VERSION
            or not isinstance(payload, Mapping)
            or not isinstance(expected, str)
            or _DIGEST_RE.fullmatch(expected) is None
        ):
            raise HandoffIntegrityError("handoff_envelope_invalid")
        actual = sha256(_canonical_json(payload)).hexdigest()
        if actual != expected:
            raise HandoffIntegrityError("handoff_digest_mismatch")
        return HandoffEvidence.from_payload(payload)

    def read_optional(self) -> HandoffEvidence | None:
        if not self.path.exists():
            return None
        return self.read()

    load = read


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OuterControllerHandoff:
    """Idempotent, non-replaying lifecycle authority outside the Worker."""

    def __init__(
        self,
        store: HandoffEvidenceStore,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not isinstance(store, HandoffEvidenceStore):
            raise HandoffIntegrityError("handoff_store_invalid")
        self.store = store
        self._clock = clock
        self._lock = RLock()

    def _time(self) -> datetime:
        current = self._clock()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise HandoffIntegrityError("handoff_clock_invalid")
        return current.astimezone(timezone.utc)

    def _read(self, identity: HandoffIdentity) -> HandoffEvidence:
        evidence = self.store.read()
        if evidence.identity != identity:
            raise HandoffConflictError("handoff_identity_mismatch")
        if evidence.phase not in {
            HandoffPhase.COMPLETED,
            HandoffPhase.ROLLED_BACK,
            HandoffPhase.SUPERSEDED,
        }:
            if self._time() > _parse_time(identity.health_deadline, "health_deadline"):
                raise HandoffStaleError("handoff_evidence_expired")
        if identity.replay_initiating_command:
            raise HandoffTransitionError("initiating_command_replay_forbidden")
        return evidence

    def _transition(self, evidence: HandoffEvidence, **updates: object) -> HandoffEvidence:
        next_evidence = replace(
            evidence,
            **updates,
            recorded_at=_iso(self._time()),
            transition_seq=evidence.transition_seq + 1,
        )
        self.store.write(next_evidence)
        return next_evidence

    def prepare(self, identity: HandoffIdentity) -> HandoffEvidence:
        """Durably bind one attempt; repeat calls with the same identity are idempotent."""

        if not isinstance(identity, HandoffIdentity):
            raise HandoffIntegrityError("handoff_identity_invalid")
        with self._lock:
            existing = self.store.read_optional()
            if existing is not None:
                if existing.identity != identity:
                    # A terminal handoff may be followed by one explicitly
                    # bound successor attempt.  The successor must start
                    # from the terminal known-good tree, use the then-current
                    # main as its stable base, and carry a strictly newer
                    # command/claim identity.  Non-terminal, stale or
                    # ambiguous evidence remains immutable and fail-closed.
                    if existing.phase is HandoffPhase.COMPLETED:
                        successor_base = existing.identity.reconciled_adoption_sha
                    elif existing.phase is HandoffPhase.ROLLED_BACK:
                        successor_base = existing.rollback_commit_sha
                    else:
                        successor_base = None
                    if (
                        successor_base is None
                        or identity.attempt_id == existing.identity.attempt_id
                        or identity.previous_stable_sha != successor_base
                        or identity.latest_main_sha != identity.previous_stable_sha
                        or identity.initiating_project_id != existing.identity.initiating_project_id
                        or identity.initiating_command_id <= existing.identity.initiating_command_id
                        or identity.initiating_claim_generation <= existing.identity.initiating_claim_generation
                    ):
                        raise HandoffConflictError("duplicate_or_stale_handoff_identity")
                    evidence = HandoffEvidence(
                        identity=identity,
                        phase=HandoffPhase.PREPARED,
                        recorded_at=_iso(self._time()),
                        transition_seq=0,
                        worker_boundary_quiesced=False,
                        active_run_ids=(),
                    )
                    self.store.write(evidence)
                    return evidence
                return self._read(identity)
            evidence = HandoffEvidence(
                identity=identity,
                phase=HandoffPhase.PREPARED,
                recorded_at=_iso(self._time()),
                transition_seq=0,
                worker_boundary_quiesced=False,
                active_run_ids=(),
            )
            self.store.write(evidence)
            return evidence

    def begin_draining(self, identity: HandoffIdentity) -> HandoffEvidence:
        """Close new admission without waiting inside an initiating RunScope."""

        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.DRAINING:
                return evidence
            if evidence.phase is not HandoffPhase.PREPARED:
                raise HandoffTransitionError("handoff_drain_transition_invalid")
            return self._transition(evidence, phase=HandoffPhase.DRAINING)

    def record_worker_quiesced(
        self,
        identity: HandoffIdentity,
        *,
        active_run_ids: Sequence[str],
        initiating_run_id: str | None = None,
    ) -> HandoffEvidence:
        """Record a completed Worker boundary without blocking on active work.

        The caller must report an empty active-run set.  In particular, an
        initiating RunScope appearing in that set is rejected immediately so
        it can never wait for its own drain completion.
        """

        active = tuple(
            sorted(
                {
                    _safe_token(item, "active_run_id")
                    for item in _sequence_values(active_run_ids, "active_run_ids")
                }
            )
        )
        if len(active) > MAX_HANDOFF_RUN_IDS:
            raise HandoffNotQuiescedError("active_run_set_too_large")
        initiator = (
            _safe_token(initiating_run_id, "initiating_run_id")
            if initiating_run_id is not None
            else identity.initiating_run_id
        )
        if active:
            if initiator in active:
                raise HandoffNotQuiescedError("initiating_run_still_active")
            raise HandoffNotQuiescedError("worker_boundary_has_active_runs")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.QUIESCED:
                return evidence
            if evidence.phase is not HandoffPhase.DRAINING:
                raise HandoffTransitionError("worker_quiescence_transition_invalid")
            return self._transition(
                evidence,
                phase=HandoffPhase.QUIESCED,
                worker_boundary_quiesced=True,
                active_run_ids=(),
            )

    def mark_adopted(
        self,
        identity: HandoffIdentity,
        *,
        observed_adoption_sha: str,
    ) -> HandoffEvidence:
        """Record a post-quiescence adoption point; never moves a Git ref."""

        observed = validate_sha(observed_adoption_sha, "observed_adoption_sha")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.ADOPTED:
                if evidence.observed_worker_sha not in (None, observed):
                    raise HandoffConflictError("adopted_identity_mismatch")
                return evidence
            if evidence.phase is not HandoffPhase.QUIESCED or not evidence.worker_boundary_quiesced:
                raise HandoffTransitionError("promotion_before_worker_quiescence")
            if observed != identity.reconciled_adoption_sha:
                raise HandoffConflictError("adoption_tree_identity_mismatch")
            return self._transition(
                evidence,
                phase=HandoffPhase.ADOPTED,
                worker_exit_code=PROMOTION_RESTART_EXIT_CODE,
                restart_requested_at=_iso(self._time()),
            )

    def record_worker_restart(
        self,
        identity: HandoffIdentity,
        *,
        launcher_identity: str,
        worker_identity: str,
        worker_sha: str,
        exit_code: int = PROMOTION_RESTART_EXIT_CODE,
    ) -> HandoffEvidence:
        """Bind the replacement process to the exact adoption identity."""

        observed_sha = validate_sha(worker_sha, "worker_sha")
        launcher = _safe_token(launcher_identity, "launcher_identity")
        worker = _safe_token(worker_identity, "worker_identity")
        if exit_code != PROMOTION_RESTART_EXIT_CODE:
            raise HandoffTransitionError("unexpected_worker_restart_exit_code")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.PROBATION:
                if (
                    evidence.observed_launcher_identity != launcher
                    or evidence.observed_worker_identity != worker
                    or evidence.observed_worker_sha != observed_sha
                ):
                    raise HandoffConflictError("replacement_identity_mismatch")
                return evidence
            if evidence.phase is not HandoffPhase.ADOPTED:
                raise HandoffTransitionError("worker_restart_before_adoption")
            if observed_sha != identity.reconciled_adoption_sha:
                raise HandoffConflictError("replacement_tree_identity_mismatch")
            if launcher != identity.expected_launcher_identity or worker != identity.expected_worker_identity:
                raise HandoffConflictError("replacement_startup_identity_mismatch")
            return self._transition(
                evidence,
                phase=HandoffPhase.PROBATION,
                observed_launcher_identity=launcher,
                observed_worker_identity=worker,
                observed_worker_sha=observed_sha,
                worker_exit_code=exit_code,
            )

    def mark_health_ready(
        self,
        identity: HandoffIdentity,
        *,
        observation: HealthObservation,
        launcher_identity: str,
        worker_identity: str,
        worker_sha: str,
    ) -> HandoffEvidence:
        """Complete handoff only after the bounded existing health contract is READY."""

        if not isinstance(observation, HealthObservation):
            raise HandoffHealthError("health_observation_invalid")
        observed_sha = validate_sha(worker_sha, "worker_sha")
        launcher = _safe_token(launcher_identity, "launcher_identity")
        worker = _safe_token(worker_identity, "worker_identity")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.COMPLETED:
                return evidence
            if evidence.phase is not HandoffPhase.PROBATION:
                raise HandoffHealthError("health_before_worker_probation")
            if (
                evidence.observed_launcher_identity != launcher
                or evidence.observed_worker_identity != worker
                or evidence.observed_worker_sha != observed_sha
                or observed_sha != identity.reconciled_adoption_sha
            ):
                raise HandoffConflictError("health_identity_mismatch")
            requested = evidence.restart_requested_at
            if requested is None:
                raise HandoffHealthError("restart_request_evidence_missing")
            contract = HealthGateContract(
                restart_requested_at=requested,
                health_deadline=identity.health_deadline,
                probation_seconds=identity.probation_seconds,
                health_criteria=identity.health_criteria,
            )
            decision = contract.evaluate(observation)
            if decision.status is not HealthGateStatus.READY:
                raise HandoffHealthError("health_gate_not_ready")
            return self._transition(evidence, phase=HandoffPhase.COMPLETED, failure_reason=None)

    def request_rollback(self, identity: HandoffIdentity, *, reason: str) -> HandoffEvidence:
        """Move to an outer-controlled rollback requirement without the Worker."""

        safe_reason = _safe_token(reason, "failure_reason")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.ROLLBACK_REQUIRED:
                return evidence
            if evidence.phase not in {HandoffPhase.ADOPTED, HandoffPhase.PROBATION}:
                raise HandoffTransitionError("rollback_request_transition_invalid")
            return self._transition(
                evidence,
                phase=HandoffPhase.ROLLBACK_REQUIRED,
                failure_reason=safe_reason,
            )

    def mark_forward_rollback(
        self,
        identity: HandoffIdentity,
        *,
        rollback_commit_sha: str,
        forward_only_verified: bool,
    ) -> HandoffEvidence:
        """Record a new rollback commit; resetting/force-pushing is impossible here."""

        rollback_sha = validate_sha(rollback_commit_sha, "rollback_commit_sha")
        if not isinstance(forward_only_verified, bool) or not forward_only_verified:
            raise HandoffTransitionError("forward_rollback_proof_required")
        if rollback_sha in {
            identity.previous_stable_sha,
            identity.reconciled_adoption_sha,
        }:
            raise HandoffTransitionError("rollback_must_be_new_forward_commit")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.ROLLED_BACK:
                if evidence.rollback_commit_sha != rollback_sha:
                    raise HandoffConflictError("rollback_identity_mismatch")
                return evidence
            if evidence.phase is not HandoffPhase.ROLLBACK_REQUIRED:
                raise HandoffTransitionError("forward_rollback_before_request")
            return self._transition(
                evidence,
                phase=HandoffPhase.ROLLED_BACK,
                rollback_commit_sha=rollback_sha,
            )

    def mark_superseded(self, identity: HandoffIdentity, *, reason: str) -> HandoffEvidence:
        """Permanently close an attempt without making it eligible for replay."""

        safe_reason = _safe_token(reason, "superseded_reason")
        with self._lock:
            evidence = self._read(identity)
            if evidence.phase is HandoffPhase.SUPERSEDED:
                if evidence.failure_reason != safe_reason:
                    raise HandoffConflictError("superseded_reason_mismatch")
                return evidence
            if evidence.phase in {
                HandoffPhase.COMPLETED,
                HandoffPhase.ROLLED_BACK,
            }:
                raise HandoffTransitionError("terminal_handoff_cannot_be_superseded")
            return self._transition(
                evidence,
                phase=HandoffPhase.SUPERSEDED,
                failure_reason=safe_reason,
            )

    def recover(self, identity: HandoffIdentity) -> HandoffEvidence:
        """Reload exact durable identity after Launcher/Worker process replacement."""

        with self._lock:
            return self._read(identity)

    def claims_allowed(self, identity: HandoffIdentity) -> bool:
        """Return the durable gate result without creating or replaying work."""

        return self.recover(identity).claims_allowed


class HandoffConsumeStatus(str, Enum):
    """Result of one bounded Launcher-owned handoff consumption attempt."""

    NO_HANDOFF = "NO_HANDOFF"
    WAITING_FOR_WORKER_EXIT = "WAITING_FOR_WORKER_EXIT"
    WAITING_FOR_QUIESCENCE = "WAITING_FOR_QUIESCENCE"
    WAITING_FOR_PROBATION = "WAITING_FOR_PROBATION"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class WorkerRestartObservation:
    """Exact process/tree identity returned by an outer restart operation."""

    launcher_identity: str
    worker_identity: str
    worker_sha: str
    exit_code: int = PROMOTION_RESTART_EXIT_CODE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "launcher_identity",
            _safe_token(self.launcher_identity, "launcher_identity"),
        )
        object.__setattr__(
            self,
            "worker_identity",
            _safe_token(self.worker_identity, "worker_identity"),
        )
        object.__setattr__(
            self,
            "worker_sha",
            validate_sha(self.worker_sha, "worker_sha"),
        )
        if self.exit_code != PROMOTION_RESTART_EXIT_CODE:
            raise HandoffIntegrityError("unexpected_worker_restart_exit_code")


@dataclass(frozen=True, slots=True)
class ForwardRollbackObservation:
    """Proof returned by the outer rollback operation before it is recorded."""

    rollback_commit_sha: str
    forward_only_verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rollback_commit_sha",
            validate_sha(self.rollback_commit_sha, "rollback_commit_sha"),
        )
        if not isinstance(self.forward_only_verified, bool):
            raise HandoffIntegrityError("forward_only_verified_invalid")


@dataclass(frozen=True, slots=True)
class LauncherHandoffActions:
    """Explicit outer-controller actions required to consume one handoff.

    These callbacks are deliberately infrastructure-only.  They must verify
    exact Git identities, use non-force/forward-only operations, and be
    idempotent across a Launcher crash between the side effect and the
    evidence transition.  No callback is a command executor and none may
    invoke or replay Codex for the initiating command.
    """

    validate_identity: Callable[[HandoffIdentity], None]
    worker_stopped: Callable[[HandoffIdentity], bool]
    active_run_ids: Callable[[HandoffIdentity], Sequence[str]]
    ensure_adopted: Callable[[HandoffIdentity], str]
    ensure_worker_restarted: Callable[[HandoffIdentity], WorkerRestartObservation]
    observe_health: Callable[[HandoffIdentity], HealthObservation]
    ensure_forward_rollback: Callable[[HandoffIdentity, str], ForwardRollbackObservation]

    def __post_init__(self) -> None:
        for name in (
            "validate_identity",
            "worker_stopped",
            "active_run_ids",
            "ensure_adopted",
            "ensure_worker_restarted",
            "observe_health",
            "ensure_forward_rollback",
        ):
            if not callable(getattr(self, name)):
                raise HandoffIntegrityError(f"handoff_action_{name}_invalid")


def _unavailable_identity(identity: HandoffIdentity) -> None:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def _unavailable_stopped(identity: HandoffIdentity) -> bool:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def _unavailable_active(identity: HandoffIdentity) -> Sequence[str]:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def _unavailable_adopted(identity: HandoffIdentity) -> str:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def _unavailable_restarted(identity: HandoffIdentity) -> WorkerRestartObservation:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def _unavailable_health(identity: HandoffIdentity) -> HealthObservation:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def _unavailable_rollback(
    identity: HandoffIdentity,
    reason: str,
) -> ForwardRollbackObservation:
    raise HandoffConsumerBlockedError("handoff_actions_unavailable")


def unavailable_handoff_actions() -> LauncherHandoffActions:
    """Return the fail-closed action set used by an unarmed Launcher."""

    return LauncherHandoffActions(
        validate_identity=_unavailable_identity,
        worker_stopped=_unavailable_stopped,
        active_run_ids=_unavailable_active,
        ensure_adopted=_unavailable_adopted,
        ensure_worker_restarted=_unavailable_restarted,
        observe_health=_unavailable_health,
        ensure_forward_rollback=_unavailable_rollback,
    )


@dataclass(frozen=True, slots=True)
class HandoffConsumeResult:
    """Secret-free outcome of one outer-controller consumption step."""

    status: HandoffConsumeStatus
    phase: HandoffPhase | None
    transition_seq: int | None
    reason: str | None = None
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, HandoffConsumeStatus):
            object.__setattr__(self, "status", HandoffConsumeStatus(self.status))
        if self.phase is not None and not isinstance(self.phase, HandoffPhase):
            object.__setattr__(self, "phase", HandoffPhase(self.phase))
        if self.transition_seq is not None:
            object.__setattr__(
                self,
                "transition_seq",
                _positive_int(self.transition_seq, "transition_seq", allow_zero=True),
            )
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _safe_token(self.reason, "consume_reason"),
            )
        normalized = tuple(
            _safe_token(item, "consume_action")
            for item in _sequence_values(self.actions, "consume_actions")
        )
        if len(normalized) > MAX_HANDOFF_TOKENS:
            raise HandoffIntegrityError("consume_actions_too_many")
        object.__setattr__(self, "actions", tuple(dict.fromkeys(normalized)))

    @property
    def terminal(self) -> bool:
        """Return whether this attempt is durably closed and must not replay."""

        return self.status in {
            HandoffConsumeStatus.COMPLETED,
            HandoffConsumeStatus.ROLLED_BACK,
            HandoffConsumeStatus.SUPERSEDED,
        }


class LauncherHandoffConsumer:
    """Consume durable handoff evidence from the surviving Launcher only.

    The consumer performs one bounded step at a time.  Every external action
    is supplied by ``LauncherHandoffActions`` and must be safe to repeat after
    a crash.  The consumer itself never writes Protocol-v2 state, mutates
    Git, starts Codex, or waits for the initiating Worker to drain.
    """

    def __init__(
        self,
        controller: OuterControllerHandoff,
        *,
        actions: LauncherHandoffActions | None = None,
    ) -> None:
        if not isinstance(controller, OuterControllerHandoff):
            raise HandoffIntegrityError("handoff_controller_invalid")
        self.controller = controller
        self.actions = actions or unavailable_handoff_actions()
        self._lock = RLock()

    @staticmethod
    def _active_ids(raw: object) -> tuple[str, ...]:
        values = _sequence_values(raw, "active_run_ids")
        if len(values) > MAX_HANDOFF_RUN_IDS:
            raise HandoffNotQuiescedError("active_run_set_too_large")
        normalized = tuple(sorted({_safe_token(item, "active_run_id") for item in values}))
        return normalized

    @staticmethod
    def _reason(decision: object) -> str:
        reasons = getattr(decision, "reasons", ())
        if isinstance(reasons, tuple) and reasons and isinstance(reasons[0], str):
            return reasons[0]
        return "health_gate_failed"

    @staticmethod
    def _result(
        status: HandoffConsumeStatus,
        evidence: HandoffEvidence | None,
        *,
        reason: str | None = None,
        actions: Sequence[str] = (),
    ) -> HandoffConsumeResult:
        return HandoffConsumeResult(
            status=status,
            phase=evidence.phase if evidence is not None else None,
            transition_seq=evidence.transition_seq if evidence is not None else None,
            reason=reason,
            actions=tuple(actions),
        )

    def _invoke(self, name: str, callback: Callable[..., object], *args: object) -> object:
        try:
            return callback(*args)
        except HandoffError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            # Never copy arbitrary callback text into durable or health
            # evidence: adapter errors can contain paths or credentials.
            raise HandoffConsumerBlockedError(
                f"handoff_action_{name}_failed_{type(exc).__name__}"
            ) from exc

    def consume_after_worker_exit(
        self,
        *,
        worker_exit_code: int | None = None,
        expected_identity: HandoffIdentity | None = None,
    ) -> HandoffConsumeResult:
        """Consume at most one durable lifecycle step after Worker exit.

        A non-terminal handoff is never acted on while the old Worker is
        active or while any RunScope is reported active.  Repeated calls are
        safe: completed, rolled-back and superseded evidence returns without
        invoking any action.
        """

        with self._lock, self.controller.store.locked():
            evidence = self.controller.store.read_optional()
            if evidence is None:
                return self._result(HandoffConsumeStatus.NO_HANDOFF, None)
            identity = evidence.identity
            if expected_identity is not None and expected_identity != identity:
                raise HandoffConflictError("handoff_identity_mismatch")
            if evidence.phase is HandoffPhase.COMPLETED:
                return self._result(HandoffConsumeStatus.COMPLETED, evidence)
            if evidence.phase is HandoffPhase.ROLLED_BACK:
                return self._result(HandoffConsumeStatus.ROLLED_BACK, evidence)
            if evidence.phase is HandoffPhase.SUPERSEDED:
                return self._result(HandoffConsumeStatus.SUPERSEDED, evidence)
            if identity.mode is not AdoptionMode.MANUAL:
                raise HandoffConsumerBlockedError("unattended_adoption_disabled")

            # Re-read through the controller so the deadline and exact
            # identity checks run before any infrastructure callback.  A
            # direct store read is intentionally not sufficient here.
            evidence = self.controller.recover(identity)
            if worker_exit_code != PROMOTION_RESTART_EXIT_CODE:
                raise HandoffConsumerBlockedError("handoff_worker_exit_code_invalid")
            self._invoke(
                "validate_identity",
                self.actions.validate_identity,
                identity,
            )

            actions: list[str] = []
            restart_observation: WorkerRestartObservation | None = None
            if evidence.phase in {HandoffPhase.PREPARED, HandoffPhase.DRAINING}:
                if evidence.phase is HandoffPhase.PREPARED:
                    # DRAINING is the durable no-new-claim boundary.  It is
                    # safe to record while waiting for the old process to
                    # disappear; quiescence/adoption remain forbidden until
                    # the explicit exit and empty active-run proof arrive.
                    self.controller.begin_draining(identity)
                    actions.append("begin_draining")
                    evidence = self.controller.recover(identity)
                stopped = self._invoke(
                    "worker_stopped",
                    self.actions.worker_stopped,
                    identity,
                )
                if not isinstance(stopped, bool):
                    raise HandoffIntegrityError("worker_stopped_result_invalid")
                if not stopped:
                    return self._result(
                        HandoffConsumeStatus.WAITING_FOR_WORKER_EXIT,
                        evidence,
                        reason="worker_still_active",
                    )
                active = self._active_ids(
                    self._invoke("active_run_ids", self.actions.active_run_ids, identity)
                )
                if active:
                    return self._result(
                        HandoffConsumeStatus.WAITING_FOR_QUIESCENCE,
                        evidence,
                        reason="active_run_scopes",
                    )
                evidence = self.controller.recover(identity)
                if evidence.phase is HandoffPhase.DRAINING:
                    self.controller.record_worker_quiesced(
                        identity,
                        active_run_ids=(),
                    )
                    actions.append("record_worker_quiesced")
                evidence = self.controller.recover(identity)

            if evidence.phase is HandoffPhase.QUIESCED:
                self._invoke(
                    "validate_identity",
                    self.actions.validate_identity,
                    identity,
                )
                observed = self._invoke(
                    "ensure_adopted",
                    self.actions.ensure_adopted,
                    identity,
                )
                observed_sha = validate_sha(observed, "observed_adoption_sha")
                if observed_sha != identity.reconciled_adoption_sha:
                    raise HandoffConflictError("adoption_tree_identity_mismatch")
                self.controller.mark_adopted(
                    identity,
                    observed_adoption_sha=observed_sha,
                )
                actions.append("record_adopted")
                evidence = self.controller.recover(identity)

            if evidence.phase is HandoffPhase.ADOPTED:
                restart = self._invoke(
                    "ensure_worker_restarted",
                    self.actions.ensure_worker_restarted,
                    identity,
                )
                if not isinstance(restart, WorkerRestartObservation):
                    raise HandoffIntegrityError("worker_restart_observation_invalid")
                restart_observation = restart
                self.controller.record_worker_restart(
                    identity,
                    launcher_identity=restart.launcher_identity,
                    worker_identity=restart.worker_identity,
                    worker_sha=restart.worker_sha,
                    exit_code=restart.exit_code,
                )
                actions.append("record_worker_restart")
                evidence = self.controller.recover(identity)

            if evidence.phase is HandoffPhase.PROBATION:
                # A Launcher may have crashed after the durable PROBATION
                # transition but before the health sample.  Re-establish the
                # exact replacement process through the idempotent outer
                # action before observing health; this never replays the
                # initiating command.
                if restart_observation is None:
                    restart = self._invoke(
                        "ensure_worker_restarted",
                        self.actions.ensure_worker_restarted,
                        identity,
                    )
                    if not isinstance(restart, WorkerRestartObservation):
                        raise HandoffIntegrityError("worker_restart_observation_invalid")
                    restart_observation = restart
                    evidence = self.controller.record_worker_restart(
                        identity,
                        launcher_identity=restart.launcher_identity,
                        worker_identity=restart.worker_identity,
                        worker_sha=restart.worker_sha,
                        exit_code=restart.exit_code,
                    )
                    actions.append("verify_worker_restart")
                observation = self._invoke(
                    "observe_health",
                    self.actions.observe_health,
                    identity,
                )
                if not isinstance(observation, HealthObservation):
                    raise HandoffHealthError("health_observation_invalid")
                requested = evidence.restart_requested_at
                if requested is None:
                    raise HandoffHealthError("restart_request_evidence_missing")
                contract = HealthGateContract(
                    restart_requested_at=requested,
                    health_deadline=identity.health_deadline,
                    probation_seconds=identity.probation_seconds,
                    health_criteria=identity.health_criteria,
                )
                decision = contract.evaluate(observation)
                if decision.status is HealthGateStatus.READY:
                    if (
                        evidence.observed_launcher_identity is None
                        or evidence.observed_worker_identity is None
                        or evidence.observed_worker_sha is None
                    ):
                        raise HandoffHealthError("probation_identity_incomplete")
                    self.controller.mark_health_ready(
                        identity,
                        observation=observation,
                        launcher_identity=evidence.observed_launcher_identity,
                        worker_identity=evidence.observed_worker_identity,
                        worker_sha=evidence.observed_worker_sha,
                    )
                    actions.append("record_health_ready")
                    evidence = self.controller.recover(identity)
                    return self._result(
                        HandoffConsumeStatus.COMPLETED,
                        evidence,
                        actions=actions,
                    )
                if decision.status in {
                    HealthGateStatus.WAITING_FOR_RESTART,
                    HealthGateStatus.PROBATION,
                }:
                    return self._result(
                        HandoffConsumeStatus.WAITING_FOR_PROBATION,
                        evidence,
                        reason=self._reason(decision),
                        actions=actions,
                    )
                reason = self._reason(decision)
                self.controller.request_rollback(identity, reason=reason)
                actions.append("request_rollback")
                evidence = self.controller.recover(identity)

            if evidence.phase is HandoffPhase.ROLLBACK_REQUIRED:
                reason = evidence.failure_reason
                if reason is None:
                    raise HandoffIntegrityError("rollback_reason_missing")
                rollback = self._invoke(
                    "ensure_forward_rollback",
                    self.actions.ensure_forward_rollback,
                    identity,
                    reason,
                )
                if not isinstance(rollback, ForwardRollbackObservation):
                    raise HandoffIntegrityError("rollback_observation_invalid")
                if not rollback.forward_only_verified:
                    raise HandoffTransitionError("forward_rollback_proof_required")
                self.controller.mark_forward_rollback(
                    identity,
                    rollback_commit_sha=rollback.rollback_commit_sha,
                    forward_only_verified=rollback.forward_only_verified,
                )
                actions.append("record_forward_rollback")
                evidence = self.controller.recover(identity)
                return self._result(
                    HandoffConsumeStatus.ROLLED_BACK,
                    evidence,
                    actions=actions,
                )

            if evidence.phase is HandoffPhase.COMPLETED:
                return self._result(
                    HandoffConsumeStatus.COMPLETED,
                    evidence,
                    actions=actions,
                )
            if evidence.phase is HandoffPhase.ROLLED_BACK:
                return self._result(
                    HandoffConsumeStatus.ROLLED_BACK,
                    evidence,
                    actions=actions,
                )
            raise HandoffTransitionError("handoff_consumer_phase_unhandled")

    def try_consume_after_worker_exit(
        self,
        *,
        worker_exit_code: int | None = None,
        expected_identity: HandoffIdentity | None = None,
    ) -> HandoffConsumeResult:
        """Return a bounded BLOCKED result instead of leaking unsafe details."""

        try:
            return self.consume_after_worker_exit(
                worker_exit_code=worker_exit_code,
                expected_identity=expected_identity,
            )
        except HandoffError as exc:
            phase: HandoffPhase | None = None
            transition_seq: int | None = None
            try:
                evidence = self.controller.store.read_optional()
            except HandoffError:
                evidence = None
            if evidence is not None:
                phase = evidence.phase
                transition_seq = evidence.transition_seq
            return HandoffConsumeResult(
                status=HandoffConsumeStatus.BLOCKED,
                phase=phase,
                transition_seq=transition_seq,
                reason=type(exc).__name__,
            )


__all__ = [
    "HANDOFF_EVIDENCE_FILENAME",
    "HANDOFF_SCHEMA_VERSION",
    "HandoffConflictError",
    "HandoffConsumeResult",
    "HandoffConsumeStatus",
    "HandoffConsumerBlockedError",
    "HandoffError",
    "HandoffEvidence",
    "HandoffEvidenceStore",
    "HandoffHealthError",
    "HandoffIdentity",
    "HandoffIntegrityError",
    "HandoffNotQuiescedError",
    "HandoffPhase",
    "HandoffStaleError",
    "HandoffTransitionError",
    "ForwardRollbackObservation",
    "LauncherHandoffActions",
    "LauncherHandoffConsumer",
    "OuterControllerHandoff",
    "WorkerRestartObservation",
    "unavailable_handoff_actions",
]
