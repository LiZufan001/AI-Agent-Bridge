import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bridge_dashboard import render_dashboard
from bridge_doctor import main as doctor_main
from vnext_runtime.health_aggregator import HealthAggregator, Severity


NOW = datetime(2001, 1, 15, 12, 0, tzinfo=timezone.utc)


class HealthProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from test_state_fixture import make_state
        make_state(self.root, git=False)
        (self.root / "projects").mkdir(exist_ok=True)
        (self.root / "worker" / "runtime").mkdir(parents=True)
        self.config = {"runtime": {"max_parallel_runs": 1}, "projects": {"p": {"enabled": True}}}
        self._write_json(self.root / "worker" / "config.json", self.config)
        self._health()

    def tearDown(self):
        self.temp.cleanup()

    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def _state(self, project="p", **updates):
        value = {
            "protocol_version": 2, "project_id": project, "status": "REPORT_READY",
            "generation": 4, "latest_command": 4, "latest_report": 4,
            "last_reviewed_report": 4, "active_run": None,
            "updated_at": "2001-01-15T11:59:00+00:00",
        }
        value.update(updates)
        self._write_json(self.root / "projects" / project / "state.json", value)

    def _health(self, **updates):
        value = {"schema_version": 1, "updated_at": "2001-01-15T11:59:00+00:00",
                 "last_poll_at": "2001-01-15T11:59:00+00:00", "coordinator_lifecycle": "RUNNING",
                 "max_parallel_runs": 1, "active_run_count": 0, "active_runs": []}
        value.update(updates)
        self._write_json(self.root / "worker" / "runtime" / "worker-health.json", value)
        self._write_json(self.root / "worker" / "runtime" / "launcher-health.json", value)
        self._write_json(self.root / "worker" / "runtime" / "resource-registry.json", [])

    def _aggregate(self):
        return HealthAggregator(self.root, config_path=self.root / "worker" / "config.json", now=NOW).collect()

    def test_healthy_idle_projects_healthy(self):
        self._state()
        snapshot = self._aggregate()
        self.assertEqual(snapshot.severity, Severity.HEALTHY)
        self.assertEqual(snapshot.projects[0].severity, Severity.HEALTHY)

    def test_active_lease_projects_bounded_run_evidence_without_mutation(self):
        active = {"project_id": "p", "command_id": 5, "run_id": "run-005-test",
                  "claimed_generation": 5, "claimed_at": "2001-01-15T11:50:00+00:00",
                  "lease_expires_at": "2001-01-15T13:00:00+00:00", "provider": "codex", "model": "gpt-test"}
        self._state(status="CODEX_RUNNING", generation=5, latest_command=5, active_run=active)
        before = (self.root / "projects" / "p" / "state.json").read_bytes()
        project = self._aggregate().projects[0]
        self.assertEqual((project.active_run.run_id, project.active_run.provider, project.active_run.model), ("run-005-test", "codex", "gpt-test"))
        self.assertEqual(project.active_run.duration_seconds, 600)
        self.assertEqual(before, (self.root / "projects" / "p" / "state.json").read_bytes())

    def test_slot_wait_is_attention_and_non_authoritative(self):
        self._state(status="COMMAND_READY", generation=5, latest_command=5)
        self._health(resource_wait_project="p", resource_wait_reason="path_conflict")
        project = self._aggregate().projects[0]
        self.assertEqual(project.severity, Severity.ATTENTION)
        self.assertTrue(project.waiting)
        self.assertEqual(project.wait_reason, "path_conflict")

    def test_heartbeat_loss_is_attention_idle_and_action_required_when_running(self):
        self._state()
        self._health(last_poll_at="2001-01-15T08:00:00+00:00")
        self.assertEqual(self._aggregate().severity, Severity.ATTENTION)
        active = {"project_id": "p", "command_id": 5, "run_id": "run-005-test", "claimed_generation": 5,
                  "claimed_at": "2001-01-15T11:50:00+00:00", "lease_expires_at": "2001-01-15T13:00:00+00:00"}
        self._state(status="CODEX_RUNNING", generation=5, active_run=active)
        self.assertEqual(self._aggregate().severity, Severity.ACTION_REQUIRED)

    def test_human_and_recovery_required_are_action_required(self):
        for status in ("HUMAN_REQUIRED", "RECOVERY_REQUIRED"):
            with self.subTest(status=status):
                self._state(status=status)
                self.assertEqual(self._aggregate().projects[0].severity, Severity.ACTION_REQUIRED)

    def test_pending_contradiction_is_visible_without_recovery_invocation(self):
        self._state()
        journal = {"schema_version": 1, "project_id": "p", "run_id": "run-004-test", "command_id": 4,
                   "claim_generation": 4, "interrupted_at": "2001-01-15T11:00:00+00:00",
                   "interruption_kind": "network", "interruption_reason_safe": "unsafe compensation",
                   "external_side_effects_unknown": True, "remote_publish_pending": False,
                   "journal_status": "conflict"}
        self._write_json(self.root / "worker" / "runtime" / "p" / "recovery" / "run-004-test.json", journal)
        before = (self.root / "projects" / "p" / "state.json").read_bytes()
        project = self._aggregate().projects[0]
        self.assertEqual(project.severity, Severity.ACTION_REQUIRED)
        self.assertEqual(project.recovery[0].status, "conflict")
        self.assertEqual(before, (self.root / "projects" / "p" / "state.json").read_bytes())

    def test_corrupt_optional_evidence_is_conservative_and_secret_safe(self):
        self._state()
        health_path = self.root / "worker" / "runtime" / "worker-health.json"
        health_path.write_text("Authorization: bearer super-secret\n", encoding="utf-8")
        snapshot = self._aggregate()
        rendered = snapshot.to_json()
        self.assertEqual(snapshot.worker["severity"], Severity.ATTENTION)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_project_filter_has_only_requested_project_and_host_summary(self):
        self._state("q")
        self.config["projects"]["q"] = {"enabled": True}
        self._write_json(self.root / "worker" / "config.json", self.config)
        snapshot = HealthAggregator(self.root, config_path=self.root / "worker" / "config.json", now=NOW).collect("q")
        self.assertEqual([item.project_id for item in snapshot.projects], ["q"])
        self.assertIn("worker", snapshot.to_dict())

    def test_json_is_deterministic_bounded_and_dashboard_shares_snapshot(self):
        self._state()
        snapshot = self._aggregate()
        self.assertEqual(snapshot.to_json(), snapshot.to_json())
        self.assertLess(len(snapshot.to_json()), 20000)
        self.assertIn("Protocol v2", render_dashboard(snapshot))
        self.assertIn('"projects"', snapshot.to_json())

    def test_capacity_bounds_registry_projection(self):
        self._state()
        self._write_json(self.root / "worker" / "runtime" / "resource-registry.json", [
            {"project_id": "p", "run_id": "run-1", "acquired_at": "2001-01-15T11:00:00+00:00"},
            {"project_id": "q", "run_id": "run-2", "acquired_at": "2001-01-15T11:01:00+00:00"},
        ])
        snapshot = self._aggregate()
        self.assertEqual(snapshot.coordinator["active_run_count"], 1)
        self.assertEqual(len(snapshot.coordinator["active_runs"]), 1)

    def test_doctor_json_uses_aggregator_and_is_machine_parseable(self):
        self._state()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(doctor_main(["--bridge-root", str(self.root), "--config", str(self.root / "worker" / "config.json"), "--json"]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["projects"][0]["project_id"], "p")
        self.assertEqual(value["projects"][0]["severity"], "Healthy")

    def test_observability_module_has_no_mutation_authority(self):
        source = (Path(__file__).parent / "vnext_runtime" / "health_aggregator.py").read_text(encoding="utf-8")
        self.assertNotIn("publish_cas", source)
        self.assertNotIn("claim_command", source)
        self.assertNotIn("recovery_resolution", source)


if __name__ == "__main__":
    unittest.main()
