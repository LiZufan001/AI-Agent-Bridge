import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import windows_worker_launcher as launcher
from vnext_runtime.handoff import HandoffConsumeResult, HandoffConsumeStatus


class _FakeProcess:
    def __init__(self, code: int, pid: int):
        self.code = code
        self.pid = pid

    def wait(self, timeout=None):
        return self.code


class WorkerRestartProtocolTests(unittest.TestCase):
    def setUp(self):
        from test_state_fixture import make_state
        self.state_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_temp.cleanup)
        self.state_root = make_state(Path(self.state_temp.name))
        root_mock = patch.object(launcher.state_roots, "resolve_state_root", return_value=self.state_root)
        root_mock.start()
        self.addCleanup(root_mock.stop)

    def test_launcher_owns_one_consumer_across_worker_replacements(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = root / "worker.py"
            config = root / "config.json"
            log = root / "launcher.log"
            worker.write_text("pass\n", encoding="utf-8")
            config.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(worker_script=worker, config=config, log_file=log, state_root=self.state_root)
            scope = launcher._NoopLifetimeScope()
            consumer = MagicMock()
            consumer.try_consume_after_worker_exit.side_effect = [
                HandoffConsumeResult(HandoffConsumeStatus.NO_HANDOFF, None, None),
                HandoffConsumeResult(HandoffConsumeStatus.NO_HANDOFF, None, None),
            ]

            with patch.object(launcher, "parse_args", return_value=args), patch.object(
                launcher, "create_lifetime_scope", return_value=scope
            ), patch.object(
                launcher, "resolve_worker_entrypoint", return_value=worker
            ), patch.object(
                launcher, "create_handoff_consumer", return_value=consumer
            ) as create_consumer, patch.object(
                launcher.subprocess,
                "Popen",
                side_effect=[_FakeProcess(launcher.WORKER_RESTART_CODE, 101), _FakeProcess(0, 102)],
            ), patch.object(launcher.time, "sleep"):
                self.assertEqual(launcher.main(), 0)

            create_consumer.assert_called_once_with(self.state_root / "worker/runtime")
            self.assertEqual(
                consumer.try_consume_after_worker_exit.call_args_list,
                [
                    unittest.mock.call(worker_exit_code=launcher.WORKER_RESTART_CODE),
                    unittest.mock.call(worker_exit_code=0),
                ],
            )

    def test_launcher_restarts_once_on_dedicated_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = root / "worker.py"
            config = root / "config.json"
            log = root / "launcher.log"
            worker.write_text("pass\n", encoding="utf-8")
            config.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(worker_script=worker, config=config, log_file=log, state_root=self.state_root)
            scope = launcher._NoopLifetimeScope()

            with patch.object(launcher, "parse_args", return_value=args), patch.object(
                launcher, "create_lifetime_scope", return_value=scope
            ), patch.object(
                launcher, "resolve_worker_entrypoint", return_value=worker
            ), patch.object(
                launcher.subprocess,
                "Popen",
                side_effect=[_FakeProcess(launcher.WORKER_RESTART_CODE, 101), _FakeProcess(0, 102)],
            ) as popen, patch.object(launcher.time, "sleep") as sleep:
                self.assertEqual(launcher.main(), 0)

            self.assertEqual(popen.call_count, 2)
            sleep.assert_called_once_with(1)
            text = log.read_text(encoding="utf-8")
            self.assertIn("worker_restart_requested", text)

    def test_launcher_does_not_die_after_old_restart_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = root / "worker.py"
            config = root / "config.json"
            log = root / "launcher.log"
            worker.write_text("pass\n", encoding="utf-8")
            config.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(worker_script=worker, config=config, log_file=log, state_root=self.state_root)
            scope = launcher._NoopLifetimeScope()
            processes = [
                _FakeProcess(launcher.WORKER_RESTART_CODE, 200 + index)
                for index in range(launcher.RESTART_DIAGNOSTIC_THRESHOLD + 1)
            ]
            processes.append(_FakeProcess(0, 299))

            with patch.object(launcher, "parse_args", return_value=args), patch.object(
                launcher, "create_lifetime_scope", return_value=scope
            ), patch.object(
                launcher, "resolve_worker_entrypoint", return_value=worker
            ), patch.object(
                launcher.subprocess, "Popen", side_effect=processes
            ) as popen, patch.object(launcher.time, "sleep"):
                self.assertEqual(launcher.main(), 0)

            self.assertEqual(
                popen.call_count,
                launcher.RESTART_DIAGNOSTIC_THRESHOLD + 2,
            )
            log_text = log.read_text(encoding="utf-8")
            self.assertIn("worker_restart_failure_threshold", log_text)
            self.assertNotIn("worker_restart_limit_exceeded", log_text)

    def test_launcher_restarts_nonzero_exit_with_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = root / "worker.py"
            config = root / "config.json"
            log = root / "launcher.log"
            worker.write_text("pass\n", encoding="utf-8")
            config.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(worker_script=worker, config=config, log_file=log, state_root=self.state_root)
            scope = launcher._NoopLifetimeScope()

            with patch.object(launcher, "parse_args", return_value=args), patch.object(
                launcher, "create_lifetime_scope", return_value=scope
            ), patch.object(
                launcher, "resolve_worker_entrypoint", return_value=worker
            ), patch.object(
                launcher.subprocess,
                "Popen",
                side_effect=[_FakeProcess(1, 301), _FakeProcess(0, 302)],
            ) as popen, patch.object(launcher.time, "sleep") as sleep:
                self.assertEqual(launcher.main(), 0)

            self.assertEqual(popen.call_count, 2)
            sleep.assert_called_once_with(1.0)
            self.assertIn("reason=worker_nonzero_exit", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
