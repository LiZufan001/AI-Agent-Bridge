"""Authority-focused Phase-8.6-A recovery classifier tests."""

from __future__ import annotations

import unittest

from vnext_runtime.models import RunIdentity
from vnext_runtime.recovery_evidence import (
    RECOVERY_ENVELOPE_SCHEMA_VERSION,
    RECOVERY_WAL_SCHEMA_VERSION,
    CanonicalRunSnapshot,
    ContainmentEvidence,
    EvidenceState,
    JournalDisposition,
    LifecycleEvidence,
    PublicationEvidence,
    PushReconciliation,
    PushResolutionState,
    RecoveryClassification,
    RecoveryCommandKind,
    RecoveryEnvelope,
    RecoveryWal,
    RecoveryWalEvent,
    ReportEvidence,
    WalEventKind,
    classify_recovery,
)


class RecoveryAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = RunIdentity("p", 4, "run-004-authority", 8)

    def canonical(
        self,
        *,
        status: str = "CODEX_RUNNING",
        latest_report: int = 3,
        active_identity: RunIdentity | None = None,
    ) -> CanonicalRunSnapshot:
        return CanonicalRunSnapshot(
            project_id="p",
            status=status,
            generation=8,
            latest_command=4,
            latest_report=latest_report,
            active_identity=(
                self.identity
                if active_identity is None and status == "CODEX_RUNNING"
                else active_identity
            ),
        )

    def safe_containment(
        self,
        egress: EvidenceState,
    ) -> ContainmentEvidence:
        return ContainmentEvidence(
            containment_intact=EvidenceState.YES,
            project_egress_observed=egress,
            unclassified_project_egress=EvidenceState.NO,
            local_effects_reconciled=EvidenceState.YES,
        )

    def intent(self) -> RecoveryWalEvent:
        return RecoveryWalEvent(
            sequence=1,
            kind=WalEventKind.PUSH_INTENT,
            recorded_at="2001-01-15T21:30:00+08:00",
            effect_id="push-1",
            target_id="origin/main",
            expected_object="a" * 40,
        )

    def confirmed(self) -> RecoveryWalEvent:
        return RecoveryWalEvent(
            sequence=2,
            kind=WalEventKind.PUSH_CONFIRMED,
            recorded_at="2001-01-15T21:30:01+08:00",
            effect_id="push-1",
            target_id="origin/main",
            expected_object="a" * 40,
            observed_object="a" * 40,
        )

    def envelope(
        self,
        *,
        disposition: JournalDisposition = JournalDisposition.PENDING,
        egress: EvidenceState = EvidenceState.YES,
        events: tuple[RecoveryWalEvent, ...] = (),
        reconciliations: tuple[PushReconciliation, ...] = (),
    ) -> RecoveryEnvelope:
        return RecoveryEnvelope(
            schema_version=RECOVERY_ENVELOPE_SCHEMA_VERSION,
            identity=self.identity,
            command_kind=RecoveryCommandKind.EXECUTE,
            journal_disposition=disposition,
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            report=ReportEvidence(final_marker_valid=EvidenceState.NO),
            containment=self.safe_containment(egress),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
            wal=RecoveryWal(
                schema_version=RECOVERY_WAL_SCHEMA_VERSION,
                identity=self.identity,
                events=events,
            ),
            push_reconciliations=reconciliations,
        )

    def test_local_terminal_evidence_cannot_override_exact_running_lease(self) -> None:
        for disposition in (
            JournalDisposition.RECONCILED,
            JournalDisposition.SUPERSEDED,
        ):
            with self.subTest(disposition=disposition):
                assessment = classify_recovery(
                    self.canonical(),
                    self.envelope(disposition=disposition),
                )
                self.assertEqual(
                    assessment.classification,
                    RecoveryClassification.CONFLICT,
                )
                self.assertEqual(
                    assessment.reason,
                    "local_terminal_evidence_contradicts_exact_running_lease",
                )

    def test_local_terminal_evidence_only_becomes_superseded_after_canonical_advance(self) -> None:
        for disposition in (
            JournalDisposition.RECONCILED,
            JournalDisposition.SUPERSEDED,
        ):
            with self.subTest(disposition=disposition):
                assessment = classify_recovery(
                    self.canonical(status="RECOVERY_REQUIRED", active_identity=None),
                    self.envelope(disposition=disposition),
                )
                self.assertEqual(
                    assessment.classification,
                    RecoveryClassification.SUPERSEDED,
                )

    def test_pending_local_evidence_cannot_guess_through_canonical_advance(self) -> None:
        assessment = classify_recovery(
            self.canonical(status="REPORT_READY", active_identity=None),
            self.envelope(disposition=JournalDisposition.PENDING),
        )
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.CONFLICT,
        )
        self.assertEqual(
            assessment.reason,
            "canonical_state_advanced_while_local_evidence_pending",
        )

    def test_confirmed_push_reconciliation_must_match_exact_target_and_object(self) -> None:
        assessment = classify_recovery(
            self.canonical(),
            self.envelope(
                events=(self.intent(), self.confirmed()),
                reconciliations=(
                    PushReconciliation(
                        effect_id="push-1",
                        target_id="origin/other",
                        expected_object="a" * 40,
                        state=PushResolutionState.EXACT_APPLIED,
                        observed_object="a" * 40,
                    ),
                ),
            ),
        )
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.CONFLICT,
        )
        self.assertEqual(
            assessment.reason,
            "push_reconciliation_identity_mismatch",
        )

    def test_reconciled_applied_push_conflicts_with_positive_no_egress(self) -> None:
        assessment = classify_recovery(
            self.canonical(),
            self.envelope(
                egress=EvidenceState.NO,
                events=(self.intent(),),
                reconciliations=(
                    PushReconciliation(
                        effect_id="push-1",
                        target_id="origin/main",
                        expected_object="a" * 40,
                        state=PushResolutionState.EXACT_APPLIED,
                        observed_object="a" * 40,
                    ),
                ),
            ),
        )
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.CONFLICT,
        )
        self.assertEqual(
            assessment.reason,
            "applied_push_contradicts_positive_no_egress_evidence",
        )

    def test_matching_reconciled_applied_push_with_positive_egress_can_close(self) -> None:
        assessment = classify_recovery(
            self.canonical(),
            self.envelope(
                egress=EvidenceState.YES,
                events=(self.intent(),),
                reconciliations=(
                    PushReconciliation(
                        effect_id="push-1",
                        target_id="origin/main",
                        expected_object="a" * 40,
                        state=PushResolutionState.EXACT_APPLIED,
                        observed_object="a" * 40,
                    ),
                ),
            ),
        )
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        )


if __name__ == "__main__":
    unittest.main()
