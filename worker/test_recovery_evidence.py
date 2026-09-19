"""Phase-8.6-A pure recovery evidence/classifier tests."""

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
    RecoveryEvidenceError,
    RecoveryWal,
    RecoveryWalEvent,
    ReportEvidence,
    WalEventKind,
    classify_recovery,
    envelope_from_legacy_journal,
)


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = RunIdentity("p", 4, "run-004-phase86", 8)

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

    def wal(self, *events: RecoveryWalEvent) -> RecoveryWal:
        return RecoveryWal(
            schema_version=RECOVERY_WAL_SCHEMA_VERSION,
            identity=self.identity,
            events=tuple(events),
        )

    def envelope(
        self,
        *,
        command_kind: RecoveryCommandKind = RecoveryCommandKind.EXECUTE,
        disposition: JournalDisposition = JournalDisposition.PENDING,
        lifecycle: LifecycleEvidence | None = None,
        report: ReportEvidence | None = None,
        containment: ContainmentEvidence | None = None,
        publication: PublicationEvidence | None = None,
        wal: RecoveryWal | None = None,
        reconciliations: tuple[PushReconciliation, ...] = (),
    ) -> RecoveryEnvelope:
        return RecoveryEnvelope(
            schema_version=RECOVERY_ENVELOPE_SCHEMA_VERSION,
            identity=self.identity,
            command_kind=command_kind,
            journal_disposition=disposition,
            lifecycle=lifecycle or LifecycleEvidence(),
            report=report or ReportEvidence(),
            containment=containment or ContainmentEvidence(),
            publication=publication or PublicationEvidence(),
            wal=wal or self.wal(),
            push_reconciliations=reconciliations,
        )

    @staticmethod
    def push_intent(
        *,
        sequence: int = 1,
        effect_id: str = "git-push-1",
        target_id: str = "origin/main",
        expected: str = "a" * 40,
    ) -> RecoveryWalEvent:
        return RecoveryWalEvent(
            sequence=sequence,
            kind=WalEventKind.PUSH_INTENT,
            recorded_at="2001-01-15T21:00:00+08:00",
            effect_id=effect_id,
            target_id=target_id,
            expected_object=expected,
        )

    @staticmethod
    def push_confirm(
        *,
        sequence: int = 2,
        effect_id: str = "git-push-1",
        target_id: str = "origin/main",
        expected: str = "a" * 40,
        observed: str = "a" * 40,
    ) -> RecoveryWalEvent:
        return RecoveryWalEvent(
            sequence=sequence,
            kind=WalEventKind.PUSH_CONFIRMED,
            recorded_at="2001-01-15T21:00:01+08:00",
            effect_id=effect_id,
            target_id=target_id,
            expected_object=expected,
            observed_object=observed,
        )

    def safe_interrupted_containment(
        self,
        *,
        egress: EvidenceState = EvidenceState.NO,
    ) -> ContainmentEvidence:
        return ContainmentEvidence(
            containment_intact=EvidenceState.YES,
            project_egress_observed=egress,
            unclassified_project_egress=EvidenceState.NO,
            local_effects_reconciled=EvidenceState.YES,
        )

    def test_wal_requires_strict_sequence_and_exact_push_confirmation(self) -> None:
        intent = self.push_intent()
        confirmation = self.push_confirm()
        wal = self.wal(intent, confirmation)
        effects = wal.push_effects()
        self.assertEqual(len(effects), 1)
        self.assertTrue(effects[0].confirmed)
        self.assertEqual(effects[0].observed_object, "a" * 40)

        with self.assertRaisesRegex(RecoveryEvidenceError, "strictly increasing"):
            self.wal(intent, self.push_confirm(sequence=1))
        with self.assertRaisesRegex(RecoveryEvidenceError, "prior exact PUSH_INTENT"):
            self.wal(confirmation)
        with self.assertRaisesRegex(RecoveryEvidenceError, "exact intended"):
            self.wal(intent, self.push_confirm(observed="b" * 40))

    def test_wal_intent_without_confirmation_remains_explicit(self) -> None:
        effects = self.wal(self.push_intent()).push_effects()
        self.assertEqual(len(effects), 1)
        self.assertFalse(effects[0].confirmed)
        self.assertIsNone(effects[0].observed_object)

    def test_envelope_rejects_cross_run_wal_identity(self) -> None:
        other = RunIdentity("p", 4, "run-other", 8)
        with self.assertRaisesRegex(RecoveryEvidenceError, "identities do not match"):
            RecoveryEnvelope(
                schema_version=RECOVERY_ENVELOPE_SCHEMA_VERSION,
                identity=self.identity,
                command_kind=RecoveryCommandKind.EXECUTE,
                journal_disposition=JournalDisposition.PENDING,
                lifecycle=LifecycleEvidence(),
                report=ReportEvidence(),
                containment=ContainmentEvidence(),
                publication=PublicationEvidence(),
                wal=RecoveryWal(
                    schema_version=RECOVERY_WAL_SCHEMA_VERSION,
                    identity=other,
                ),
            )

    def test_completed_exact_report_is_publishable_without_effect_inference(self) -> None:
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            report=ReportEvidence(
                final_marker_valid=EvidenceState.YES,
                materialized=EvidenceState.YES,
                integrity_valid=EvidenceState.YES,
            ),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.PUBLISH_COMPLETED_REPORT,
        )
        self.assertFalse(assessment.executor_replay_allowed)

    def test_completed_report_defers_publication_without_changing_semantic_action(self) -> None:
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            report=ReportEvidence(
                final_marker_valid=EvidenceState.YES,
                materialized=EvidenceState.YES,
                integrity_valid=EvidenceState.YES,
            ),
            publication=PublicationEvidence(store_available=EvidenceState.NO),
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.DEFER_PUBLICATION,
        )
        self.assertEqual(
            assessment.deferred_action,
            RecoveryClassification.PUBLISH_COMPLETED_REPORT,
        )

    def test_valid_marker_without_report_integrity_is_never_promoted(self) -> None:
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            report=ReportEvidence(
                final_marker_valid=EvidenceState.YES,
                materialized=EvidenceState.YES,
                integrity_valid=EvidenceState.UNKNOWN,
            ),
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )

    def test_execute_process_never_created_requires_positive_no_effect_evidence(self) -> None:
        safe = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.NO,
                process_exited=EvidenceState.NO,
            ),
            containment=ContainmentEvidence(
                project_egress_observed=EvidenceState.NO,
                local_effects_reconciled=EvidenceState.YES,
            ),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), safe).classification,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        )

        unknown_egress = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.NO,
                process_exited=EvidenceState.NO,
            ),
            containment=ContainmentEvidence(
                project_egress_observed=EvidenceState.UNKNOWN,
                local_effects_reconciled=EvidenceState.YES,
            ),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), unknown_egress).classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )

    def test_finalize_never_closes_without_strict_completed_report(self) -> None:
        envelope = self.envelope(
            command_kind=RecoveryCommandKind.FINALIZE,
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.NO,
                process_exited=EvidenceState.NO,
            ),
            containment=ContainmentEvidence(
                project_egress_observed=EvidenceState.NO,
                local_effects_reconciled=EvidenceState.YES,
            ),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )
        self.assertEqual(assessment.reason, "finalize_completion_not_proven")

    def test_safe_interrupted_created_process_can_close_blocked_without_rerun(self) -> None:
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        )
        self.assertFalse(assessment.executor_replay_allowed)

    def test_missing_lifecycle_or_containment_is_recovery_not_safe(self) -> None:
        self.assertEqual(
            classify_recovery(self.canonical(), self.envelope()).classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )
        uncertain_containment = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=ContainmentEvidence(
                containment_intact=EvidenceState.UNKNOWN,
                project_egress_observed=EvidenceState.NO,
                unclassified_project_egress=EvidenceState.NO,
                local_effects_reconciled=EvidenceState.YES,
            ),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), uncertain_containment).classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )

    def test_generic_project_egress_without_effect_specific_proof_requires_recovery(self) -> None:
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.YES),
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )
        self.assertEqual(
            assessment.reason,
            "project_egress_without_effect_specific_proof",
        )

    def test_unconfirmed_push_intent_requires_exact_read_only_reconciliation(self) -> None:
        wal = self.wal(self.push_intent())
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.YES),
            wal=wal,
        )
        self.assertEqual(
            classify_recovery(self.canonical(), envelope).classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )

        reconciled = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.YES),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
            wal=wal,
            reconciliations=(
                PushReconciliation(
                    effect_id="git-push-1",
                    target_id="origin/main",
                    expected_object="a" * 40,
                    state=PushResolutionState.EXACT_APPLIED,
                    observed_object="a" * 40,
                ),
            ),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), reconciled).classification,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        )

    def test_exact_not_applied_push_can_close_when_no_egress_is_proven(self) -> None:
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.NO),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
            wal=self.wal(self.push_intent()),
            reconciliations=(
                PushReconciliation(
                    effect_id="git-push-1",
                    target_id="origin/main",
                    expected_object="a" * 40,
                    state=PushResolutionState.EXACT_NOT_APPLIED,
                    observed_object="0" * 40,
                ),
            ),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), envelope).classification,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        )

    def test_confirmed_push_requires_matching_egress_and_is_exactly_bound(self) -> None:
        wal = self.wal(self.push_intent(), self.push_confirm())
        envelope = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.YES),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
            wal=wal,
        )
        self.assertEqual(
            classify_recovery(self.canonical(), envelope).classification,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        )

        contradictory = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.NO),
            wal=wal,
        )
        self.assertEqual(
            classify_recovery(self.canonical(), contradictory).classification,
            RecoveryClassification.CONFLICT,
        )

    def test_ambiguous_or_mismatched_push_reconciliation_never_becomes_safe(self) -> None:
        wal = self.wal(self.push_intent())
        ambiguous = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.YES),
            wal=wal,
            reconciliations=(
                PushReconciliation(
                    effect_id="git-push-1",
                    target_id="origin/main",
                    expected_object="a" * 40,
                    state=PushResolutionState.AMBIGUOUS,
                ),
            ),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), ambiguous).classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )

        mismatch = self.envelope(
            lifecycle=LifecycleEvidence(
                process_created=EvidenceState.YES,
                process_exited=EvidenceState.YES,
            ),
            containment=self.safe_interrupted_containment(egress=EvidenceState.YES),
            wal=wal,
            reconciliations=(
                PushReconciliation(
                    effect_id="git-push-1",
                    target_id="origin/other",
                    expected_object="a" * 40,
                    state=PushResolutionState.EXACT_NOT_APPLIED,
                ),
            ),
        )
        self.assertEqual(
            classify_recovery(self.canonical(), mismatch).classification,
            RecoveryClassification.CONFLICT,
        )

    def test_canonical_supersession_and_identity_mismatch_are_distinct(self) -> None:
        envelope = self.envelope()
        self.assertEqual(
            classify_recovery(
                self.canonical(latest_report=4), envelope
            ).classification,
            RecoveryClassification.SUPERSEDED,
        )
        other = RunIdentity("p", 4, "run-other", 8)
        self.assertEqual(
            classify_recovery(
                self.canonical(active_identity=other), envelope
            ).classification,
            RecoveryClassification.CONFLICT,
        )

    def test_local_terminal_evidence_cannot_override_canonical_authority(self) -> None:
        for disposition in (
            JournalDisposition.RECONCILED,
            JournalDisposition.SUPERSEDED,
        ):
            with self.subTest(disposition=disposition):
                self.assertEqual(
                    classify_recovery(
                        self.canonical(),
                        self.envelope(disposition=disposition),
                    ).classification,
                    RecoveryClassification.CONFLICT,
                )
                self.assertEqual(
                    classify_recovery(
                        self.canonical(status="RECOVERY_REQUIRED"),
                        self.envelope(disposition=disposition),
                    ).classification,
                    RecoveryClassification.SUPERSEDED,
                )
        conflict = self.envelope(disposition=JournalDisposition.CONFLICT)
        self.assertEqual(
            classify_recovery(self.canonical(), conflict).classification,
            RecoveryClassification.CONFLICT,
        )

    def test_legacy_network_journal_maps_missing_provenance_to_unknown(self) -> None:
        legacy = {
            "schema_version": 1,
            "project_id": "p",
            "command_id": 4,
            "run_id": "run-004-phase86",
            "claim_generation": 8,
            "journal_status": "pending",
            "marker_status": "NETWORK_INTERRUPTED",
            "worktree_dirty": False,
            "local_commit_created": False,
            "unpushed_commits_present": False,
            "external_side_effects_unknown": True,
        }
        envelope = envelope_from_legacy_journal(legacy)
        self.assertEqual(
            envelope.containment.project_egress_observed,
            EvidenceState.UNKNOWN,
        )
        self.assertEqual(
            envelope.containment.local_effects_reconciled,
            EvidenceState.YES,
        )
        assessment = classify_recovery(self.canonical(), envelope)
        self.assertEqual(
            assessment.classification,
            RecoveryClassification.REQUIRE_RECOVERY,
        )
        self.assertFalse(assessment.executor_replay_allowed)

    def test_invalid_evidence_never_collapses_unknown_to_false(self) -> None:
        with self.assertRaises(RecoveryEvidenceError):
            LifecycleEvidence(
                process_created=EvidenceState.NO,
                process_exited=EvidenceState.YES,
            )
        with self.assertRaises(RecoveryEvidenceError):
            ReportEvidence(
                materialized=EvidenceState.NO,
                integrity_valid=EvidenceState.YES,
            )
        with self.assertRaises(RecoveryEvidenceError):
            ContainmentEvidence(
                project_egress_observed=EvidenceState.NO,
                unclassified_project_egress=EvidenceState.YES,
            )


if __name__ == "__main__":
    unittest.main()
