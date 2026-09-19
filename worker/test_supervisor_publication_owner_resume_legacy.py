import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_supervisor_publication_gateway as fixtures
from supervisor_publication_gateway import SupervisorPublicationGateway


class SupervisorPublicationLegacyOwnerResumeTests(unittest.TestCase):
    """Legacy root events must not poison a newer verified resume event."""

    def setUp(self) -> None:
        self.fixture = fixtures.SupervisorPublicationGatewayIntegrationTests(
            "test_success_publishes_exact_command_and_state_in_one_commit"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @property
    def project(self) -> Path:
        return self.fixture.worker / "projects" / "p"

    def _write_action(self, name: str, lines: list[str]) -> str:
        path = self.project / "owner-actions" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([f"# {name}", "", *lines, ""]), encoding="utf-8")
        return str(path.relative_to(self.fixture.worker)).replace("\\", "/")

    def test_legacy_root_without_root_action_id_allows_newer_verified_event(self) -> None:
        state_path = self.project / "state.json"
        state = self.fixture._state()
        state["status"] = "HUMAN_REQUIRED"
        state["human_required"] = True
        state["active_run"] = None
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        root = self._write_action(
            "owner-action-003",
            [
                "- action_id: owner-action-003",
                "- blocker_key: disposable-owner-boundary",
                "- owner_status: AWAITING_OWNER",
                "- verification_status: PENDING",
                "- relates_to_report: 5",
            ],
        )
        verified = self._write_action(
            "owner-action-005",
            [
                "- action_id: owner-action-005",
                "- root_action_id: owner-action-003",
                "- relates_to: owner-action-004",
                "- blocker_key: disposable-owner-boundary",
                "- owner_status: OWNER_REPORTED_DONE",
                "- verification_status: VERIFIED",
                "- verification_source: disposable_test_evidence",
                "- relates_to_report: 5",
            ],
        )
        self.fixture._commit_push_paths(
            self.fixture.worker,
            ["projects/p/state.json", root, verified],
            "set legacy-root verified owner resume boundary",
        )
        self.fixture._stage_request("legacy-root-resume")

        allowed = SimpleNamespace(execution_allowed=True)
        with patch(
            "supervisor_publication_gateway.worker_execution_control.new_execution_allowed",
            return_value=allowed,
        ):
            result = self.fixture._result(
                SupervisorPublicationGateway(self.fixture.worker).poll(),
                "legacy-root-resume",
            )

        self.assertEqual((result.outcome, result.reason), ("published", "published"))
        state = self.fixture._state()
        self.assertEqual(state["status"], "COMMAND_READY")
        self.assertEqual(state["generation"], 11)
        self.assertEqual(state["latest_command"], 6)
        self.assertEqual(state["last_reviewed_report"], 5)
        self.assertFalse(state["human_required"])


if __name__ == "__main__":
    unittest.main()
