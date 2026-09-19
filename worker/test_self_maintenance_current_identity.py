import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_BRANCH = "feature/synthetic-candidate"
RETIRED_BASE = "4444444444444444444444444444444444444444"
CURRENT_BRANCH = "maintenance/example-change"
CURRENT_BASE = "3333333333333333333333333333333333333333"


class CurrentSelfMaintenanceIdentityTests(unittest.TestCase):
    def test_active_self_maintenance_source_has_no_retired_fixed_identity(self) -> None:
        text = (ROOT / "worker" / "self_maintenance.py").read_text(encoding="utf-8")
        self.assertNotIn(RETIRED_BRANCH, text)
        self.assertNotIn(RETIRED_BASE, text)
        self.assertIn("operation-specific configuration", text)

    def test_remote_registry_binds_current_dashboard_identity(self) -> None:
        registry = json.loads(
            (ROOT / "tests/fixtures/synthetic-state/worker/remote-projects.json").read_text(encoding="utf-8")
        )
        project = registry["hosts"]["SYNTHETIC-HOST"]["projects"]["engine-maintenance"]
        self.assertFalse(project["enabled"])
        self.assertFalse(project["self_maintenance"]["enabled"])
        self.assertEqual(project["self_maintenance"]["candidate_branch"], CURRENT_BRANCH)
        self.assertEqual(project["self_maintenance"]["bootstrap_base"], CURRENT_BASE)
        self.assertNotEqual(project["self_maintenance"]["candidate_branch"], RETIRED_BRANCH)
        self.assertNotEqual(project["self_maintenance"]["bootstrap_base"], RETIRED_BASE)

    def test_bridge_project_and_template_context_do_not_reinstate_fixed_identity(self) -> None:
        paths = (
            ROOT / "tests/fixtures/synthetic-state/projects" / "engine-maintenance" / "MISSION.md",
            ROOT / "tests/fixtures/synthetic-state/projects" / "engine-maintenance" / "SUPERVISOR.md",
            ROOT / "tests/fixtures/synthetic-state/projects" / "engine-maintenance" / "WORKDIR.md",
            ROOT / "templates" / "project" / "engine-maintenance" / "MISSION.md",
            ROOT / "templates" / "project" / "engine-maintenance" / "README.md",
            ROOT / "templates" / "project" / "engine-maintenance" / "SUPERVISOR.md",
            ROOT / "templates" / "project" / "engine-maintenance" / "WORKDIR.md.template",
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn(RETIRED_BASE, text)
                # Historical names may be mentioned only to state that they are retired.
                if RETIRED_BRANCH in text:
                    lowered = text.casefold()
                    self.assertTrue(
                        any(marker in lowered for marker in ("no standing", "historical", "do not reuse")),
                        msg=f"retired branch appears as live guidance in {path}",
                    )


if __name__ == "__main__":
    unittest.main()
