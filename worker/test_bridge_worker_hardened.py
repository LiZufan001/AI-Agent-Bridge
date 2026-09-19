import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker_hardened as h
from codex_lifecycle import CodexRunResult


def run_result(
    *,
    code=0,
    message="",
    marker=None,
    marker_result=None,
    runtime_error=None,
):
    return CodexRunResult(
        exit_code=code,
        final_message=message,
        stdout_tail="out",
        stderr_tail="err",
        marker=marker,
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at=None,
        process_exited_at="2001-01-15T00:00:01+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result=(
            marker_result
            or ("VALID" if marker is not None else "PROCESS_EXITED_WITHOUT_MARKER")
        ),
        runtime_error=runtime_error,
    )


class HardenedWorkerTests(unittest.TestCase):
    def test_prompt_says_worker_already_owns_lease(self):
        prompt = h.hardened_build_prompt("p", "mission", "command")
        self.assertIn("ALREADY atomically claimed", prompt)
        self.assertIn("DO NOT invoke `$bridge-manual`", prompt)
        self.assertIn("BRIDGE_EXECUTION_JSON", prompt)

    def test_parse_requires_exactly_one_valid_marker(self):
        self.assertEqual(
            h.parse_execution_result('x\nBRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}\n'),
            {"status": "SUCCESS"},
        )
        self.assertIsNone(h.parse_execution_result("ordinary prose only"))
        self.assertIsNone(
            h.parse_execution_result(
                'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}\n'
                'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}'
            )
        )
        self.assertIsNone(
            h.parse_execution_result('BRIDGE_EXECUTION_JSON: {"status":"MAYBE"}')
        )

    def test_zero_exit_without_marker_records_contract_failure(self):
        original = run_result(code=0, message="task did not happen")
        with patch.object(
            h,
            "_ORIGINAL_CODEX_RUN",
            return_value=original,
        ):
            result = h.hardened_codex_run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.final_message, "task did not happen")
        self.assertIn("PROCESS_EXITED_WITHOUT_MARKER", result.contract_error)

    def test_capture_failure_is_distinct_from_process_exit_without_marker(self):
        original = run_result(
            code=125,
            marker_result="MARKER_CAPTURE_FAILED",
            runtime_error="final-message file could not be read",
        )
        with patch.object(h, "_ORIGINAL_CODEX_RUN", return_value=original):
            result = h.hardened_codex_run()
        self.assertIn("MARKER_CAPTURE_FAILED", result.contract_error)
        self.assertNotIn("PROCESS_EXITED_WITHOUT_MARKER", result.contract_error)

    def test_invalid_marker_is_distinct_from_missing_marker(self):
        original = run_result(
            code=0,
            message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}\n'
            'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
            marker_result="INVALID_FINAL_MARKER",
        )
        with patch.object(h, "_ORIGINAL_CODEX_RUN", return_value=original):
            result = h.hardened_codex_run()
        self.assertIn("INVALID_FINAL_MARKER", result.contract_error)

    def test_blocked_marker_is_preserved_as_business_result(self):
        final = (
            "Could not complete.\n"
            'BRIDGE_EXECUTION_JSON: {"status":"BLOCKED","reason":"owner input needed"}'
        )
        original = run_result(
            code=0,
            message=final,
            marker={"status": "BLOCKED", "reason": "owner input needed"},
        )
        with patch.object(
            h,
            "_ORIGINAL_CODEX_RUN",
            return_value=original,
        ):
            result = h.hardened_codex_run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.marker["status"], "BLOCKED")
        self.assertIn("BLOCKED", result.contract_error)

    def test_success_marker_preserves_zero_exit(self):
        final = 'Done.\nBRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}'
        original = run_result(code=0, message=final, marker={"status": "SUCCESS"})
        with patch.object(
            h,
            "_ORIGINAL_CODEX_RUN",
            return_value=original,
        ):
            result = h.hardened_codex_run()
        self.assertIs(result, original)
        self.assertIsNone(result.contract_error)




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
