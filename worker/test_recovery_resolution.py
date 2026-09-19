import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_common
import protocol_core as pc
import recovery_journal as rj
import recovery_resolution as rr


PROJECT_ID = "example-service"
COMMAND_ID = 7
RUN_ID = "run-007-0123456789ab"
CLAIM_GENERATION = 8
EXPECTED_GENERATION = 9
SOURCE = "manual_chatgpt"
KIND = "EXECUTE"
PENDING_RELATIVE = "worker/runtime/example-service/pending-report-007.md"


def recovery_state(**updates):
    state = {
        "protocol_version": 2,
        "project_id": PROJECT_ID,
        "status": "RECOVERY_REQUIRED",
        "generation": EXPECTED_GENERATION,
        "latest_command": COMMAND_ID,
        "latest_report": COMMAND_ID - 1,
        "last_reviewed_report": COMMAND_ID - 1,
        "active_run": None,
        "finalized": False,
        "human_required": False,
        "worker_pid": None,
        "recovery_reason": (
            f"network guard interrupted Codex run {RUN_ID}; "
            "automatic rerun prohibited"
        ),
        "last_execution_error": "network guard interruption; manual recovery required",
    }
    state.update(updates)
    return state


def recovery_journal(**updates):
    journal = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "command_id": COMMAND_ID,
        "run_id": RUN_ID,
        "claim_generation": CLAIM_GENERATION,
        "interrupted_at": "2024-01-01T01:00:00+00:00",
        "interruption_kind": "network_guard",
        "interruption_reason_safe": "network guard became unsafe: probe failed or timed out",
        "claimed_at": "2024-01-01T00:00:00+00:00",
        "lease_expires_at": "2024-01-01T04:00:00+00:00",
        "head_before": "a" * 40,
        "head_after": "a" * 40,
        "worktree_dirty": False,
        "local_commit_created": False,
        "unpushed_commits_present": False,
        "report_path": "projects/example-service/reports/report-007.md",
        "pending_report_path": PENDING_RELATIVE,
        "marker_status": "PROCESS_TERMINATED_WITHOUT_MARKER",
        "process_exit_code": 123,
        "termination_reason": "network_guard",
        "external_side_effects_unknown": True,
        "remote_publish_pending": False,
        "journal_status": "reconciled",
        "reconciled_at": "2024-01-01T01:01:00+00:00",
        "reconciliation_reason": "remote RECOVERY_REQUIRED publication confirmed",
    }
    journal.update(updates)
    return journal


def pending_text(*, outcome="NETWORK_INTERRUPTED", include_kind=False):
    kind_line = "- kind: EXECUTE\n" if include_kind else ""
    return (
        "# Report 007 — example-service\n\n"
        f"- command_id: 7\n- outcome: {outcome}\n"
        "- source: manual_chatgpt\n"
        f"{kind_line}"
        "- based_on_report: 6\n"
        f"- run_id: `{RUN_ID}`\n"
        f"- claim_generation: {CLAIM_GENERATION}\n\n"
        "## Codex final response\n\n"
        "(No final Codex message was captured.)\n\n"
        "network guard interruption; no final success marker; "
        "external_side_effects_unknown=true\n"
    )


class RecoveryResolutionFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        # recovery_resolution normalizes the bridge root before constructing
        # CAS payload paths.  Resolve the fixture root too so Windows 8.3
        # aliases on hosted runners do not make equivalent paths compare
        # unequal in the fake publisher.
        self.root = Path(self.temp_dir.name).resolve()
        project = self.root / "projects" / PROJECT_ID
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir(parents=True)
        (self.root / "worker" / "runtime" / PROJECT_ID).mkdir(parents=True)
        (project / "state.json").write_text(
            bridge_common.json_text(recovery_state()),
            encoding="utf-8",
        )
        (project / "commands" / "command-007.md").write_text(
            (
                '<!-- bridge-command: {"command_id":7,"source":"manual_chatgpt",'
                '"based_on_report":6,"expected_generation":7,"kind":"EXECUTE",'
                '"executor":{"model":"synthetic-model","reasoning_effort":"high"}} -->\n'
                "# Command 007 — synthetic fixture\n"
            ),
            encoding="utf-8",
        )
        pending_path = self.root / PENDING_RELATIVE
        pending_path.write_text(pending_text(), encoding="utf-8")
        journal_path = rj.journal_path(self.root, PROJECT_ID, RUN_ID)
        rj.write_journal(journal_path, recovery_journal())

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self):
        return rr.ResolutionRequest(
            bridge_root=self.root,
            project_id=PROJECT_ID,
            command_id=COMMAND_ID,
            run_id=RUN_ID,
            claim_generation=CLAIM_GENERATION,
            expected_generation=EXPECTED_GENERATION,
            source=SOURCE,
            kind=KIND,
        )

    def pending_digest(self):
        return hashlib.sha256(
            (self.root / PENDING_RELATIVE).read_bytes()
        ).hexdigest()

    def test_inspect_is_read_only_and_binds_the_synthetic_identity(self):
        state_before = (self.root / "projects" / PROJECT_ID / "state.json").read_bytes()
        with patch.object(rr.git_store, "publish_cas") as publish:
            evidence = rr.inspect_resolution(self.request())
        publish.assert_not_called()
        self.assertEqual(evidence.decision, "ALLOW")
        self.assertEqual(evidence.pending_report_sha256, self.pending_digest())
        self.assertEqual(evidence.pending_report_header["outcome"], "NETWORK_INTERRUPTED")
        self.assertEqual(evidence.canonical_outcome, "BLOCKED")
        self.assertFalse(evidence.report_byte_identical_to_pending)
        self.assertFalse(evidence.canonical_report_path.exists())
        self.assertEqual(
            state_before,
            (self.root / "projects" / PROJECT_ID / "state.json").read_bytes(),
        )

    def test_resolve_publishes_wrapper_and_state_in_one_payload(self):
        state_path = self.root / "projects" / PROJECT_ID / "state.json"

        def fake_publish(**kwargs):
            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(kwargs["already_applied"](current))
            self.assertTrue(kwargs["expected"](current))
            payloads = kwargs["payload_builder"](current)
            self.assertEqual(
                set(payloads),
                {
                self.root / "projects" / PROJECT_ID / "reports" / "report-007.md",
                    state_path,
                },
            )
            for path, text in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return json.loads(state_path.read_text(encoding="utf-8"))

        with patch.object(rr.git_store, "publish_cas", side_effect=fake_publish) as publish:
            result = rr.resolve_resolution(
                self.request(),
                expected_report_sha256=self.pending_digest(),
            )
        publish.assert_called_once()
        self.assertEqual(result["result"], "RESOLVED")
        self.assertEqual(result["status"], "REPORT_READY")
        self.assertEqual(result["generation_after"], 10)
        self.assertEqual(result["latest_report"], 7)

        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["status"], "REPORT_READY")
        self.assertEqual(final_state["generation"], 10)
        self.assertEqual(final_state["latest_command"], 7)
        self.assertEqual(final_state["latest_report"], 7)
        self.assertEqual(final_state["last_reviewed_report"], 6)
        self.assertIsNone(final_state["active_run"])
        self.assertIsNone(final_state["worker_pid"])
        self.assertFalse(final_state["human_required"])
        self.assertNotIn("recovery_reason", final_state)
        self.assertNotIn("last_execution_error", final_state)
        self.assertEqual(
            final_state["recovery_note"]["resolution"],
            pc.RECOVERY_RESOLUTION,
        )

        report = (
            self.root / "projects" / PROJECT_ID / "reports" / "report-007.md"
        ).read_text(encoding="utf-8")
        self.assertIn("- outcome: BLOCKED", report)
        self.assertNotIn("- outcome: SUCCESS", report)
        self.assertIn("network guard interruption", report)
        self.assertIn("external_side_effects_unknown: true", report)
        self.assertIn(pending_text(), report)

        # The preserved pending report has its own legacy header.  Conformance
        # must continue to read the wrapper's canonical BLOCKED header rather
        # than the embedded NETWORK_INTERRUPTED evidence header.
        import importlib.util

        conformance_path = (
            Path(__file__).resolve().parents[1]
            / "protocol"
            / "v2"
            / "check_conformance.py"
        )
        spec = importlib.util.spec_from_file_location(
            "bridge_check_conformance", conformance_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        conformance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(conformance)
        self.assertEqual(
            conformance.parse_bullet_metadata(
                self.root / "projects" / PROJECT_ID / "reports" / "report-007.md"
            )["outcome"],
            "BLOCKED",
        )

    def test_repeating_the_same_resolve_is_idempotent_without_a_second_commit(self):
        self.test_resolve_publishes_wrapper_and_state_in_one_payload()
        state_path = self.root / "projects" / PROJECT_ID / "state.json"
        with patch.object(rr.git_store, "publish_cas") as publish:
            result = rr.resolve_resolution(
                self.request(),
                expected_report_sha256=self.pending_digest(),
            )
        publish.assert_not_called()
        self.assertEqual(result["result"], "ALREADY_RESOLVED")
        self.assertEqual(json.loads(state_path.read_text())["generation"], 10)

    def test_no_rerun_boundary_imports_no_executor_or_codex_lifecycle(self):
        self.assertFalse(hasattr(rr, "executor"))
        self.assertFalse(hasattr(rr, "codex_run"))
        self.assertFalse(hasattr(rr, "codex_lifecycle"))
        self.assertEqual(rr.inspect_resolution(self.request()).decision, "ALLOW")

    def test_wrong_reviewed_digest_fails_before_cas(self):
        with patch.object(rr.git_store, "publish_cas") as publish:
            with self.assertRaises(rr.RecoveryResolutionConflict):
                rr.resolve_resolution(
                    self.request(),
                    expected_report_sha256="0" * 64,
                )
        publish.assert_not_called()

    def test_different_existing_canonical_report_fails_closed(self):
        report_path = self.root / "projects" / PROJECT_ID / "reports" / "report-007.md"
        report_path.write_text("# unrelated report\n", encoding="utf-8")
        with self.assertRaises(rr.RecoveryResolutionConflict):
            rr.inspect_resolution(self.request())

    def test_remote_race_aborts_without_publishing_a_report(self):
        state_path = self.root / "projects" / PROJECT_ID / "state.json"
        report_path = self.root / "projects" / PROJECT_ID / "reports" / "report-007.md"

        def racing_publish(**kwargs):
            current = json.loads(state_path.read_text(encoding="utf-8"))
            current["generation"] = 10
            state_path.write_text(bridge_common.json_text(current), encoding="utf-8")
            with self.assertRaises(rr.RecoveryResolutionConflict):
                kwargs["expected"](current)
            raise bridge_common.CASConflict("simulated remote generation race")

        with patch.object(rr.git_store, "publish_cas", side_effect=racing_publish):
            with self.assertRaises(rr.RecoveryResolutionConflict):
                rr.resolve_resolution(
                    self.request(),
                    expected_report_sha256=self.pending_digest(),
                )
        self.assertFalse(report_path.exists())


class RecoveryResolutionPureProtocolTests(unittest.TestCase):
    def setUp(self):
        self.state = recovery_state()
        self.journal = recovery_journal()
        self.identity = {
            "project_id": PROJECT_ID,
            "command_id": COMMAND_ID,
            "run_id": RUN_ID,
            "claim_generation": CLAIM_GENERATION,
            "source": SOURCE,
            "kind": KIND,
            "based_on_report": 6,
            "outcome": "BLOCKED",
            "interrupted": True,
            "no_final_success_marker": True,
            "external_side_effects_unknown": True,
            "pending_report_sha256": "a" * 64,
            "interruption_classification": "NETWORK_INTERRUPTED",
        }
        self.kwargs = {
            "state": self.state,
            "journal": self.journal,
            "report_identity": self.identity,
            "project_id": PROJECT_ID,
            "command_id": COMMAND_ID,
            "run_id": RUN_ID,
            "claim_generation": CLAIM_GENERATION,
            "expected_generation": EXPECTED_GENERATION,
            "source": SOURCE,
            "kind": KIND,
            "based_on_report": 6,
            "pending_report_path": PENDING_RELATIVE,
            "expected_report_sha256": "a" * 64,
            "actual_report_sha256": "a" * 64,
            "canonical_report_exists": False,
            "canonical_report_matches": False,
        }

    def test_matching_recovery_snapshot_is_allowed(self):
        self.assertEqual(pc.validate_recovery_resolution(**self.kwargs), "ALLOW")

    def test_already_resolved_snapshot_is_idempotent(self):
        state = dict(self.state)
        state.update(
            {
                "status": "REPORT_READY",
                "generation": 10,
                "latest_report": 7,
                "recovery_note": {
                    "resolved_command_id": 7,
                    "run_id": RUN_ID,
                    "claim_generation": CLAIM_GENERATION,
                    "resolved_at": "2024-01-01T02:00:00+00:00",
                    "resolution": pc.RECOVERY_RESOLUTION,
                },
            }
        )
        kwargs = dict(
            self.kwargs,
            state=state,
            canonical_report_exists=True,
            canonical_report_matches=True,
        )
        self.assertEqual(pc.validate_recovery_resolution(**kwargs), "ALREADY_RESOLVED")

    def test_identity_and_integrity_guards_fail_closed(self):
        cases = (
            ("status", dict(state=dict(self.state, status="REPORT_READY"))),
            ("generation", dict(state=dict(self.state, generation=60))),
            ("command", dict(state=dict(self.state, latest_command=20))),
            ("active_run", dict(state=dict(self.state, active_run={"run_id": "other"}))),
            ("journal_status", dict(journal=dict(self.journal, journal_status="pending"))),
            ("remote_pending", dict(journal=dict(self.journal, remote_publish_pending=True))),
            ("wrong_run", dict(run_id="run-other")),
            ("wrong_claim", dict(claim_generation=7)),
            ("wrong_digest", dict(actual_report_sha256="b" * 64)),
            ("wrong_report", dict(report_identity=dict(self.identity, outcome="SUCCESS"))),
            (
                "wrong_pending_path",
                dict(pending_report_path="worker/runtime/other/pending-report-007.md"),
            ),
            (
                "wrong_report_path",
                dict(journal=dict(self.journal, report_path="projects/other/reports/report-007.md")),
            ),
            ("different_canonical", dict(canonical_report_exists=True, canonical_report_matches=False)),
        )
        for label, updates in cases:
            with self.subTest(label=label):
                kwargs = dict(self.kwargs)
                kwargs.update(updates)
                with self.assertRaises((pc.ProtocolViolation, pc.ProtocolConflict)):
                    pc.validate_recovery_resolution(**kwargs)

    def test_synthetic_run_identity_is_well_formed(self):
        self.assertEqual(self.kwargs["project_id"], "example-service")
        self.assertEqual(self.kwargs["command_id"], 7)
        self.assertEqual(self.kwargs["run_id"], "run-007-0123456789ab")
        self.assertEqual(self.kwargs["claim_generation"], 8)
        self.assertEqual(self.kwargs["expected_generation"], 9)


if __name__ == "__main__":
    unittest.main()
