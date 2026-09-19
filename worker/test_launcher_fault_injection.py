import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import worker_health
import windows_worker_launcher as launcher
from vnext_runtime.adoption import AdoptionMode, AdoptionPolicy, RollbackIdentity
from vnext_runtime.handoff import HandoffIdentity
from vnext_runtime.launcher_actions import (
    FaultInjection,
    LauncherActionError,
    LauncherActionConfig,
    LauncherOwnedHandoffActions,
)


HEALTH_TIME = "2024-01-01T00:00:00+00:00"
SHA_STABLE = "a" * 40
SHA_MAIN = "b" * 40
SHA_CANDIDATE = "c" * 40
SHA_ADOPTED = "d" * 40


class _LiveWorker:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None


class LauncherFaultInjectionTests(unittest.TestCase):
    def _fixture(self, launcher_actions=None):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        worker = root / "worker"
        runtime = worker / "runtime"
        runtime.mkdir(parents=True)
        (worker / "bridge_worker_hardened.py").write_text(
            "# fixture\n",
            encoding="utf-8",
        )
        config_path = worker / "config.json"
        payload = {
            "adoption": {
                "controlled_adoption_enabled": True,
                "unattended_adoption_enabled": False,
            }
        }
        if launcher_actions is not None:
            payload["launcher_actions"] = launcher_actions
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return temporary, root, runtime, config_path

    @staticmethod
    def _controller(root, runtime, config_path, policy=None):
        return launcher.create_launcher_action_controller(
            repository_root=root,
            runtime_root=runtime,
            worker_script=root / "worker" / "bridge_worker_hardened.py",
            config_path=config_path,
            log_file=root / "worker" / "logs" / "worker.log",
            policy=policy or AdoptionPolicy(controlled_adoption_enabled=True),
        )

    def _identity(self, command_id: int) -> HandoffIdentity:
        return HandoffIdentity(
            attempt_id=f"attempt-{command_id:03d}",
            initiated_at=HEALTH_TIME,
            previous_stable_sha=SHA_STABLE,
            latest_main_sha=SHA_MAIN,
            accepted_candidate_sha=SHA_CANDIDATE,
            reconciled_adoption_sha=SHA_ADOPTED,
            initiating_project_id="example-service",
            initiating_run_id=f"run-{command_id:03d}-test",
            initiating_command_id=command_id,
            initiating_claim_generation=10,
            expected_launcher_identity="launcher-v1",
            expected_worker_identity="worker-v1",
            startup_contract=("outer_launcher_job", "exit_75_restart"),
            health_deadline="2099-01-01T00:00:00+00:00",
            health_criteria=(
                "launcher_alive",
                "worker_healthy",
                "protocol_ready",
                "recovery_clear",
            ),
            probation_seconds=2,
            rollback_identity=RollbackIdentity(known_good_sha=SHA_STABLE),
            mode=AdoptionMode.MANUAL,
        )

    def _write_idle_health(self, root: Path, runtime: Path, pid: int) -> None:
        worker_health.update_worker_health(
            root,
            runtime_root=runtime,
            max_parallel_runs=1,
            active_run_count=0,
            active_runs=[],
            worker_pid=pid,
            worker_started_at=HEALTH_TIME,
            last_poll_at=HEALTH_TIME,
            last_successful_poll_at=HEALTH_TIME,
            last_successful_fetch_at=HEALTH_TIME,
            worker_healthy=True,
            protocol_ready=True,
            recovery_clear=True,
        )

    def test_missing_or_default_configuration_is_none(self):
        temporary, root, runtime, config_path = self._fixture()
        try:
            action = self._controller(root, runtime, config_path)
            self.assertIsInstance(action, LauncherOwnedHandoffActions)
            self.assertEqual(action.config.fault_injection, FaultInjection.NONE)
            self.assertIsNone(action.config.fault_injection_command_id)
        finally:
            temporary.cleanup()

    def test_controlled_configuration_binds_probation_failure_to_synthetic_command(self):
        temporary, root, runtime, config_path = self._fixture(
            {
                "fault_injection": "probation_failure",
                "fault_injection_command_id": 2,
            }
        )
        try:
            action = self._controller(root, runtime, config_path)
            self.assertIsInstance(action, LauncherOwnedHandoffActions)
            self.assertEqual(
                action.config.fault_injection,
                FaultInjection.PROBATION_FAILURE,
            )
            self.assertEqual(action.config.fault_injection_command_id, 2)
        finally:
            temporary.cleanup()

    def test_unattended_or_uncontrolled_policy_cannot_arm_fault(self):
        temporary, root, runtime, config_path = self._fixture(
            {
                "fault_injection": "probation_failure",
                "fault_injection_command_id": 2,
            }
        )
        try:
            self.assertIsNone(
                self._controller(
                    root,
                    runtime,
                    config_path,
                    AdoptionPolicy(controlled_adoption_enabled=False),
                )
            )
            unattended = self._controller(
                root,
                runtime,
                config_path,
                AdoptionPolicy(
                    controlled_adoption_enabled=True,
                    unattended_adoption_enabled=True,
                ),
            )
            self.assertIsInstance(unattended, LauncherOwnedHandoffActions)
            self.assertEqual(unattended.config.fault_injection, FaultInjection.NONE)
            self.assertIsNone(unattended.config.fault_injection_command_id)
        finally:
            temporary.cleanup()

    def test_malformed_configuration_fails_closed_to_none(self):
        malformed = (
            {"fault_injection": "probation_failure"},
            {
                "fault_injection": "probation_failure",
                "fault_injection_command_id": "2",
            },
            {
                "fault_injection": "probation_failure",
                "fault_injection_command_id": True,
            },
            {
                "fault_injection": "rollback_failure",
                "fault_injection_command_id": 2,
            },
            {"unsupported_hook": "python -c anything"},
        )
        for value in malformed:
            with self.subTest(value=value):
                temporary, root, runtime, config_path = self._fixture(value)
                try:
                    action = self._controller(root, runtime, config_path)
                    self.assertIsInstance(action, LauncherOwnedHandoffActions)
                    self.assertEqual(action.config.fault_injection, FaultInjection.NONE)
                    self.assertIsNone(action.config.fault_injection_command_id)
                finally:
                    temporary.cleanup()

    def test_corrupt_json_configuration_fails_closed(self):
        temporary, root, runtime, config_path = self._fixture()
        try:
            config_path.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(launcher._controlled_adoption_policy(config_path))
        finally:
            temporary.cleanup()

    def test_health_observation_injects_target_and_treats_next_command_as_none(self):
        temporary, root, runtime, config_path = self._fixture(
            {
                "fault_injection": "probation_failure",
                "fault_injection_command_id": 2,
            }
        )
        try:
            action = self._controller(root, runtime, config_path)
            self.assertIsInstance(action, LauncherOwnedHandoffActions)
            self._write_idle_health(root, runtime, 3400)
            action._replacement_process = _LiveWorker(3400)

            target_command = action.observe_health(self._identity(2))
            next_command = action.observe_health(self._identity(3))

            self.assertFalse(target_command.worker_healthy)
            self.assertTrue(next_command.worker_healthy)
            self.assertEqual(action.active_run_ids(self._identity(3)), ())
        finally:
            temporary.cleanup()

    def test_direct_probation_config_requires_controlled_exact_command(self):
        temporary, root, runtime, config_path = self._fixture()
        try:
            common = dict(
                repository_root=root,
                runtime_root=runtime,
                worker_script=root / "worker" / "bridge_worker_hardened.py",
                config_path=config_path,
                log_file=root / "worker" / "logs" / "worker.log",
                adoption_policy=AdoptionPolicy(controlled_adoption_enabled=True),
            )
            with self.assertRaises(LauncherActionError):
                LauncherActionConfig(
                    **common,
                    fault_injection=FaultInjection.PROBATION_FAILURE,
                )
            with self.assertRaises(LauncherActionError):
                LauncherActionConfig(
                    **common,
                    fault_injection=FaultInjection.NONE,
                    fault_injection_command_id=2,
                )
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
