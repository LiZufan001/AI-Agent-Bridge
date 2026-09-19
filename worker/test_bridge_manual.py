import unittest

import bridge_manual as bm
import bridge_worker as bw


class BridgeManualTests(unittest.TestCase):
    def test_report_ready_can_start(self):
        state = {
            "protocol_version": 2,
            "status": "REPORT_READY",
            "active_run": None,
        }
        self.assertIsNone(bm.validate_manual_start(state, None))

    def test_unclaimed_scheduled_command_can_be_superseded(self):
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "latest_command": 8,
            "active_run": None,
        }
        meta = {"source": "scheduled_chatgpt", "kind": "EXECUTE"}
        self.assertEqual(bm.validate_manual_start(state, meta), 8)

    def test_manual_command_does_not_supersede_non_scheduled_command(self):
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "latest_command": 8,
            "active_run": None,
        }
        meta = {"source": "manual_chatgpt", "kind": "EXECUTE"}
        with self.assertRaises(bw.CASConflict):
            bm.validate_manual_start(state, meta)

    def test_running_project_rejects_manual_start(self):
        state = {
            "protocol_version": 2,
            "status": "CODEX_RUNNING",
            "active_run": {"run_id": "run-1"},
        }
        with self.assertRaises(bw.CASConflict):
            bm.validate_manual_start(state, None)

    def test_combined_manual_claim_consumes_two_generations(self):
        state = {
            "protocol_version": 2,
            "status": "REPORT_READY",
            "generation": 10,
            "latest_command": 5,
            "latest_report": 5,
            "last_reviewed_report": 4,
            "active_run": None,
        }
        claimed = bm.build_manual_claim_state(
            current=state,
            command_id=6,
            run_id="manual-006-abc",
            request_id="abc",
            kind="EXECUTE",
            supersedes_command_id=None,
            lease_hours=6,
        )
        self.assertEqual(claimed["generation"], 12)
        self.assertEqual(claimed["last_reviewed_report"], 5)
        self.assertEqual(
            claimed["active_run"]["publication_generation"], 11
        )
        self.assertEqual(claimed["active_run"]["claimed_generation"], 12)

    def test_manual_command_metadata_preserves_base_snapshot(self):
        text = bm.render_manual_command(
            command_id=9,
            body="Do the bounded task.",
            based_on_report=8,
            expected_generation=21,
            kind="EXECUTE",
            request_id="req1",
            supersedes_command_id=7,
        )
        meta = bw.command_metadata(text)
        self.assertEqual(meta["command_id"], 9)
        self.assertEqual(meta["source"], "manual_chatgpt")
        self.assertEqual(meta["based_on_report"], 8)
        self.assertEqual(meta["expected_generation"], 21)
        self.assertEqual(meta["supersedes_command_id"], 7)
        self.assertNotIn("executor", meta)

    def test_manual_command_can_record_a_canonical_owner_withdrawal(self):
        text = bm.render_manual_command(
            command_id=10,
            body="Continue the owner-approved task.",
            based_on_report=9,
            expected_generation=22,
            kind="EXECUTE",
            request_id="req-withdrawal",
            supersedes_command_id=None,
            withdraws_command_id=8,
        )
        meta = bw.command_metadata(text)
        self.assertEqual(meta["withdraws_command_id"], 8)
        self.assertNotIn("supersedes_command_id", meta)

    def test_executor_metadata_does_not_control_manual_session(self):
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "active_run": None,
            "latest_command": 8,
        }
        existing_meta = {
            "source": "scheduled_chatgpt",
            "kind": "EXECUTE",
            "executor": {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            },
        }
        self.assertEqual(bm.validate_manual_start(state, existing_meta), 8)

    def test_finalize_requires_strict_marker(self):
        misleading = (
            "Email failed, although I wanted final_delivery SUCCESS and "
            "completion_email SENT."
        )
        self.assertEqual(
            bm.final_status("FINALIZE", "SUCCESS", misleading),
            "REPORT_READY",
        )
        good = (
            'Done.\nBRIDGE_FINAL_JSON: '
            '{"final_delivery":"SUCCESS","completion_email":"SENT"}'
        )
        self.assertEqual(
            bm.final_status("FINALIZE", "SUCCESS", good),
            "FINAL_REPORT_READY",
        )

    def test_active_run_requires_exact_lease_identity(self):
        state = {
            "status": "CODEX_RUNNING",
            "generation": 12,
            "active_run": {
                "run_id": "manual-006-abc",
                "command_id": 6,
                "claimed_generation": 12,
                "source": "manual_chatgpt",
            },
        }
        self.assertTrue(
            bm.active_run_matches(
                state,
                run_id="manual-006-abc",
                command_id=6,
                claimed_generation=12,
            )
        )
        self.assertFalse(
            bm.active_run_matches(
                state,
                run_id="manual-006-other",
                command_id=6,
                claimed_generation=12,
            )
        )


if __name__ == "__main__":
    unittest.main()
