import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "supervisor"))
import project_view as v


class ProjectViewTests(unittest.TestCase):
    def test_synthetic_four_project_projection(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic-state"
        view = v.overview(root)
        self.assertTrue(view["healthy"], view)
        bridge = next(p for p in view["projects"] if p["project_id"] == "engine-maintenance")
        self.assertEqual(bridge["canonical"]["status"], "REPORT_READY")
        self.assertEqual(bridge["canonical"]["latest_command"], 2)
        self.assertEqual(bridge["canonical"]["latest_report"], 2)
        thread = next(
            t for t in bridge["owner_threads"] if t["root_action_id"] == "owner-action-001"
        )
        self.assertEqual(thread["latest_action_id"], "owner-action-003")
        self.assertEqual(thread["disposition"], "RESUME_PENDING")
        self.assertFalse(thread["matches_latest_report"])
        self.assertFalse(thread["requires_attention"])

    def test_project_without_active_goal_does_not_require_current_goal_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "supervisor").mkdir()
            project = root / "projects" / "p"
            (project / "goals").mkdir(parents=True)
            (project / "owner-actions").mkdir()
            (root / "supervisor" / "portfolio.json").write_text(
                '{"projects":[{"project_id":"p","owner_selected":true,'
                '"owner_paused":false,"priority_rank":10}]}',
                encoding="utf-8",
            )
            (project / "state.json").write_text(
                '{"project_id":"p","status":"REPORT_READY","generation":1,'
                '"latest_command":1,"latest_report":1,"last_reviewed_report":1,'
                '"active_run":null}',
                encoding="utf-8",
            )

            view = v.overview(root)

            self.assertTrue(view["healthy"], view)
            self.assertIsNone(view["projects"][0]["active_goal"])

    def test_root_cross_link_does_not_merge_different_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            folder = project / "owner-actions"
            folder.mkdir()
            for i, related in [(1, "null"), (2, "owner-action-001")]:
                (folder / f"owner-action-{i:03d}.md").write_text(
                    f"- action_id: owner-action-{i:03d}\n"
                    f"- root_action_id: owner-action-{i:03d}\n"
                    f"- relates_to: {related}\n"
                    f"- blocker_key: blocker-{i}\n"
                    "- owner_status: AWAITING_OWNER\n"
                    "- verification_status: PENDING\n",
                    encoding="utf-8",
                )
            self.assertEqual(len(v.owner_threads(project)), 2)

    def test_archival_threads_allow_historical_blocker_correction_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            folder = project / "owner-actions"
            folder.mkdir()
            (folder / "owner-action-001.md").write_text(
                "- action_id: owner-action-001\n"
                "- root_action_id: owner-action-001\n"
                "- relates_to: null\n"
                "- blocker_key: original-blocker\n"
                "- owner_status: OWNER_REPORTED_DONE\n"
                "- verification_status: VERIFIED\n",
                encoding="utf-8",
            )
            (folder / "owner-action-002.md").write_text(
                "- action_id: owner-action-002\n"
                "- root_action_id: owner-action-001\n"
                "- relates_to: owner-action-001\n"
                "- blocker_key: target-correction\n"
                "- owner_status: OWNER_REPORTED_DONE\n"
                "- verification_status: VERIFIED\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(v.ProjectViewError, "OWNER_BLOCKER_MISMATCH"):
                v.owner_threads(project)
            archived = v.owner_threads(project, enforce_blocker_consistency=False)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0]["latest_action_id"], "owner-action-002")
            self.assertEqual(archived[0]["blocker_key"], "target-correction")

    def test_missing_root_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / "owner-actions").mkdir()
            (project / "owner-actions/owner-action-002.md").write_text(
                "- action_id: owner-action-002\n"
                "- root_action_id: owner-action-001\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(v.ProjectViewError, "ROOT_MISSING"):
                v.owner_threads(project)

    def test_metadata_duplicate_keys_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "goal.md"
            path.write_text(
                '<!-- bridge-goal: {"goal_id":1,"goal_id":2} -->', encoding="utf-8"
            )
            with self.assertRaises(v.ProjectViewError):
                v.metadata(path, "bridge-goal")

    def test_nonfinite_json_refused(self) -> None:
        with self.assertRaises(v.ProjectViewError):
            v._strict_json_load('{"value": NaN}')


if __name__ == "__main__":
    unittest.main()
