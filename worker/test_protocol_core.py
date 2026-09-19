import json
import subprocess
import sys
import unittest
from pathlib import Path

import bridge_manual as bm
import bridge_worker as bw
import protocol_core as pc


ROOT = Path(__file__).resolve().parents[1]


def valid_meta(**updates):
    value = {
        "command_id": 4,
        "source": "scheduled_chatgpt",
        "based_on_report": 3,
        "expected_generation": 7,
        "kind": "EXECUTE",
    }
    value.update(updates)
    return value


class ProtocolCoreConformanceTests(unittest.TestCase):
    def test_runtime_constants_match_machine_readable_spec(self):
        transitions = json.loads(
            (ROOT / "protocol" / "v2" / "transitions.json").read_text(
                encoding="utf-8"
            )
        )
        state_schema = json.loads(
            (ROOT / "protocol" / "v2" / "state.schema.json").read_text(
                encoding="utf-8"
            )
        )
        command_schema = json.loads(
            (ROOT / "protocol" / "v2" / "command.schema.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(pc.PROTOCOL_VERSION, transitions["wire_protocol_version"])
        self.assertEqual(pc.ENGINEERING_PROFILE, transitions["engineering_profile"])
        self.assertEqual(
            set(pc.ALLOWED_STATES),
            set(state_schema["properties"]["status"]["enum"]),
        )
        self.assertEqual(set(pc.ALLOWED_STATES), set(transitions["allowed_states"]))
        self.assertEqual(set(pc.TERMINAL_STATES), set(transitions["terminal_states"]))
        self.assertEqual(
            set(pc.COMMAND_SOURCES),
            set(command_schema["properties"]["source"]["enum"]),
        )
        self.assertEqual(
            set(pc.COMMAND_KINDS),
            set(command_schema["properties"]["kind"]["enum"]),
        )
        self.assertEqual(
            set(pc.SUPPORTED_REASONING_EFFORTS),
            set(
                command_schema["properties"]["executor"]["properties"][
                    "reasoning_effort"
                ]["enum"]
            ),
        )
        self.assertNotIn("OWNER_IN_PROGRESS", pc.ALLOWED_STATES)

    def test_generation_effects_remain_explicit_v2_semantics(self):
        self.assertEqual(pc.publication_generation(6), 7)
        self.assertEqual(pc.claim_generation(6), 7)
        self.assertEqual(pc.report_generation(6), 7)
        self.assertEqual(pc.recovery_generation(6), 7)

        finalizer = next(
            item
            for item in json.loads(
                (ROOT / "protocol" / "v2" / "transitions.json").read_text(
                    encoding="utf-8"
                )
            )["canonical_transitions"]
            if item["event"] == "worker.claim_finalizer"
        )
        self.assertEqual(finalizer["from"], ["FINALIZING"])
        self.assertEqual(finalizer["to"], "CODEX_RUNNING")
        self.assertEqual(finalizer["generation_effect"], "+1")

    def test_manual_fast_lane_keeps_two_logical_generations(self):
        plan = pc.manual_claim_generations(10)
        self.assertEqual(
            (plan.base_generation, plan.publication_generation, plan.claimed_generation),
            (10, 11, 12),
        )

    def test_manual_eligibility_rejects_second_executor(self):
        state = {
            "protocol_version": 2,
            "status": "CODEX_RUNNING",
            "generation": 12,
            "active_run": {
                "run_id": "manual-006-abc",
                "command_id": 6,
                "source": "manual_chatgpt",
                "claimed_generation": 12,
            },
        }
        with self.assertRaises(pc.ProtocolConflict):
            pc.manual_start_decision(state, None)
        with self.assertRaises(bw.CASConflict):
            bm.validate_manual_start(state, None)

    def test_active_run_identity_requires_all_lease_fields(self):
        state = {
            "project_id": "p",
            "status": "CODEX_RUNNING",
            "generation": 12,
            "active_run": {
                "run_id": "run-006",
                "command_id": 6,
                "source": "manual_chatgpt",
                "claimed_generation": 12,
            },
        }
        identity = {
            "run_id": "run-006",
            "command_id": 6,
            "claimed_generation": 12,
        }
        self.assertTrue(
            pc.matches_execution_lease(
                state, source="manual_chatgpt", **identity
            )
        )
        for field, value in (
            ("run_id", "run-other"),
            ("command_id", 7),
            ("claimed_generation", 11),
        ):
            changed = dict(identity)
            changed[field] = value
            with self.subTest(field=field):
                self.assertFalse(
                    pc.matches_execution_lease(
                        state, source="manual_chatgpt", **changed
                    )
                )

    def test_recovery_identity_is_stricter_than_a_run_id_match(self):
        journal = {
            "project_id": "p",
            "command_id": 4,
            "run_id": "run-004",
            "claim_generation": 8,
        }
        state = {
            "project_id": "p",
            "status": "CODEX_RUNNING",
            "generation": 8,
            "active_run": {
                "run_id": "run-004",
                "command_id": 4,
                "claimed_generation": 8,
            },
        }
        self.assertTrue(pc.matches_recovery_running_state(journal, state))
        for field, value in (
            ("project_id", "other"),
            ("command_id", 5),
            ("run_id", "run-other"),
            ("claim_generation", 9),
        ):
            changed = dict(journal)
            changed[field] = value
            with self.subTest(field=field):
                self.assertFalse(pc.matches_recovery_running_state(changed, state))

        recovery_state = {
            "project_id": "p",
            "status": "RECOVERY_REQUIRED",
            "generation": 9,
            "latest_command": 4,
            "active_run": None,
            "worker_pid": None,
            "recovery_reason": "run-004; automatic rerun prohibited",
        }
        self.assertTrue(pc.matches_recovery_state(journal, recovery_state))

    def test_stale_generation_and_report_are_protocol_conflicts(self):
        with self.assertRaises(pc.ProtocolConflict):
            pc.validate_pending_command(
                state={"generation": 8, "latest_report": 3},
                command_id=4,
                meta=valid_meta(expected_generation=7),
            )
        with self.assertRaises(pc.ProtocolConflict):
            pc.validate_pending_command(
                state={"generation": 7, "latest_report": 4},
                command_id=4,
                meta=valid_meta(based_on_report=3),
            )

    def test_metadata_is_unique_and_schema_strict(self):
        command = (
            '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
            '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n'
        )
        self.assertEqual(pc.parse_command_metadata(command)["command_id"], 4)
        duplicate = command + command
        with self.assertRaises(pc.ProtocolViolation):
            pc.parse_command_metadata(duplicate)
        with self.assertRaises(bw.WorkerError):
            bw.command_metadata(duplicate)
        with self.assertRaises(pc.ProtocolViolation):
            pc.validate_command_metadata({"command_id": 4})
        with self.assertRaises(pc.ProtocolViolation):
            pc.validate_command_metadata(valid_meta(unknown_field=True))

    def test_withdrawal_metadata_is_distinct_from_supersede_metadata(self):
        meta = valid_meta(withdraws_command_id=3)
        pc.validate_command_metadata(meta)
        with self.assertRaises(pc.ProtocolViolation):
            pc.validate_command_metadata(
                valid_meta(supersedes_command_id=3, withdraws_command_id=2)
            )

    def test_atomic_owner_withdrawal_and_manual_start_decision(self):
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "generation": 10,
            "latest_command": 1,
            "latest_report": 0,
            "active_run": None,
        }
        meta = valid_meta(
            command_id=1,
            based_on_report=0,
            expected_generation=10,
            source="manual_chatgpt",
            kind="EXECUTE",
        )
        replacement = valid_meta(
            command_id=2,
            source="manual_chatgpt",
            based_on_report=0,
            expected_generation=11,
            kind="EXECUTE",
            manual_request_id="request-002",
            withdraws_command_id=1,
        )
        decision = pc.withdraw_and_manual_start_decision(
            state,
            target_command_id=1,
            target_meta=meta,
            target_report_exists=False,
            reason="owner_withdrawn / requirements_changed",
            replacement_meta=replacement,
            replacement_filename_command_id=2,
            existing_command_ids=[1],
        )
        self.assertEqual(decision.target_command_id, 1)
        self.assertEqual(decision.replacement_command_id, 2)
        self.assertEqual(decision.publication_generation, 11)
        self.assertEqual(decision.claimed_generation, 12)

        rejects = (
            ("active run", {**state, "active_run": {"run_id": "r"}}, {}),
            ("state generation", {**state, "generation": 11}, {}),
            ("latest command", {**state, "latest_command": 2}, {}),
            ("target report", state, {"target_report_exists": True}),
            (
                "wrong source",
                state,
                {"target_meta": {**meta, "source": "scheduled_chatgpt"}},
            ),
            (
                "wrong kind",
                state,
                {"target_meta": {**meta, "kind": "FINALIZE"}},
            ),
            (
                "invalid replacement schema",
                state,
                {"replacement_meta": {**replacement, "unknown": True}},
            ),
            (
                "replacement self reference",
                state,
                {"replacement_meta": {**replacement, "withdraws_command_id": 2}},
            ),
            (
                "replacement future reference",
                state,
                {"replacement_meta": {**replacement, "withdraws_command_id": 39}},
            ),
            (
                "duplicate withdrawal record",
                {**state, "withdrawn_commands": [{"command_id": 1}]},
                {},
            ),
        )
        for label, changed, updates in rejects:
            with self.subTest(changed=changed):
                kwargs = {
                    "target_meta": meta,
                    "target_report_exists": False,
                    "replacement_meta": replacement,
                    "replacement_filename_command_id": 2,
                    "existing_command_ids": [1],
                    "existing_withdrawal_command_ids": [1],
                    **updates,
                }
                with self.assertRaises((pc.ProtocolConflict, pc.ProtocolViolation)):
                    pc.withdraw_and_manual_start_decision(
                        changed,
                        target_command_id=1,
                        reason="owner_withdrawn / requirements_changed",
                        **kwargs,
                    )

        with self.assertRaises(pc.ProtocolConflict):
            pc.withdraw_and_manual_start_decision(
                state,
                target_command_id=1,
                target_meta=meta,
                target_report_exists=False,
                reason="owner_withdrawn / requirements_changed",
                replacement_meta=replacement,
                replacement_filename_command_id=2,
                existing_command_ids=[1, 2],
            )

    def test_finalization_marker_remains_strict(self):
        misleading = "Email failed; final_delivery SUCCESS and completion_email SENT."
        self.assertFalse(pc.maybe_final_report_ready("FINALIZING", "SUCCESS", misleading))
        valid = (
            'Done.\nBRIDGE_FINAL_JSON: '
            '{"final_delivery":"SUCCESS","completion_email":"SENT"}'
        )
        self.assertTrue(pc.maybe_final_report_ready("FINALIZING", "SUCCESS", valid))
        self.assertEqual(
            pc.report_status_after_execution("FINALIZE", "SUCCESS", valid),
            "FINAL_REPORT_READY",
        )

    def test_legacy_worker_and_manual_wrappers_match_core_for_valid_decisions(self):
        command = (
            '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
            '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n'
        )
        self.assertEqual(bw.command_metadata(command), pc.parse_command_metadata(command))
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "latest_command": 4,
            "active_run": None,
        }
        meta = {"source": "scheduled_chatgpt", "kind": "EXECUTE"}
        self.assertEqual(
            bm.validate_manual_start(state, meta),
            pc.manual_start_decision(state, meta).supersedes_command_id,
        )

    def test_repository_conformance_command_still_accepts_legacy_and_current_states(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "protocol" / "v2" / "check_conformance.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Protocol v2 conformance PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
