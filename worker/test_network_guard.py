import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
from vnext_runtime.guards.network import GuardVerdict, NetworkGuard
from vnext_runtime.models import ExecutionProfile, ExecutionRequest, RunIdentity
from vnext_runtime.run_scope import RunScope


def guard_config():
    return {
        "network_guard": {
            "enabled": True,
            "probe_url": "https://probe.invalid/trace",
            "timeout_seconds": 1,
            "watchdog_interval_seconds": 1,
            "blocked_country_codes": ["CN"],
            "fail_closed": True,
        }
    }


class NetworkGuardTests(unittest.TestCase):
    def test_policy_preserves_https_and_country_rules(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"country_code":"US"}'

        guard = NetworkGuard(guard_config(), opener=lambda *_args, **_kwargs: Response())
        self.assertEqual(guard.preflight(), GuardVerdict(True, "country allowed: US", "US"))
        guard.dispose()

        invalid = guard_config()
        invalid["network_guard"]["probe_url"] = "http://probe.invalid/trace"
        self.assertEqual(
            NetworkGuard(invalid).preflight(),
            GuardVerdict(False, "probe URL is not HTTPS"),
        )

    def test_scope_owns_guard_and_disposal_makes_callback_safe(self):
        calls = []
        guard = NetworkGuard(
            guard_config(),
            probe_check=lambda: (calls.append("A") or GuardVerdict(False, "country blocked: CN", "CN")),
        )
        scope = RunScope(
            RunIdentity("project", 5, "run-005-a", 10),
            run_dir=Path("runtime/runs/run-005-a"),
        )
        scope.activate()
        scope.own_guard(guard)
        callback = guard.watchdog_callback()

        self.assertEqual(callback(), (False, "country blocked: CN"))
        scope.close()
        scope.close()
        self.assertTrue(guard.disposed)
        self.assertEqual(callback(), (True, "guard disposed"))
        self.assertEqual(calls, ["A"])

    def test_run_a_disposal_cannot_affect_run_b(self):
        run_a = NetworkGuard(
            guard_config(),
            probe_check=lambda: GuardVerdict(False, "country blocked: CN", "CN"),
        )
        run_b = NetworkGuard(
            guard_config(),
            probe_check=lambda: GuardVerdict(True, "country allowed: US", "US"),
        )
        scope_a = RunScope(RunIdentity("project", 5, "run-005-a", 10))
        scope_b = RunScope(RunIdentity("project", 6, "run-006-b", 11))
        scope_a.activate()
        scope_b.activate()
        scope_a.own_guard(run_a)
        scope_b.own_guard(run_b)
        callback_a = run_a.watchdog_callback()
        callback_b = run_b.watchdog_callback()

        scope_a.close()
        self.assertEqual(callback_a(), (True, "guard disposed"))
        self.assertEqual(callback_b(), (True, "country allowed: US"))
        self.assertTrue(run_a.disposed)
        self.assertFalse(run_b.disposed)
        scope_b.close()

    def test_guard_has_no_canonical_or_executor_surface(self):
        guard = NetworkGuard(guard_config())
        self.assertFalse(hasattr(guard, "publish_cas"))
        self.assertFalse(hasattr(guard, "publish_report"))
        self.assertFalse(hasattr(guard, "execute"))
        self.assertEqual(guard.guard_id, "network")
        guard.dispose()

    def test_scoped_codex_adapter_registers_guard_on_exact_scope(self):
        scope = RunScope(RunIdentity("project", 5, "run-005-a", 10))
        scope.activate()
        request = ExecutionRequest(
            project_id="project",
            command_id=5,
            run_id="run-005-a",
            workdir=Path("X:/synthetic/c/candidate"),
            mission="mission",
            command_text="command",
            kind="EXECUTE",
            profile=ExecutionProfile(),
        )
        captured = {}

        def fake_legacy(**kwargs):
            captured["guard"] = kwargs["network_guard"]
            self.assertFalse(captured["guard"].disposed)
            return bw.CodexRunResult(
                exit_code=0,
                final_message="",
                stdout_tail="",
                stderr_tail="",
                marker={"status": "SUCCESS"},
                launched_at="2001-01-15T00:00:00+08:00",
                final_marker_detected_at="2001-01-15T00:00:01+08:00",
                process_exited_at="2001-01-15T00:00:02+08:00",
                wrapper_pid=1,
                stdout_log_path=Path("stdout.log"),
                stderr_log_path=Path("stderr.log"),
                process_scope="test",
            )

        config = {
            "network_guard": {
                "enabled": True,
                "probe_url": "https://probe.invalid/trace",
            }
        }
        with patch.object(bw, "_codex_run_legacy", side_effect=fake_legacy):
            bw.codex_run(
                run_scope=scope,
                execution_request=request,
                codex_argv=[sys.executable],
                codex_args=["exec", "--dangerously-bypass-approvals-and-sandbox"],
                workdir=Path("X:/synthetic/c/candidate"),
                output_file=Path("run/final-message.txt"),
                prompt="prompt",
                timeout_seconds=10,
                network_config=config,
            )
        self.assertIsInstance(captured["guard"], NetworkGuard)
        scope.close()
        self.assertTrue(captured["guard"].disposed)

    def test_guard_events_do_not_copy_untrusted_reason_text(self):
        events = []
        guard = NetworkGuard(
            guard_config(),
            probe_check=lambda: GuardVerdict(False, "token=do-not-log", "US"),
            event_logger=lambda event, fields: events.append((event, fields)),
        )
        guard.check()
        self.assertNotIn("do-not-log", repr(events))
        self.assertEqual(events[0][1]["reason"], "network guard verdict unavailable")
        guard.dispose()


if __name__ == "__main__":
    unittest.main()
