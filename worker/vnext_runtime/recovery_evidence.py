"""Pure recovery evidence and classification contracts.

This module deliberately owns no I/O. It does not import an executor/provider,
Git store, network client, recovery publisher, or canonical state writer. It
models already-observed facts so later integration can make conservative,
deterministic recovery decisions without replaying an interrupted executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .models import RunIdentity


RECOVERY_ENVELOPE_SCHEMA_VERSION = 1
RECOVERY_WAL_SCHEMA_VERSION = 1


class RecoveryEvidenceError(ValueError):
    """Recovery evidence is malformed, contradictory, or identity-unsafe."""


class EvidenceState(str, Enum):
    """Explicit three-valued evidence; UNKNOWN is never equivalent to NO."""

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class RecoveryCommandKind(str, Enum):
    """Protocol-v2 command kinds relevant to recovery classification."""

    EXECUTE = "EXECUTE"
    FINALIZE = "FINALIZE"


class JournalDisposition(str, Enum):
    """Local evidence lifecycle; never a Protocol-v2 status."""

    PENDING = "PENDING"
    RECONCILED = "RECONCILED"
    SUPERSEDED = "SUPERSEDED"
    CONFLICT = "CONFLICT"


class CanonicalRelation(str, Enum):
    """Relation between one local envelope and a canonical project snapshot."""

    EXACT_RUNNING_LEASE = "EXACT_RUNNING_LEASE"
    REPORT_ALREADY_PUBLISHED = "REPORT_ALREADY_PUBLISHED"
    STATE_ADVANCED = "STATE_ADVANCED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


class RecoveryClassification(str, Enum):
    """Pure recovery action classification; no member means executor replay."""

    NO_ACTION = "NO_ACTION"
    DEFER_PUBLICATION = "DEFER_PUBLICATION"
    PUBLISH_COMPLETED_REPORT = "PUBLISH_COMPLETED_REPORT"
    PUBLISH_INTERRUPTED_REPORT = "PUBLISH_INTERRUPTED_REPORT"
    REQUIRE_RECOVERY = "REQUIRE_RECOVERY"
    SUPERSEDED = "SUPERSEDED"
    CONFLICT = "CONFLICT"


class WalEventKind(str, Enum):
    """Small write-ahead-log vocabulary for facts that change recovery meaning."""

    RUN_CLAIMED = "RUN_CLAIMED"
    PROCESS_CREATE_INTENT = "PROCESS_CREATE_INTENT"
    PROCESS_CREATED = "PROCESS_CREATED"
    PUSH_INTENT = "PUSH_INTENT"
    PUSH_CONFIRMED = "PUSH_CONFIRMED"
    REPORT_MATERIALIZED = "REPORT_MATERIALIZED"
    REPORT_PUBLISH_INTENT = "REPORT_PUBLISH_INTENT"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    TARGETED_TERMINATION_REQUESTED = "TARGETED_TERMINATION_REQUESTED"
    TARGETED_TERMINATION_CONFIRMED = "TARGETED_TERMINATION_CONFIRMED"
    PROCESS_EXITED = "PROCESS_EXITED"
    BROKER_STARTED = "BROKER_STARTED"
    BROKER_STOPPED = "BROKER_STOPPED"
    CONTAINMENT_PREPARED = "CONTAINMENT_PREPARED"
    CONTAINMENT_RECONCILED = "CONTAINMENT_RECONCILED"
    CONTAINMENT_RELEASED = "CONTAINMENT_RELEASED"
    PROJECT_EGRESS_RECONCILED = "PROJECT_EGRESS_RECONCILED"


class PushResolutionState(str, Enum):
    """Read-only reconciliation result for an unconfirmed push intent."""

    EXACT_APPLIED = "EXACT_APPLIED"
    EXACT_NOT_APPLIED = "EXACT_NOT_APPLIED"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RecoveryWalEvent:
    """One bounded, secret-safe WAL event for an exact run."""

    sequence: int
    kind: WalEventKind
    recorded_at: str
    effect_id: str | None = None
    target_id: str | None = None
    expected_object: str | None = None
    observed_object: str | None = None
    artifact_digest: str | None = None
    precondition_object: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.sequence, "sequence", minimum=1)
        object.__setattr__(self, "kind", _enum_value(WalEventKind, self.kind, "kind"))
        _require_text(self.recorded_at, "recorded_at")
        for label in (
            "effect_id",
            "target_id",
            "expected_object",
            "observed_object",
            "artifact_digest",
            "precondition_object",
        ):
            value = getattr(self, label)
            if value is not None:
                _require_text(value, label)

        if self.kind is WalEventKind.PUSH_INTENT:
            _require_text(self.effect_id, "effect_id")
            _require_text(self.target_id, "target_id")
            _require_text(self.expected_object, "expected_object")
            if self.observed_object is not None:
                raise RecoveryEvidenceError(
                    "PUSH_INTENT cannot claim an observed_object before confirmation"
                )
        elif self.kind is WalEventKind.PUSH_CONFIRMED:
            _require_text(self.effect_id, "effect_id")
            _require_text(self.target_id, "target_id")
            _require_text(self.expected_object, "expected_object")
            _require_text(self.observed_object, "observed_object")
        elif self.kind in {
            WalEventKind.REPORT_MATERIALIZED,
            WalEventKind.REPORT_PUBLISH_INTENT,
            WalEventKind.REPORT_PUBLISHED,
        }:
            _require_text(self.artifact_digest, "artifact_digest")


@dataclass(frozen=True, slots=True)
class PushWalEffect:
    """Derived push intent/confirmation pair from a validated WAL."""

    effect_id: str
    target_id: str
    expected_object: str
    confirmed: bool
    observed_object: str | None = None
    precondition_object: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.effect_id, "effect_id")
        _require_text(self.target_id, "target_id")
        _require_text(self.expected_object, "expected_object")
        if not isinstance(self.confirmed, bool):
            raise RecoveryEvidenceError("confirmed must be boolean")
        if self.confirmed:
            _require_text(self.observed_object, "observed_object")
        elif self.observed_object is not None:
            raise RecoveryEvidenceError(
                "an unconfirmed push effect cannot contain observed_object"
            )

    @property
    def recorded_precondition(self) -> str | None:
        """Compatibility name for the exact pre-write remote identity."""

        return self.precondition_object


@dataclass(frozen=True, slots=True)
class RecoveryWal:
    """Validated append-order evidence for one exact run."""

    schema_version: int
    identity: RunIdentity
    events: tuple[RecoveryWalEvent, ...] = ()

    def __post_init__(self) -> None:
        _require_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != RECOVERY_WAL_SCHEMA_VERSION:
            raise RecoveryEvidenceError("unsupported recovery WAL schema_version")
        if not isinstance(self.identity, RunIdentity):
            raise RecoveryEvidenceError("WAL identity must be a RunIdentity")
        if not isinstance(self.events, tuple):
            raise RecoveryEvidenceError("WAL events must be an immutable tuple")

        previous_sequence = 0
        intents: dict[str, RecoveryWalEvent] = {}
        confirmations: set[str] = set()
        for event in self.events:
            if not isinstance(event, RecoveryWalEvent):
                raise RecoveryEvidenceError("WAL contains a non-event value")
            if event.sequence <= previous_sequence:
                raise RecoveryEvidenceError(
                    "WAL event sequence must be strictly increasing"
                )
            previous_sequence = event.sequence
            if event.kind is WalEventKind.PUSH_INTENT:
                assert event.effect_id is not None
                if event.effect_id in intents:
                    raise RecoveryEvidenceError(
                        "duplicate PUSH_INTENT effect_id is ambiguous"
                    )
                intents[event.effect_id] = event
            elif event.kind is WalEventKind.PUSH_CONFIRMED:
                assert event.effect_id is not None
                intent = intents.get(event.effect_id)
                if intent is None:
                    raise RecoveryEvidenceError(
                        "PUSH_CONFIRMED requires a prior exact PUSH_INTENT"
                    )
                if event.effect_id in confirmations:
                    raise RecoveryEvidenceError(
                        "duplicate PUSH_CONFIRMED effect_id is ambiguous"
                    )
                if (
                    event.target_id != intent.target_id
                    or event.expected_object != intent.expected_object
                    or event.observed_object != intent.expected_object
                    or (
                        intent.precondition_object is not None
                        and event.precondition_object != intent.precondition_object
                    )
                ):
                    raise RecoveryEvidenceError(
                        "PUSH_CONFIRMED does not bind the exact intended target/object"
                    )
                confirmations.add(event.effect_id)

    def push_effects(self) -> tuple[PushWalEffect, ...]:
        """Return push effects in intent order after exact confirmation checks."""

        intents: list[RecoveryWalEvent] = []
        confirmations: dict[str, RecoveryWalEvent] = {}
        for event in self.events:
            if event.kind is WalEventKind.PUSH_INTENT:
                intents.append(event)
            elif event.kind is WalEventKind.PUSH_CONFIRMED:
                assert event.effect_id is not None
                confirmations[event.effect_id] = event
        effects: list[PushWalEffect] = []
        for intent in intents:
            assert intent.effect_id is not None
            assert intent.target_id is not None
            assert intent.expected_object is not None
            confirmation = confirmations.get(intent.effect_id)
            effects.append(
                PushWalEffect(
                    effect_id=intent.effect_id,
                    target_id=intent.target_id,
                    expected_object=intent.expected_object,
                    confirmed=confirmation is not None,
                    observed_object=(
                        confirmation.observed_object if confirmation is not None else None
                    ),
                    precondition_object=intent.precondition_object,
                )
            )
        return tuple(effects)


@dataclass(frozen=True, slots=True)
class PushReconciliation:
    """Read-only external observation for one unconfirmed exact push intent."""

    effect_id: str
    target_id: str
    expected_object: str
    state: PushResolutionState
    observed_object: str | None = None
    precondition_object: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.effect_id, "effect_id")
        _require_text(self.target_id, "target_id")
        _require_text(self.expected_object, "expected_object")
        object.__setattr__(
            self,
            "state",
            _enum_value(PushResolutionState, self.state, "state"),
        )
        if self.observed_object is not None:
            _require_text(self.observed_object, "observed_object")
        if self.precondition_object is not None:
            _require_text(self.precondition_object, "precondition_object")
        if self.state is PushResolutionState.EXACT_APPLIED:
            if self.observed_object != self.expected_object:
                raise RecoveryEvidenceError(
                    "EXACT_APPLIED must observe the exact expected object"
                )
        if (
            self.state is PushResolutionState.EXACT_NOT_APPLIED
            and self.precondition_object is not None
            and self.observed_object != self.precondition_object
        ):
            raise RecoveryEvidenceError(
                "EXACT_NOT_APPLIED must observe the exact recorded precondition"
            )

    @property
    def recorded_precondition(self) -> str | None:
        """Compatibility name for the exact pre-write remote identity."""

        return self.precondition_object


@dataclass(frozen=True, slots=True)
class LifecycleEvidence:
    """Positive/negative/unknown process lifecycle facts."""

    process_created: EvidenceState = EvidenceState.UNKNOWN
    process_exited: EvidenceState = EvidenceState.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "process_created",
            _enum_value(EvidenceState, self.process_created, "process_created"),
        )
        object.__setattr__(
            self,
            "process_exited",
            _enum_value(EvidenceState, self.process_exited, "process_exited"),
        )
        if (
            self.process_created is EvidenceState.NO
            and self.process_exited is EvidenceState.YES
        ):
            raise RecoveryEvidenceError(
                "process cannot be positively exited when creation is positively absent"
            )


@dataclass(frozen=True, slots=True)
class ReportEvidence:
    """Integrity facts for an already-produced executor result/report."""

    final_marker_valid: EvidenceState = EvidenceState.UNKNOWN
    materialized: EvidenceState = EvidenceState.UNKNOWN
    integrity_valid: EvidenceState = EvidenceState.UNKNOWN
    outcome: str | None = None

    def __post_init__(self) -> None:
        for label in ("final_marker_valid", "materialized", "integrity_valid"):
            object.__setattr__(
                self,
                label,
                _enum_value(EvidenceState, getattr(self, label), label),
            )
        if (
            self.integrity_valid is EvidenceState.YES
            and self.materialized is not EvidenceState.YES
        ):
            raise RecoveryEvidenceError(
                "report integrity cannot be proven without materialized report evidence"
            )
        if self.outcome is not None and self.outcome not in {
            "SUCCESS",
            "FAILED",
            "BLOCKED",
        }:
            raise RecoveryEvidenceError("report outcome is not a Protocol-v2 result")


@dataclass(frozen=True, slots=True)
class ContainmentEvidence:
    """Run-owned network/local-effect evidence used only for interruption safety."""

    containment_intact: EvidenceState = EvidenceState.UNKNOWN
    project_egress_observed: EvidenceState = EvidenceState.UNKNOWN
    unclassified_project_egress: EvidenceState = EvidenceState.UNKNOWN
    local_effects_reconciled: EvidenceState = EvidenceState.UNKNOWN

    def __post_init__(self) -> None:
        for label in (
            "containment_intact",
            "project_egress_observed",
            "unclassified_project_egress",
            "local_effects_reconciled",
        ):
            object.__setattr__(
                self,
                label,
                _enum_value(EvidenceState, getattr(self, label), label),
            )
        if (
            self.project_egress_observed is EvidenceState.NO
            and self.unclassified_project_egress is EvidenceState.YES
        ):
            raise RecoveryEvidenceError(
                "unclassified project egress contradicts positive no-egress evidence"
            )


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    """Availability of the existing canonical publication path."""

    store_available: EvidenceState = EvidenceState.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "store_available",
            _enum_value(EvidenceState, self.store_available, "store_available"),
        )


@dataclass(frozen=True, slots=True)
class CanonicalRunSnapshot:
    """Small immutable canonical view needed by the pure classifier."""

    project_id: str
    status: str
    generation: int
    latest_command: int
    latest_report: int
    active_identity: RunIdentity | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.status, "status")
        _require_int(self.generation, "generation", minimum=0)
        _require_int(self.latest_command, "latest_command", minimum=0)
        _require_int(self.latest_report, "latest_report", minimum=0)
        if self.active_identity is not None and not isinstance(
            self.active_identity, RunIdentity
        ):
            raise RecoveryEvidenceError(
                "active_identity must be a RunIdentity or None"
            )

    def relation_to(self, identity: RunIdentity) -> CanonicalRelation:
        """Classify canonical relation without mutating or interpreting history."""

        if identity.project_id != self.project_id:
            return CanonicalRelation.IDENTITY_MISMATCH
        if self.latest_report >= identity.command_id:
            return CanonicalRelation.REPORT_ALREADY_PUBLISHED
        if self.status == "CODEX_RUNNING":
            if self.active_identity == identity:
                return CanonicalRelation.EXACT_RUNNING_LEASE
            return CanonicalRelation.IDENTITY_MISMATCH
        return CanonicalRelation.STATE_ADVANCED


@dataclass(frozen=True, slots=True)
class RecoveryEnvelope:
    """Schema-versioned collection of exact-run evidence for pure classification."""

    schema_version: int
    identity: RunIdentity
    command_kind: RecoveryCommandKind
    journal_disposition: JournalDisposition
    lifecycle: LifecycleEvidence
    report: ReportEvidence
    containment: ContainmentEvidence
    publication: PublicationEvidence
    wal: RecoveryWal
    push_reconciliations: tuple[PushReconciliation, ...] = ()

    def __post_init__(self) -> None:
        _require_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != RECOVERY_ENVELOPE_SCHEMA_VERSION:
            raise RecoveryEvidenceError(
                "unsupported recovery envelope schema_version"
            )
        if not isinstance(self.identity, RunIdentity):
            raise RecoveryEvidenceError("envelope identity must be a RunIdentity")
        object.__setattr__(
            self,
            "command_kind",
            _enum_value(RecoveryCommandKind, self.command_kind, "command_kind"),
        )
        object.__setattr__(
            self,
            "journal_disposition",
            _enum_value(
                JournalDisposition,
                self.journal_disposition,
                "journal_disposition",
            ),
        )
        if not isinstance(self.lifecycle, LifecycleEvidence):
            raise RecoveryEvidenceError("lifecycle must be LifecycleEvidence")
        if not isinstance(self.report, ReportEvidence):
            raise RecoveryEvidenceError("report must be ReportEvidence")
        if not isinstance(self.containment, ContainmentEvidence):
            raise RecoveryEvidenceError("containment must be ContainmentEvidence")
        if not isinstance(self.publication, PublicationEvidence):
            raise RecoveryEvidenceError("publication must be PublicationEvidence")
        if not isinstance(self.wal, RecoveryWal):
            raise RecoveryEvidenceError("wal must be RecoveryWal")
        if self.wal.identity != self.identity:
            raise RecoveryEvidenceError(
                "recovery envelope and WAL identities do not match"
            )
        if not isinstance(self.push_reconciliations, tuple) or any(
            not isinstance(item, PushReconciliation)
            for item in self.push_reconciliations
        ):
            raise RecoveryEvidenceError(
                "push_reconciliations must be an immutable tuple of PushReconciliation"
            )
        seen: set[str] = set()
        for item in self.push_reconciliations:
            if item.effect_id in seen:
                raise RecoveryEvidenceError(
                    "duplicate push reconciliation effect_id is ambiguous"
                )
            seen.add(item.effect_id)


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """Pure decision result; automatic executor replay is structurally absent."""

    classification: RecoveryClassification
    reason: str
    deferred_action: RecoveryClassification | None = None
    executor_replay_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "classification",
            _enum_value(
                RecoveryClassification,
                self.classification,
                "classification",
            ),
        )
        _require_text(self.reason, "reason")
        if self.deferred_action is not None:
            object.__setattr__(
                self,
                "deferred_action",
                _enum_value(
                    RecoveryClassification,
                    self.deferred_action,
                    "deferred_action",
                ),
            )
        if self.executor_replay_allowed:
            raise RecoveryEvidenceError(
                "Recovery assessments cannot authorize executor replay"
            )
        if self.classification is RecoveryClassification.DEFER_PUBLICATION:
            if self.deferred_action not in {
                RecoveryClassification.PUBLISH_COMPLETED_REPORT,
                RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
            }:
                raise RecoveryEvidenceError(
                    "deferred publication must preserve the exact publication action"
                )
        elif self.deferred_action is not None:
            raise RecoveryEvidenceError(
                "deferred_action is only valid for DEFER_PUBLICATION"
            )


def classify_recovery(
    canonical: CanonicalRunSnapshot,
    envelope: RecoveryEnvelope,
) -> RecoveryAssessment:
    """Classify exact-run evidence without I/O, mutation, or executor invocation."""

    if not isinstance(canonical, CanonicalRunSnapshot):
        raise RecoveryEvidenceError("canonical must be CanonicalRunSnapshot")
    if not isinstance(envelope, RecoveryEnvelope):
        raise RecoveryEvidenceError("envelope must be RecoveryEnvelope")

    disposition = envelope.journal_disposition
    if disposition is JournalDisposition.CONFLICT:
        return _assessment(RecoveryClassification.CONFLICT, "local_evidence_conflict")

    relation = canonical.relation_to(envelope.identity)
    if relation is CanonicalRelation.REPORT_ALREADY_PUBLISHED:
        return _assessment(
            RecoveryClassification.SUPERSEDED,
            "canonical_report_already_published",
        )
    if relation is CanonicalRelation.IDENTITY_MISMATCH:
        return _assessment(
            RecoveryClassification.CONFLICT,
            "canonical_identity_mismatch",
        )
    if relation is CanonicalRelation.STATE_ADVANCED:
        if disposition in {
            JournalDisposition.RECONCILED,
            JournalDisposition.SUPERSEDED,
        }:
            return _assessment(
                RecoveryClassification.SUPERSEDED,
                "canonical_state_advanced_past_local_terminal_evidence",
            )
        return _assessment(
            RecoveryClassification.CONFLICT,
            "canonical_state_advanced_while_local_evidence_pending",
        )

    # Canonical exact running lease is the strongest authority. A local journal
    # claiming terminal reconciliation/supersession cannot silence a still-live
    # exact lease merely because its local status was written first.
    if disposition in {
        JournalDisposition.RECONCILED,
        JournalDisposition.SUPERSEDED,
    }:
        return _assessment(
            RecoveryClassification.CONFLICT,
            "local_terminal_evidence_contradicts_exact_running_lease",
        )

    completed = _completed_report_proven(envelope)
    if completed:
        return _publication_assessment(
            RecoveryClassification.PUBLISH_COMPLETED_REPORT,
            "completed_report_proven",
            envelope.publication,
        )

    if envelope.report.final_marker_valid is EvidenceState.YES:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "final_marker_without_complete_report_integrity",
        )

    if envelope.report.outcome == "SUCCESS":
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "interrupted_report_claims_success",
        )

    if envelope.command_kind is RecoveryCommandKind.FINALIZE:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "finalize_completion_not_proven",
        )

    lifecycle = envelope.lifecycle
    containment = envelope.containment
    push_status = _evaluate_push_effects(envelope)
    if push_status.classification is RecoveryClassification.CONFLICT:
        return push_status

    if lifecycle.process_created is EvidenceState.UNKNOWN:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "process_creation_unknown",
        )

    if lifecycle.process_created is EvidenceState.NO:
        if envelope.wal.push_effects():
            return _assessment(
                RecoveryClassification.CONFLICT,
                "push_wal_exists_without_created_executor",
            )
        if containment.project_egress_observed is not EvidenceState.NO:
            return _assessment(
                RecoveryClassification.REQUIRE_RECOVERY,
                "no_process_but_no_positive_no_egress_evidence",
            )
        if containment.local_effects_reconciled is not EvidenceState.YES:
            return _assessment(
                RecoveryClassification.REQUIRE_RECOVERY,
                "no_process_local_effects_not_proven_clear",
            )
        return _publication_assessment(
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
            "executor_process_never_created",
            envelope.publication,
        )

    if lifecycle.process_exited is not EvidenceState.YES:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "executor_exit_not_proven",
        )
    if containment.containment_intact is not EvidenceState.YES:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "containment_integrity_not_proven",
        )
    if containment.local_effects_reconciled is not EvidenceState.YES:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "local_effects_not_reconciled",
        )
    if containment.project_egress_observed is EvidenceState.UNKNOWN:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "project_egress_unknown",
        )
    if containment.unclassified_project_egress is not EvidenceState.NO:
        return _assessment(
            RecoveryClassification.REQUIRE_RECOVERY,
            "unclassified_project_egress_not_proven_absent",
        )

    if push_status.classification is RecoveryClassification.REQUIRE_RECOVERY:
        return push_status

    push_effects = envelope.wal.push_effects()
    if containment.project_egress_observed is EvidenceState.YES:
        if not push_effects:
            return _assessment(
                RecoveryClassification.REQUIRE_RECOVERY,
                "project_egress_without_effect_specific_proof",
            )
    elif _any_push_applied(envelope):
        return _assessment(
            RecoveryClassification.CONFLICT,
            "applied_push_contradicts_positive_no_egress_evidence",
        )

    return _publication_assessment(
        RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        "interrupted_run_effects_reconciled",
        envelope.publication,
    )


def envelope_from_legacy_journal(
    journal: Mapping[str, object],
    *,
    command_kind: RecoveryCommandKind = RecoveryCommandKind.EXECUTE,
) -> RecoveryEnvelope:
    """Map schema-v1 legacy journal facts conservatively into the new envelope.

    Legacy journals predate broker/WAL provenance. Even a legacy boolean named
    ``external_side_effects_unknown`` cannot prove positive no-egress evidence,
    so missing provenance remains UNKNOWN and classification stays conservative.
    """

    if not isinstance(journal, Mapping):
        raise RecoveryEvidenceError("legacy journal must be a mapping")
    identity = RunIdentity(
        project_id=_mapping_text(journal, "project_id"),
        command_id=_mapping_int(journal, "command_id", minimum=1),
        run_id=_mapping_text(journal, "run_id"),
        claim_generation=_mapping_int(journal, "claim_generation", minimum=0),
    )
    disposition = _legacy_disposition(journal.get("journal_status"))

    worktree_dirty = _optional_bool(journal.get("worktree_dirty"))
    local_commit = _optional_bool(journal.get("local_commit_created"))
    unpushed = _optional_bool(journal.get("unpushed_commits_present"))
    local_effects = EvidenceState.UNKNOWN
    if any(value is True for value in (worktree_dirty, local_commit, unpushed)):
        local_effects = EvidenceState.NO
    elif all(value is False for value in (worktree_dirty, local_commit, unpushed)):
        local_effects = EvidenceState.YES

    marker = str(journal.get("marker_status") or "").strip().upper()
    marker_state = (
        EvidenceState.NO
        if marker in {"NETWORK_INTERRUPTED", "MISSING", "INVALID", "FAILED", "BLOCKED"}
        else EvidenceState.UNKNOWN
    )

    return RecoveryEnvelope(
        schema_version=RECOVERY_ENVELOPE_SCHEMA_VERSION,
        identity=identity,
        command_kind=command_kind,
        journal_disposition=disposition,
        lifecycle=LifecycleEvidence(),
        report=ReportEvidence(final_marker_valid=marker_state),
        containment=ContainmentEvidence(
            containment_intact=EvidenceState.UNKNOWN,
            project_egress_observed=EvidenceState.UNKNOWN,
            unclassified_project_egress=EvidenceState.UNKNOWN,
            local_effects_reconciled=local_effects,
        ),
        publication=PublicationEvidence(store_available=EvidenceState.UNKNOWN),
        wal=RecoveryWal(
            schema_version=RECOVERY_WAL_SCHEMA_VERSION,
            identity=identity,
        ),
    )


def _completed_report_proven(envelope: RecoveryEnvelope) -> bool:
    return (
        envelope.lifecycle.process_created is EvidenceState.YES
        and envelope.lifecycle.process_exited is EvidenceState.YES
        and envelope.report.final_marker_valid is EvidenceState.YES
        and envelope.report.materialized is EvidenceState.YES
        and envelope.report.integrity_valid is EvidenceState.YES
    )


def _evaluate_push_effects(envelope: RecoveryEnvelope) -> RecoveryAssessment:
    effects = envelope.wal.push_effects()
    reconciliations = {
        item.effect_id: item for item in envelope.push_reconciliations
    }
    effect_ids = {item.effect_id for item in effects}
    extra = sorted(set(reconciliations) - effect_ids)
    if extra:
        return _assessment(
            RecoveryClassification.CONFLICT,
            "push_reconciliation_without_matching_intent",
        )

    for effect in effects:
        reconciliation = reconciliations.get(effect.effect_id)
        if reconciliation is not None and (
            reconciliation.target_id != effect.target_id
            or reconciliation.expected_object != effect.expected_object
            or (
                effect.precondition_object is not None
                and reconciliation.precondition_object != effect.precondition_object
            )
        ):
            return _assessment(
                RecoveryClassification.CONFLICT,
                "push_reconciliation_identity_mismatch",
            )
        if effect.confirmed:
            if reconciliation is not None and reconciliation.state is not PushResolutionState.EXACT_APPLIED:
                return _assessment(
                    RecoveryClassification.CONFLICT,
                    "push_confirmation_contradicts_reconciliation",
                )
            continue
        if reconciliation is None:
            return _assessment(
                RecoveryClassification.REQUIRE_RECOVERY,
                "push_intent_unresolved",
            )
        if reconciliation.state in {
            PushResolutionState.AMBIGUOUS,
            PushResolutionState.UNKNOWN,
        }:
            return _assessment(
                RecoveryClassification.REQUIRE_RECOVERY,
                "push_reconciliation_ambiguous",
            )
        if (
            effect.precondition_object is not None
            and reconciliation.state is PushResolutionState.EXACT_NOT_APPLIED
            and reconciliation.observed_object != effect.precondition_object
        ):
            return _assessment(
                RecoveryClassification.CONFLICT,
                "push_reconciliation_precondition_mismatch",
            )
    return _assessment(RecoveryClassification.NO_ACTION, "push_effects_resolved")


def _any_push_applied(envelope: RecoveryEnvelope) -> bool:
    """Return only positively proven applied effects; never infer from absence."""

    effects = envelope.wal.push_effects()
    reconciliations = {
        item.effect_id: item for item in envelope.push_reconciliations
    }
    return any(
        effect.confirmed
        or (
            reconciliations.get(effect.effect_id) is not None
            and reconciliations[effect.effect_id].state
            is PushResolutionState.EXACT_APPLIED
        )
        for effect in effects
    )


def _publication_assessment(
    action: RecoveryClassification,
    reason: str,
    publication: PublicationEvidence,
) -> RecoveryAssessment:
    if publication.store_available is EvidenceState.YES:
        return _assessment(action, reason)
    return RecoveryAssessment(
        classification=RecoveryClassification.DEFER_PUBLICATION,
        reason=f"{reason}_publication_unavailable",
        deferred_action=action,
    )


def _assessment(
    classification: RecoveryClassification,
    reason: str,
) -> RecoveryAssessment:
    return RecoveryAssessment(classification=classification, reason=reason)


def _legacy_disposition(value: object) -> JournalDisposition:
    text = str(value or "pending").strip().casefold()
    mapping = {
        "pending": JournalDisposition.PENDING,
        "reconciled": JournalDisposition.RECONCILED,
        "superseded": JournalDisposition.SUPERSEDED,
        "conflict": JournalDisposition.CONFLICT,
    }
    try:
        return mapping[text]
    except KeyError as exc:
        raise RecoveryEvidenceError(
            "legacy journal has unsupported journal_status"
        ) from exc


def _mapping_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RecoveryEvidenceError(f"legacy journal {key} must be non-empty text")
    return value


def _mapping_int(
    mapping: Mapping[str, object],
    key: str,
    *,
    minimum: int,
) -> int:
    value = mapping.get(key)
    _require_int(value, key, minimum=minimum)
    assert isinstance(value, int)
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RecoveryEvidenceError("legacy boolean evidence must be bool or None")
    return value


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryEvidenceError(f"{label} must be non-empty text")


def _require_int(value: object, label: str, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RecoveryEvidenceError(
            f"{label} must be an integer >= {minimum}"
        )


def _enum_value(enum_type: type[Enum], value: object, label: str) -> Enum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryEvidenceError(f"{label} has unsupported value") from exc


__all__ = [
    "RECOVERY_ENVELOPE_SCHEMA_VERSION",
    "RECOVERY_WAL_SCHEMA_VERSION",
    "CanonicalRelation",
    "CanonicalRunSnapshot",
    "ContainmentEvidence",
    "EvidenceState",
    "JournalDisposition",
    "LifecycleEvidence",
    "PublicationEvidence",
    "PushReconciliation",
    "PushResolutionState",
    "PushWalEffect",
    "RecoveryAssessment",
    "RecoveryClassification",
    "RecoveryCommandKind",
    "RecoveryEnvelope",
    "RecoveryEvidenceError",
    "RecoveryWal",
    "RecoveryWalEvent",
    "ReportEvidence",
    "WalEventKind",
    "classify_recovery",
    "envelope_from_legacy_journal",
]
