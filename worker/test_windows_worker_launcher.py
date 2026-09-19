import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import windows_worker_launcher as launcher
from test_state_fixture import make_state


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=False,
            check=False,
        )
        return str(pid).encode("ascii") in (result.stdout or b"")
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


@unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
class WindowsWorkerLauncherProcessTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_root = make_state(self.root / "state")
        self.worker_pid_file = self.root / "worker.pid"
        self.child_pid_file = self.root / "child.pid"
        self.log_file = self.root / "launcher.log"
        self.worker_script = self.root / "dummy_worker.py"
        self.child_script = self.root / "dummy_child.py"
        self.config_file = self.root / "config.json"
        self.config_file.write_text(json.dumps({}), encoding="utf-8")
        self.child_script.write_text(
            textwrap.dedent(
                """
                import os
                import time
                from pathlib import Path

                Path(os.environ['CHILD_PID_FILE']).write_text(
                    str(os.getpid()), encoding='ascii'
                )
                time.sleep(300)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.worker_script.write_text(
            textwrap.dedent(
                """
                import os
                import subprocess
                import sys
                import time
                from pathlib import Path

                child = subprocess.Popen(
                    [sys.executable, os.environ['CHILD_SCRIPT']],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                Path(os.environ['WORKER_PID_FILE']).write_text(
                    str(os.getpid()), encoding='ascii'
                )
                time.sleep(float(os.environ.get('WORKER_LIFETIME', '300')))
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.launcher_process: subprocess.Popen[str] | None = None

    def tearDown(self) -> None:
        if self.launcher_process is not None and self.launcher_process.poll() is None:
            self.launcher_process.terminate()
            try:
                self.launcher_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.launcher_process.kill()
                self.launcher_process.wait(timeout=5)
        for path in (self.worker_pid_file, self.child_pid_file):
            if not path.exists():
                continue
            try:
                pid = int(path.read_text(encoding="ascii"))
            except (OSError, ValueError):
                continue
            if pid_exists(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
        self.temp_dir.cleanup()

    def start_launcher(self, *, worker_lifetime: float = 300) -> subprocess.Popen[str]:
        env = {
            **os.environ,
            "WORKER_PID_FILE": str(self.worker_pid_file),
            "CHILD_PID_FILE": str(self.child_pid_file),
            "CHILD_SCRIPT": str(self.child_script),
            "WORKER_LIFETIME": str(worker_lifetime),
            "PYTHONPATH": str(Path(launcher.__file__).resolve().parent),
        }
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(launcher.__file__).resolve()),
                "--worker-script",
                str(self.worker_script),
                "--config",
                str(self.config_file),
                "--log-file",
                str(self.log_file),
                "--state-root",
                str(self.state_root),
            ],
            cwd=str(Path(launcher.__file__).resolve().parents[1]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.launcher_process = process
        return process

    def read_pids(self) -> tuple[int, int]:
        parsed: tuple[int, int] | None = None

        def pid_files_ready() -> bool:
            nonlocal parsed
            try:
                worker_pid = int(self.worker_pid_file.read_text(encoding="ascii").strip())
                child_pid = int(self.child_pid_file.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                return False
            if worker_pid <= 0 or child_pid <= 0:
                return False
            parsed = (worker_pid, child_pid)
            return True

        self.assertTrue(
            wait_until(pid_files_ready),
            "worker/child PID files never became readable positive integers",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        return parsed

    def test_running_launcher_keeps_worker_tree_alive(self):
        process = self.start_launcher()
        self.assertTrue(
            wait_until(
                lambda: self.worker_pid_file.exists() and self.child_pid_file.exists()
            )
        )
        worker_pid, child_pid = self.read_pids()
        self.assertIsNone(process.poll())
        self.assertTrue(pid_exists(worker_pid))
        self.assertTrue(pid_exists(child_pid))
        time.sleep(0.5)
        self.assertTrue(pid_exists(worker_pid))
        self.assertTrue(pid_exists(child_pid))

    def test_launcher_termination_kills_worker_and_child_tree(self):
        process = self.start_launcher()
        self.assertTrue(
            wait_until(
                lambda: self.worker_pid_file.exists() and self.child_pid_file.exists()
            )
        )
        worker_pid, child_pid = self.read_pids()
        process.terminate()
        process.wait(timeout=10)
        self.assertTrue(wait_until(lambda: not pid_exists(worker_pid)))
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))

    def test_normal_launcher_exit_does_not_leave_worker_child(self):
        process = self.start_launcher(worker_lifetime=0.5)
        self.assertTrue(
            wait_until(
                lambda: self.worker_pid_file.exists() and self.child_pid_file.exists()
            )
        )
        _, child_pid = self.read_pids()
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))

    def test_nested_per_run_job_cleans_orphan_inside_launcher_job(self):
        result_file = self.root / "nested-result.json"
        final_file = self.root / "nested" / "final-message.txt"
        self.child_script.write_text(
            textwrap.dedent(
                """
                import os
                import subprocess
                import sys
                from pathlib import Path

                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
                Path(os.environ['CHILD_PID_FILE']).write_text(str(child.pid), encoding='ascii')
                output = Path(os.environ['NESTED_FINAL_FILE'])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}\\n',
                    encoding='utf-8',
                )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.worker_script.write_text(
            textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                from codex_lifecycle import run_codex_process

                def parse_marker(text):
                    marker = 'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}'
                    return {"status": "SUCCESS"} if text.strip() == marker else None

                output = Path(os.environ['NESTED_FINAL_FILE'])
                result = run_codex_process(
                    args=[sys.executable, os.environ['CHILD_SCRIPT']],
                    workdir=Path.cwd(),
                    output_file=output,
                    stdout_log_path=output.with_name('stdout.log'),
                    stderr_log_path=output.with_name('stderr.log'),
                    prompt='test',
                    execution_timeout_seconds=5,
                    final_grace_timeout_seconds=0.3,
                    cleanup_timeout_seconds=2,
                    marker_stable_seconds=0.08,
                    poll_interval_seconds=0.02,
                    max_log_bytes=1048576,
                    max_final_message_bytes=1048576,
                    marker_parser=parse_marker,
                )
                Path(os.environ['NESTED_RESULT_FILE']).write_text(
                    json.dumps({
                        'scope': result.process_scope,
                        'forced': result.forced_cleanup_after_final,
                        'status': (result.marker or {}).get('status'),
                        'final_message': result.final_message,
                        'exit_code': result.exit_code,
                        'stderr': result.stderr_tail,
                        'runtime_error': result.runtime_error,
                        'cleanup_error': result.cleanup_error,
                    }),
                    encoding='utf-8',
                )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        os.environ["NESTED_FINAL_FILE"] = str(final_file)
        os.environ["NESTED_RESULT_FILE"] = str(result_file)
        try:
            process = self.start_launcher()
            self.assertEqual(
                process.wait(timeout=10),
                0,
                self.log_file.read_text(encoding="utf-8", errors="replace"),
            )
        finally:
            os.environ.pop("NESTED_FINAL_FILE", None)
            os.environ.pop("NESTED_RESULT_FILE", None)
        result = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertTrue(self.child_pid_file.exists(), result)
        child_pid = int(self.child_pid_file.read_text(encoding="ascii"))
        self.assertEqual(result["scope"], "windows_per_run_job")
        self.assertTrue(result["forced"])
        self.assertEqual(result["status"], "SUCCESS")
        self.assertIsNone(result["cleanup_error"])
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))


class WindowsWorkerLauncherFallbackTests(unittest.TestCase):
    def test_non_windows_fallback_is_noop(self):
        with patch.object(launcher.os, "name", "posix"):
            scope = launcher.create_lifetime_scope()
        self.assertIsInstance(scope, launcher._NoopLifetimeScope)
        scope.attach_current_process()
        scope.close(graceful=True)


if __name__ == "__main__":
    unittest.main()
