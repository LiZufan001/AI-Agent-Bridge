import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import bridge_worker as bw
import bridge_worker_hardened as hardened
from codex_lifecycle import run_codex_process


HELPER = r'''
import os
import subprocess
import sys
import time
from pathlib import Path

mode = sys.argv[1]
output = Path(sys.argv[2])
pid_file = Path(sys.argv[3])

def write_marker(status="SUCCESS"):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "helper finished\nBRIDGE_EXECUTION_JSON: "
        + '{"status":"' + status + '"}\n',
        encoding="utf-8",
    )

if mode == "normal":
    write_marker()
elif mode == "nonzero_after_marker":
    write_marker()
    raise SystemExit(9)
elif mode == "hang_after_marker":
    write_marker()
    time.sleep(60)
elif mode == "orphan_holds_streams":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    pid_file.write_text(str(child.pid), encoding="ascii")
    write_marker()
elif mode == "exit_zero_no_marker":
    pass
elif mode == "exit_nonzero_no_marker":
    raise SystemExit(7)
elif mode == "failed_marker":
    write_marker("FAILED")
elif mode == "blocked_marker":
    write_marker("BLOCKED")
elif mode == "partial_marker":
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('BRIDGE_EXECUTION_JSON: {"status":', encoding="utf-8")
elif mode == "partial_then_valid":
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('BRIDGE_EXECUTION_JSON: {"status":', encoding="utf-8")
    time.sleep(0.04)
    write_marker()
elif mode == "hang_no_marker":
    time.sleep(60)
elif mode == "spam_log":
    os.write(1, b"x" * 8192)
    write_marker()
elif mode == "large_stderr_marker":
    os.write(2, b"e" * (11 * 1024 * 1024))
    write_marker()
elif mode == "large_logs_orphan":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    pid_file.write_text(str(child.pid), encoding="ascii")
    os.write(1, b"o" * (2 * 1024 * 1024))
    os.write(2, b"e" * (2 * 1024 * 1024))
    write_marker()
elif mode == "duplicate_marker":
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}\n'
        'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}\n',
        encoding="utf-8",
    )
elif mode == "oversized_final":
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"x" * 4096)
else:
    raise SystemExit(9)
'''


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=False,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return str(pid).encode("ascii") in (result.stdout or b"")
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


class CodexLifecycleRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.helper = self.root / "fake_codex.py"
        self.helper.write_text(HELPER, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_mode(
        self,
        mode: str,
        run_name: str,
        *,
        grace: float = 0.3,
        execution: float = 3,
        max_log_bytes: int = 1024 * 1024,
        max_final_message_bytes: int = 1024 * 1024,
    ):
        run_dir = self.root / run_name
        output = run_dir / "final-message.txt"
        return run_codex_process(
            args=[sys.executable, str(self.helper), mode, str(output), str(run_dir / "child.pid")],
            workdir=self.root,
            output_file=output,
            stdout_log_path=run_dir / "stdout.log",
            stderr_log_path=run_dir / "stderr.log",
            prompt="test prompt",
            execution_timeout_seconds=execution,
            final_grace_timeout_seconds=grace,
            cleanup_timeout_seconds=2,
            marker_stable_seconds=0.08,
            poll_interval_seconds=0.02,
            max_log_bytes=max_log_bytes,
            max_final_message_bytes=max_final_message_bytes,
            marker_parser=hardened.parse_execution_result,
        )

    def test_normal_valid_success_marker_and_clean_exit(self):
        result = self.run_mode("normal", "run-normal")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        self.assertFalse(result.forced_cleanup)
        self.assertEqual(bw.execution_outcome(result), "SUCCESS")

    def test_valid_marker_then_root_hangs_is_bounded_and_preserves_success(self):
        started = time.monotonic()
        result = self.run_mode("hang_after_marker", "run-hang")
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(result.forced_cleanup_after_final)
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        self.assertEqual(bw.execution_outcome(result), "SUCCESS")
        self.assertNotEqual(result.exit_code, 0)

    def test_valid_marker_outweighs_nonzero_process_exit(self):
        result = self.run_mode("nonzero_after_marker", "run-marker-nonzero")
        self.assertEqual(result.exit_code, 9)
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        self.assertEqual(result.marker_result, "VALID")
        self.assertEqual(bw.execution_outcome(result), "SUCCESS")

    def test_orphan_holding_inherited_streams_is_cleaned_without_eof_wait(self):
        result = self.run_mode("orphan_holds_streams", "run-orphan")
        child_pid = int((self.root / "run-orphan" / "child.pid").read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and pid_exists(child_pid):
            time.sleep(0.05)
        self.assertFalse(pid_exists(child_pid))
        self.assertTrue(result.forced_cleanup_after_final)
        self.assertEqual(bw.execution_outcome(result), "SUCCESS")

    def test_exit_zero_without_marker_is_failed(self):
        result = self.run_mode("exit_zero_no_marker", "run-no-marker-zero")
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.marker)
        self.assertEqual(bw.execution_outcome(result), "FAILED")

    def test_nonzero_without_marker_is_failed(self):
        result = self.run_mode("exit_nonzero_no_marker", "run-no-marker-seven")
        self.assertEqual(result.exit_code, 7)
        self.assertIsNone(result.marker)
        self.assertEqual(result.marker_result, "PROCESS_EXITED_WITHOUT_MARKER")
        self.assertEqual(bw.execution_outcome(result), "FAILED")

    def test_failed_marker_is_not_promoted_to_success(self):
        result = self.run_mode("failed_marker", "run-failed-marker")
        self.assertEqual(result.marker, {"status": "FAILED"})
        self.assertEqual(bw.execution_outcome(result), "FAILED")

    def test_blocked_marker_is_respected(self):
        result = self.run_mode("blocked_marker", "run-blocked-marker")
        self.assertEqual(result.marker, {"status": "BLOCKED"})
        self.assertEqual(bw.execution_outcome(result), "BLOCKED")

    def test_partial_marker_is_never_accepted(self):
        result = self.run_mode("partial_marker", "run-partial")
        self.assertIsNone(result.marker)
        self.assertEqual(result.marker_result, "INVALID_FINAL_MARKER")
        self.assertEqual(bw.execution_outcome(result), "FAILED")

    def test_duplicate_marker_is_rejected(self):
        result = self.run_mode("duplicate_marker", "run-duplicate")
        self.assertIsNone(result.marker)
        self.assertEqual(result.marker_result, "INVALID_FINAL_MARKER")
        self.assertEqual(bw.execution_outcome(result), "FAILED")

    def test_oversized_final_file_is_capture_failure(self):
        result = self.run_mode(
            "oversized_final",
            "run-oversized-final",
            max_final_message_bytes=1024,
        )
        self.assertIsNone(result.marker)
        self.assertEqual(result.marker_result, "MARKER_CAPTURE_FAILED")
        self.assertIn("exceeded", result.runtime_error)

    def test_partial_write_must_stabilize_before_valid_marker_is_accepted(self):
        result = self.run_mode("partial_then_valid", "run-partial-valid")
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        self.assertIsNotNone(result.final_marker_detected_at)

    def test_execution_timeout_without_marker_is_bounded(self):
        started = time.monotonic()
        result = self.run_mode(
            "hang_no_marker", "run-timeout", execution=0.3
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.forced_cleanup)
        self.assertEqual(result.termination_reason, "execution_timeout")
        self.assertEqual(result.marker_result, "PROCESS_TERMINATED_WITHOUT_MARKER")
        self.assertIsNone(result.marker)
        self.assertEqual(bw.execution_outcome(result), "FAILED")

    def test_log_threshold_is_diagnostic_and_does_not_terminate_execution(self):
        result = self.run_mode(
            "spam_log", "run-log-limit", max_log_bytes=1024
        )
        self.assertFalse(result.forced_cleanup)
        self.assertTrue(result.log_threshold_exceeded)
        self.assertIn("execution continued", result.log_threshold_diagnostic)
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        self.assertEqual(bw.execution_outcome(result), "SUCCESS")

    def test_stderr_over_ten_mib_keeps_marker_and_bounded_report_tail(self):
        result = self.run_mode(
            "large_stderr_marker",
            "run-large-stderr",
            max_log_bytes=10 * 1024 * 1024,
        )
        self.assertGreater(result.stderr_log_path.stat().st_size, 10 * 1024 * 1024)
        self.assertLessEqual(len(result.stderr_tail.encode("utf-8")), 16_384)
        self.assertTrue(result.log_threshold_exceeded)
        self.assertFalse(result.forced_cleanup)
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        report = bw.report_markdown(
            project_id="synthetic",
            command_id=1,
            outcome=bw.execution_outcome(result),
            exit_code=result.exit_code,
            workdir=self.root,
            head_before=None,
            head_after=None,
            final_message=result.final_message,
            stderr=result.stderr_tail,
            meta={},
            run_id="run-large-stderr",
            claim_generation=1,
            executor_profile={},
            run_result=result,
        )
        self.assertLess(len(report), 20_000)

    def test_large_stdout_stderr_with_descendant_preserves_final_result(self):
        result = self.run_mode(
            "large_logs_orphan",
            "run-large-orphan",
            max_log_bytes=1024 * 1024,
        )
        child_pid = int((self.root / "run-large-orphan" / "child.pid").read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and pid_exists(child_pid):
            time.sleep(0.05)
        self.assertFalse(pid_exists(child_pid))
        self.assertTrue(result.log_threshold_exceeded)
        self.assertTrue(result.forced_cleanup_after_final)
        self.assertEqual(result.marker, {"status": "SUCCESS"})
        self.assertEqual(bw.execution_outcome(result), "SUCCESS")

    def test_consecutive_runs_never_reuse_previous_final_message_or_logs(self):
        first = self.run_mode("normal", "run-first")
        second = self.run_mode("exit_zero_no_marker", "run-second")
        self.assertEqual(first.marker, {"status": "SUCCESS"})
        self.assertIsNone(second.marker)
        self.assertNotEqual(first.stdout_log_path, second.stdout_log_path)
        self.assertFalse((self.root / "run-second" / "final-message.txt").exists())

    def test_preexisting_stale_final_file_is_rejected_before_launch(self):
        stale = self.root / "run-stale" / "final-message.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text(
            'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}', encoding="utf-8"
        )
        with self.assertRaises(FileExistsError):
            self.run_mode("normal", "run-stale")


if __name__ == "__main__":
    unittest.main()
