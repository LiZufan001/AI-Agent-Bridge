import json
import tempfile
import unittest
from pathlib import Path

import worker_health as health


class WorkerHealthTests(unittest.TestCase):
    def test_health_is_atomic_allowlisted_and_redacts_secret_shapes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "worker-health.json"
            health.update_health(
                path,
                {
                    "last_seen_project": "example-device",
                    "last_seen_state": "COMMAND_READY",
                    "last_command_seen": "example-device#004",
                    "last_failure_kind": (
                        "Authorization: Bearer super-secret-token; "
                        "Cookie: session=private-cookie"
                    ),
                    "untrusted_field": "ghp_should_not_be_written",
                },
            )
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["last_seen_project"], "example-device")
        self.assertEqual(data["last_command_seen"], "example-device#004")
        self.assertEqual(data["last_failure_kind"], "[REDACTED]")
        self.assertNotIn("untrusted_field", data)
        self.assertNotIn("super-secret-token", content)
        self.assertNotIn("private-cookie", content)

    def test_launcher_path_maps_production_logs_to_runtime(self):
        log = Path("X:/synthetic/d/bridge/worker/logs/worker.log")
        self.assertEqual(
            health.launcher_health_path(log),
            Path("X:/synthetic/d/bridge/worker/runtime/launcher-health.json").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
