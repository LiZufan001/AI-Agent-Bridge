import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
import worker_health
import windows_worker_launcher as launcher
from codex_lifecycle import CodexRunResult
from vnext_runtime.handoff import HandoffConflictError
from vnext_runtime.launcher_actions import LauncherActionError
from vnext_runtime.adoption import AdoptionPolicy


def _run_result(status: str = "SUCCESS", code: int = 0) -> CodexRunResult:
    return CodexRunResult(
        exit_code=code,
        final_message=f'BRIDGE_EXECUTION_JSON: {{"status":"{status}"}}',
        stdout_tail="",
        stderr_tail="",
        marker={"status": status},
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at="2001-01-15T00:00:01+08:00",
        process_exited_at="2001-01-15T00:00:02+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result="VALID",
    )


class ActiveRunHealthTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        project = root / "projects" / "p"
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir()
        (project / "MISSION.md").write_text("mission", encoding="utf-8")
        state = {
            "protocol_version": 2,
            "project_id": "p",
            "status": "COMMAND_READY",
            "generation": 7,
            "latest_command": 4,
            "latest_report": 3,
            "active_run": None,
        }
        state_path = project / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (project / "commands" / "command-004.md").write_text(
            '<!-- bridge-command: {"command_id":4,"source":"manual_chatgpt",'
            '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n'
            "# Command 004\n",
            encoding="utf-8",
        )
        return project, state_path

    @staticmethod
    def _config() -> dict[str, object]:
        return {
            "codex_command": sys.executable,
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "network_guard": {"enabled": False},
        }

    @staticmethod
    def _publisher(state_path: Path):
        def publish(**kwargs):
            current = json.loads(state_path.read_text(encoding="utf-8"))
            if not kwargs["expected"](current) and not kwargs["already_applied"](current):
                raise AssertionError("test CAS precondition rejected")
            payloads = kwargs["payload_builder"](current)
            for path, value in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
            return json.loads(payloads[state_path])

        return publish

    @staticmethod
    def _health(root: Path) -> dict[str, object]:
        return worker_health.read_health(worker_health.worker_health_path(root))

    def _run_result_case(self, status: str) -> tuple[dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _project, state_path = self._fixture(root)
            bw.record_idle_run_health(root)
            during: list[dict[str, object]] = []

            def run(**_kwargs):
                during.append(self._health(root))
                return _run_result(status)

            with patch.object(bw, "publish_cas", side_effect=self._publisher(state_path)), \
                patch.object(bw, "get_head", return_value="head"), \
                patch.object(bw, "codex_run", side_effect=run):
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, self._config())
                )
            return during[0], self._health(root)

    def test_idle_projection_is_zero_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bw.record_idle_run_health(root)
            health = self._health(root)
        self.assertEqual(health["max_parallel_runs"], 1)
        self.assertEqual(health["active_run_count"], 0)
        self.assertEqual(health["active_runs"], [])

    def test_success_blocked_failed_and_exception_close_the_exact_projection(self):
        for status in ("SUCCESS", "BLOCKED", "FAILED"):
            with self.subTest(status=status):
                active, idle = self._run_result_case(status)
                self.assertEqual(active["active_run_count"], 1)
                self.assertEqual(len(active["active_runs"]), 1)
                record = active["active_runs"][0]
                self.assertEqual(record["project_id"], "p")
                self.assertTrue(record["run_id"].startswith("run-004-"))
                self.assertEqual(active["active_run_count"], len(active["active_runs"]))
                self.assertEqual(idle["active_run_count"], 0)
                self.assertEqual(idle["active_runs"], [])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _project, state_path = self._fixture(root)
            bw.record_idle_run_health(root)
            during: list[dict[str, object]] = []

            def raising(**_kwargs):
                during.append(self._health(root))
                raise RuntimeError("simulated worker execution failure")

            with patch.object(bw, "publish_cas", side_effect=self._publisher(state_path)), \
                patch.object(bw, "get_head", return_value="head"), \
                patch.object(bw, "codex_run", side_effect=raising):
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, self._config())
                )
            self.assertEqual(during[0]["active_run_count"], 1)
            self.assertEqual(self._health(root)["active_run_count"], 0)
            self.assertEqual(self._health(root)["active_runs"], [])

    def test_unexpected_post_claim_setup_error_does_not_report_quiescence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _project, state_path = self._fixture(root)
            bw.record_idle_run_health(root)
            with patch.object(bw, "publish_cas", side_effect=self._publisher(state_path)), \
                patch.object(bw, "get_head", side_effect=RuntimeError("setup failure")), \
                patch.object(bw, "codex_run") as codex:
                with self.assertRaises(RuntimeError):
                    bw.process_project(
                        root, "p", {"workdir": "__BRIDGE_ROOT__"}, self._config()
                    )
            codex.assert_not_called()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "CODEX_RUNNING")
            self.assertEqual(self._health(root)["active_run_count"], 1)
            self.assertEqual(len(self._health(root)["active_runs"]), 1)

            state["status"] = "REPORT_READY"
            state["active_run"] = None
            state_path.write_text(json.dumps(state), encoding="utf-8")
            bw.refresh_active_run_health(root)
            self.assertEqual(self._health(root)["active_run_count"], 0)
            self.assertEqual(self._health(root)["active_runs"], [])

    def test_malformed_canonical_active_state_stays_non_quiescent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _project, state_path = self._fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "CODEX_RUNNING"
            state["active_run"] = {
                "command_id": 4,
                "claimed_generation": 8,
                "claimed_at": "2001-01-15T00:00:00+08:00",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            bw.refresh_active_run_health(root)
            health = self._health(root)
            self.assertEqual(health["active_run_count"], 1)
            self.assertEqual(health["active_runs"], [{}])

    def test_network_guard_interruption_keeps_active_until_recovery_then_clears(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _project, state_path = self._fixture(root)
            bw.record_idle_run_health(root)
            during: list[dict[str, object]] = []

            def interrupted(**_kwargs):
                during.append(self._health(root))
                raise bw.NetworkGuardInterruption(
                    "network guard became unsafe: test",
                    run_result=_run_result("FAILED", 125),
                )

            with patch.object(bw, "publish_cas", side_effect=self._publisher(state_path)), \
                patch.object(bw, "get_head", return_value="head"), \
                patch.object(bw, "codex_run", side_effect=interrupted):
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, self._config())
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "RECOVERY_REQUIRED")
            self.assertIsNone(state["active_run"])
            self.assertEqual(during[0]["active_run_count"], 1)
            self.assertEqual(self._health(root)["active_run_count"], 0)
            self.assertEqual(self._health(root)["active_runs"], [])

    def test_launcher_reads_exact_active_identity_and_rejects_malformed_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "worker" / "runtime"
            worker = root / "worker"
            worker.mkdir(parents=True)
            script = worker / "bridge_worker_hardened.py"
            config = worker / "config.json"
            script.write_text("# fixture\n", encoding="utf-8")
            config.write_text("{}", encoding="utf-8")
            action = launcher.create_launcher_action_controller(
                repository_root=root,
                runtime_root=runtime,
                worker_script=script,
                config_path=config,
                log_file=worker / "logs" / "worker.log",
                policy=AdoptionPolicy(controlled_adoption_enabled=True),
            )
            self.assertIsInstance(action, launcher.LauncherOwnedHandoffActions)
            worker_health.update_worker_health(
                root,
                runtime_root=runtime,
                max_parallel_runs=1,
                active_run_count=0,
                active_runs=[],
            )
            self.assertEqual(action.active_run_ids(object()), ())

            worker_health.update_worker_health(
                root,
                runtime_root=runtime,
                max_parallel_runs=1,
                active_run_count=1,
                active_runs=[{"project_id": "p", "run_id": "run-004-exact"}],
            )
            self.assertEqual(action.active_run_ids(object()), ("run-004-exact",))

            worker_health.update_worker_health(
                root,
                runtime_root=runtime,
                active_run_count=2,
                active_runs=[{"project_id": "p", "run_id": "run-004-exact"}],
            )
            with self.assertRaises(HandoffConflictError):
                action.active_run_ids(object())

            worker_health.update_worker_health(
                root,
                runtime_root=runtime,
                active_run_count=1,
                active_runs=[{"project_id": "p"}],
            )
            with self.assertRaises(LauncherActionError):
                action.active_run_ids(object())




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
