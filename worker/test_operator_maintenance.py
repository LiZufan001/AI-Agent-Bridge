import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import operator_maintenance as om


@unittest.skipUnless(os.name == "nt", "exact Job ownership is Windows-only")
class OperatorMaintenanceWindowsTests(unittest.TestCase):
    def limits(self, **changes):
        values = {
            "timeout_seconds": 3.0,
            "poll_seconds": 0.01,
            "cleanup_seconds": 3.0,
            "stdout_bytes": 4096,
            "stderr_bytes": 4096,
            "checkpoint_count": 8,
        }
        values.update(changes)
        return om.MaintenanceLimits(**values)

    def run_python(self, source, **kwargs):
        return om.run_maintenance_command(
            [sys.executable, "-c", source],
            command_class="test-maintenance",
            limits=self.limits(**kwargs),
        )

    def assert_process_inactive_within(self, pid, creation_ticks, *, seconds=3.0):
        deadline = time.monotonic() + seconds
        while (
            om._windows_process_is_active(pid, creation_ticks)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertFalse(
            om._windows_process_is_active(pid, creation_ticks),
            f"exact owned process {pid}/{creation_ticks} survived bounded teardown",
        )

    def assert_zero(self, result):
        self.assertEqual(result["operator_owned_active_children"], 0)
        self.assertEqual(result["operator_owned_orphans"], 0)
        for checkpoint in result["checkpoints"]:
            for process in checkpoint["active_processes"]:
                self.assertFalse(
                    om._windows_process_is_active(
                        process["pid"], process["creation_ticks"]
                    )
                )

    def test_success_and_failure_both_reap_exact_tree(self):
        success = self.run_python("print('ok')")
        self.assertTrue(success["success"])
        self.assertEqual(success["result"], "SUCCESS")
        self.assert_zero(success)
        failed = self.run_python("raise SystemExit(7)")
        self.assertFalse(failed["success"])
        self.assertEqual(failed["result"], "CHILD_FAILED")
        self.assertEqual(failed["exit_code"], 7)
        self.assert_zero(failed)

    def test_hang_times_out_and_descendant_is_reaped(self):
        source = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "time.sleep(60)"
        )
        result = self.run_python(source, timeout_seconds=0.3)
        self.assertEqual(result["result"], "TIMEOUT")
        self.assertEqual(result["first_failure"]["reason"], "TIMEOUT")
        self.assert_zero(result)

    def test_completion_waits_for_exact_process_objects_to_signal(self):
        calls = 0

        def delayed_signal(_pid, _creation_ticks):
            nonlocal calls
            calls += 1
            return calls == 1

        with patch.object(
            om, "_windows_process_is_active", side_effect=delayed_signal
        ):
            result = self.run_python("print('ok')")

        self.assertTrue(result["success"])
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(result["exact_active_after_close"], [])
        self.assert_zero(result)

    def test_cancellation_propagates_and_reaps(self):
        result = om.run_maintenance_command(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            command_class="test-cancel",
            limits=self.limits(),
            cancel_check=lambda: True,
        )
        self.assertEqual(result["result"], "CANCELLED")
        self.assert_zero(result)

    def test_injected_memory_limit_aborts_without_allocating_gigabytes(self):
        hard_limit = 64 * 1024 * 1024
        result = om.run_maintenance_command(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            command_class="test-memory",
            limits=self.limits(
                hard_private_bytes=hard_limit,
                soft_private_bytes=hard_limit // 2,
            ),
            metric_sampler=lambda _pids: (hard_limit, 1024),
        )
        self.assertEqual(result["result"], "PRIVATE_BYTES_LIMIT")
        self.assertEqual(result["first_failure"]["private_bytes"], hard_limit)
        self.assert_zero(result)

    def test_windows_job_has_kernel_committed_memory_ceiling(self):
        hard_limit = 96 * 1024 * 1024
        job = om._WindowsJob(hard_limit)
        try:
            info = om._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ok = om._kernel32.QueryInformationJobObject(
                job.handle,
                om._JobObjectExtendedLimitInformation,
                om.ctypes.byref(info),
                om.ctypes.sizeof(info),
                None,
            )
            self.assertTrue(ok)
            self.assertTrue(
                info.BasicLimitInformation.LimitFlags
                & om._JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            self.assertEqual(info.JobMemoryLimit, hard_limit)
        finally:
            job.close()

    def test_outer_exception_still_reaps_owned_process(self):
        observed = []

        def explode(pids):
            observed.extend(
                (pid, om._windows_process_identity(pid)[1]) for pid in pids
            )
            raise RuntimeError("injected wrapper failure")

        with self.assertRaisesRegex(RuntimeError, "injected wrapper failure"):
            om.run_maintenance_command(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                command_class="test-exception",
                limits=self.limits(),
                metric_sampler=explode,
            )
        self.assertTrue(observed)
        for pid, creation_ticks in observed:
            self.assert_process_inactive_within(pid, creation_ticks)

    def test_stdout_and_stderr_floods_retain_only_bounded_tails(self):
        result = self.run_python(
            "import os;os.write(1,b'A'*200000);os.write(2,b'B'*210000)"
        )
        self.assertTrue(result["success"])
        for name, total, byte in (("stdout", 200000, "A"), ("stderr", 210000, "B")):
            evidence = result[name]
            self.assertEqual(evidence["total_bytes"], total)
            self.assertEqual(evidence["retained_bytes"], 4096)
            self.assertTrue(evidence["truncated"])
            self.assertEqual(evidence["tail"], byte * 4096)
        self.assert_zero(result)

    def test_checkpoint_retention_is_bounded(self):
        result = self.run_python("import time;time.sleep(.25)", checkpoint_count=3)
        self.assertTrue(result["success"])
        self.assertLessEqual(len(result["checkpoints"]), 3)
        self.assertEqual(result["checkpoint_retention_limit"], 3)

    def test_stdin_and_environment_overrides_are_bounded_and_delivered(self):
        result = om.run_maintenance_command(
            [
                sys.executable,
                "-c",
                "import os,sys;print(os.environ['BRIDGE_TEST']);print(sys.stdin.read())",
            ],
            command_class="test-stdin",
            limits=self.limits(),
            stdin_data=b"bounded-input",
            env_overrides={"BRIDGE_TEST": "bounded-env"},
        )
        self.assertTrue(result["success"])
        self.assertIn("bounded-env", result["stdout"]["tail"])
        self.assertIn("bounded-input", result["stdout"]["tail"])
        with self.assertRaisesRegex(ValueError, "stdin"):
            om.run_maintenance_command(
                [sys.executable, "-c", "pass"],
                command_class="test-stdin-limit",
                stdin_data=b"x" * (64 * 1024 + 1),
            )

    def test_success_is_impossible_when_boundary_reports_a_child(self):
        real = om._OwnedBoundary()
        original = real.process_ids
        calls = 0

        def ids(root_pid):
            nonlocal calls
            calls += 1
            values = original(root_pid)
            if not values and calls > 1:
                return (999999,)
            return values

        real.process_ids = ids
        with patch.object(om, "_OwnedBoundary", return_value=real):
            result = self.run_python("pass")
        self.assertFalse(result["success"])
        self.assertEqual(result["result"], "OPERATOR_CHILD_LEAK")

    def test_elevated_wrapper_contract_preserves_result_and_parent_cancellation(self):
        identity = om._windows_process_identity(os.getpid())
        self.assertIsNotNone(identity)

        def run_case(*, execution_id, argv, limits=None, parent_ticks=None):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result_path = root / "result.json"
                spec_path = root / "spec.json"
                spec = {
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "argv": argv,
                    "command_class": "test-elevated",
                    "cwd": str(root),
                    "limits": om._limits_payload(limits or self.limits()),
                    "parent_pid": os.getpid(),
                    "parent_creation_ticks": (
                        identity[1] if parent_ticks is None else parent_ticks
                    ),
                    "deadline_utc": om._deadline_utc(5),
                    "cancel_path": str(root / "cancel"),
                    "result_path": str(result_path),
                }
                om._write_json(spec_path, spec)
                exit_code = om._elevated_wrapper(spec_path)
                result = json.loads(result_path.read_text(encoding="utf-8"))
                return exit_code, result

        exit_code, result = run_case(
            execution_id="test-elevated-wrapper",
            argv=[sys.executable, "-c", "print('elevated-wrapper')"],
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["success"])
        self.assertEqual(result["operator_owned_orphans"], 0)

        exit_code, failed = run_case(
            execution_id="test-elevated-failure",
            argv=[sys.executable, "-c", "raise SystemExit(9)"],
        )
        self.assertEqual(exit_code, 125)
        self.assertEqual(failed["result"], "CHILD_FAILED", failed)
        self.assertEqual(failed["exit_code"], 9)
        self.assertEqual(failed["operator_owned_orphans"], 0)

        exit_code, timed_out = run_case(
            execution_id="test-elevated-timeout",
            argv=[sys.executable, "-c", "import time;time.sleep(60)"],
            limits=self.limits(timeout_seconds=0.2),
        )
        self.assertEqual(exit_code, 125)
        self.assertEqual(timed_out["result"], "TIMEOUT", timed_out)
        self.assertEqual(timed_out["operator_owned_orphans"], 0)

        exit_code, cancelled = run_case(
            execution_id="test-elevated-cancel",
            argv=[sys.executable, "-c", "import time;time.sleep(60)"],
            parent_ticks=identity[1] + 1,
        )
        self.assertEqual(exit_code, 125)
        self.assertEqual(cancelled["result"], "CANCELLED", cancelled)
        self.assertEqual(cancelled["operator_owned_orphans"], 0)


@unittest.skipUnless(os.name == "nt", "elevated result propagation is Windows-only")
class ElevatedResultPropagationTests(unittest.TestCase):
    def broker(self, result, *, reason=None, success=False):
        return {
            "schema_version": 1,
            "execution_id": "broker-test",
            "success": success,
            "result": result,
            "first_failure": (
                {"reason": reason if reason is not None else result}
                if result is not None
                else None
            ),
            "operator_owned_active_children": 0,
            "operator_owned_orphans": 0,
            "exact_active_after_close": [],
        }

    def missing_wrapper(self, broker, *, cleanup_failure=None):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)

            owner_alive = cleanup_failure is not None
            with patch.object(
                om, "run_maintenance_command", return_value=broker
            ), patch.object(
                om, "_parent_identity_alive", return_value=owner_alive
            ), patch.object(
                om, "_terminate_exact_process", return_value=None
            ):
                if owner_alive:
                    def broker_with_owner(argv, **_kwargs):
                        spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
                        om._write_json(
                            Path(spec["owner_path"]),
                            {
                                "elevated_root_pid": 999999,
                                "elevated_root_creation_ticks": 1,
                            },
                        )
                        return broker

                    with patch.object(
                        om, "run_maintenance_command", side_effect=broker_with_owner
                    ):
                        return om.run_elevated_maintenance_command(
                            [sys.executable, "-c", "pass"],
                            command_class="test-elevated-propagation",
                            evidence_directory=evidence,
                            limits=om.MaintenanceLimits(
                                timeout_seconds=1.0,
                                poll_seconds=0.01,
                                cleanup_seconds=0.01,
                            ),
                        )
                return om.run_elevated_maintenance_command(
                    [sys.executable, "-c", "pass"],
                    command_class="test-elevated-propagation",
                    evidence_directory=evidence,
                    limits=om.MaintenanceLimits(
                        timeout_seconds=1.0,
                        poll_seconds=0.01,
                        cleanup_seconds=0.01,
                    ),
                )

    def test_missing_wrapper_timeout_uses_broker_terminal_fallback(self):
        result = self.missing_wrapper(self.broker("TIMEOUT"))
        self.assertFalse(result["success"])
        self.assertEqual(result["result"], "TIMEOUT")
        self.assertFalse(result["wrapper_result_present"])
        self.assertEqual(result["result_source"], "broker-terminal-fallback")
        self.assertEqual(result["broker_terminal_cause"], "TIMEOUT")
        self.assertEqual(result["operator_owned_orphans"], 0)

    def test_missing_wrapper_allows_only_correlated_terminal_causes(self):
        for cause in (
            "CANCELLED",
            "PRIVATE_BYTES_LIMIT",
            "WORKING_SET_LIMIT",
        ):
            with self.subTest(cause=cause):
                result = self.missing_wrapper(self.broker(cause))
                self.assertEqual(result["result"], cause)
                self.assertFalse(result["success"])
                self.assertEqual(result["operator_owned_orphans"], 0)

    def test_missing_wrapper_unknown_or_generic_broker_failure_fails_closed(self):
        for result_code in ("CHILD_FAILED", "ELEVATION_BROKER_FAILED", "UNKNOWN"):
            with self.subTest(result=result_code):
                result = self.missing_wrapper(self.broker(result_code))
                self.assertEqual(result["result"], "ELEVATED_WRAPPER_FAILED")
                self.assertFalse(result["success"])
                self.assertEqual(result["result_source"], "wrapper-result-missing")

    def test_missing_wrapper_requires_correlated_failure_reason(self):
        result = self.missing_wrapper(
            self.broker("TIMEOUT", reason="CHILD_FAILED")
        )
        self.assertEqual(result["result"], "ELEVATED_WRAPPER_FAILED")
        self.assertIsNone(result["broker_terminal_cause"])
        result = self.missing_wrapper(self.broker("TIMEOUT", success=True))
        self.assertEqual(result["result"], "ELEVATED_WRAPPER_FAILED")
        self.assertIsNone(result["broker_terminal_cause"])

    def test_cleanup_failure_precedes_broker_terminal_cause(self):
        result = self.missing_wrapper(
            self.broker("TIMEOUT"), cleanup_failure="wrapper cleanup failed"
        )
        self.assertEqual(result["result"], "OPERATOR_CHILD_LEAK")
        self.assertFalse(result["success"])
        self.assertEqual(result["operator_owned_active_children"], 1)
        self.assertEqual(result["operator_owned_orphans"], 1)
        self.assertEqual(result["result_source"], "cleanup-failure")

    def test_existing_wrapper_result_remains_authoritative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = self.broker("CHILD_FAILED")
            wrapper_result = {
                "success": False,
                "result": "TIMEOUT",
                "operator_owned_active_children": 0,
                "operator_owned_orphans": 0,
                "exact_active_after_close": [],
            }

            def broker_with_wrapper(argv, **_kwargs):
                spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
                om._write_json(Path(spec["result_path"]), wrapper_result)
                return broker

            with patch.object(
                om, "run_maintenance_command", side_effect=broker_with_wrapper
            ), patch.object(om, "_parent_identity_alive", return_value=False):
                result = om.run_elevated_maintenance_command(
                    [sys.executable, "-c", "pass"],
                    command_class="test-wrapper-present",
                    evidence_directory=root,
                )
            self.assertEqual(result["result"], "TIMEOUT")
            self.assertFalse(result["success"])
            self.assertTrue(result["wrapper_result_present"])
            self.assertEqual(result["result_source"], "wrapper")


class BoundedFileReadTests(unittest.TestCase):
    def test_large_log_reads_only_tail_and_full_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.log"
            path.write_bytes(b"A" * 10000 + b"TAIL")
            self.assertEqual(om.read_bounded_bytes(path, 16, tail=True), b"A" * 12 + b"TAIL")
            with self.assertRaisesRegex(ValueError, "exceeds"):
                om.read_bounded_bytes(path, 16)


if __name__ == "__main__":
    unittest.main()
