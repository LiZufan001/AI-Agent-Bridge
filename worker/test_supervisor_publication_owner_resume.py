import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_supervisor_publication_gateway as fixtures
from supervisor_publication_gateway import SupervisorPublicationGateway


class SupervisorPublicationOwnerResumeTests(unittest.TestCase):
    """Lock the narrow HUMAN_REQUIRED -> COMMAND_READY staged resume path."""

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

    def _write_owner_action(
        self,
        number: int,
        *,
        owner_status: str = "OWNER_REPORTED_DONE",
        verification_status: str = "VERIFIED",
        relates_to_report: int = 5,
    ) -> str:
        action_id = f"owner-action-{number:03d}"
        path = self.project / "owner-actions" / f"{action_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    f"# {action_id}",
                    "",
                    f"- action_id: {action_id}",
                    f"- root_action_id: {action_id}",
                    "- relates_to: null",
                    "- blocker_key: disposable-owner-boundary",
                    f"- owner_status: {owner_status}",
                    f"- verification_status: {verification_status}",
                    "- verification_source: disposable_test_evidence",
                    "- recorded_at: 2001-01-15T01:00:00+08:00",
                    f"- relates_to_report: {relates_to_report}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return str(path.relative_to(self.fixture.worker)).replace("\\", "/")

    def _set_human_required(
        self,
        *,
        active_run: object | None = None,
        actions: list[tuple[int, str, str, int]] | None = None,
    ) -> None:
        state_path = self.project / "state.json"
        state = self.fixture._state()
        state["status"] = "HUMAN_REQUIRED"
        state["human_required"] = True
        state["active_run"] = active_run
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        relatives = ["projects/p/state.json"]
        for number, owner_status, verification_status, report in actions or [
            (1, "OWNER_REPORTED_DONE", "VERIFIED", 5)
        ]:
            relatives.append(
                self._write_owner_action(
                    number,
                    owner_status=owner_status,
                    verification_status=verification_status,
                    relates_to_report=report,
                )
            )
        self.fixture._commit_push_paths(
            self.fixture.worker,
            relatives,
            "set disposable verified owner resume boundary",
        )

    def _poll(self):
        allowed = SimpleNamespace(execution_allowed=True)
        with patch(
            "supervisor_publication_gateway.worker_execution_control.new_execution_allowed",
            return_value=allowed,
        ):
            return SupervisorPublicationGateway(self.fixture.worker).poll()

    def test_verified_owner_action_resumes_and_publishes_same_command(self) -> None:
        self._set_human_required()
        command = self.fixture._command()
        self.fixture._stage_request("resume-ok", command=command)

        results = self._poll()

        result = self.fixture._result(results, "resume-ok")
        self.assertEqual((result.outcome, result.reason), ("published", "published"))
        state = self.fixture._state()
        self.assertEqual(state["status"], "COMMAND_READY")
        self.assertEqual(state["generation"], 11)
        self.assertEqual(state["latest_command"], 6)
        self.assertEqual(state["last_reviewed_report"], 5)
        self.assertFalse(state["human_required"])
        self.assertIsNone(state["active_run"])
        self.assertEqual(
            (self.project / "commands" / "command-006.md").read_bytes(),
            command,
        )

    def test_pending_owner_verification_is_rejected_without_canonical_write(self) -> None:
        self._set_human_required(
            actions=[(1, "OWNER_REPORTED_DONE", "PENDING", 5)]
        )
        self.fixture._stage_request("resume-pending")
        before = self.fixture._state()

        result = self.fixture._result(self._poll(), "resume-pending")

        self.assertEqual(
            (result.outcome, result.reason),
            ("stale", "not_report_ready"),
        )
        self.assertEqual(self.fixture._state(), before)
        self.assertFalse((self.project / "commands" / "command-006.md").exists())

    def test_newest_matching_owner_action_must_still_be_verified(self) -> None:
        self._set_human_required(
            actions=[
                (1, "OWNER_REPORTED_DONE", "VERIFIED", 5),
                (2, "OWNER_REPORTED_DONE", "PENDING", 5),
            ]
        )
        self.fixture._stage_request("resume-newest-pending")

        result = self.fixture._result(self._poll(), "resume-newest-pending")

        self.assertEqual(
            (result.outcome, result.reason),
            ("stale", "not_report_ready"),
        )
        self.assertEqual(self.fixture._state()["status"], "HUMAN_REQUIRED")

    def test_verified_action_for_old_report_does_not_resume_current_boundary(self) -> None:
        self._set_human_required(
            actions=[(1, "OWNER_REPORTED_DONE", "VERIFIED", 4)]
        )
        self.fixture._stage_request("resume-old-report")

        result = self.fixture._result(self._poll(), "resume-old-report")

        self.assertEqual(
            (result.outcome, result.reason),
            ("stale", "not_report_ready"),
        )
        self.assertEqual(self.fixture._state()["status"], "HUMAN_REQUIRED")

    def test_human_required_finalizer_does_not_use_resume_path(self) -> None:
        self._set_human_required()
        command = self.fixture._command(source="finalizer", kind="FINALIZE")
        self.fixture._stage_request(
            "resume-finalizer",
            command=command,
            source="finalizer",
            kind="FINALIZE",
        )

        result = self.fixture._result(self._poll(), "resume-finalizer")

        self.assertEqual(
            (result.outcome, result.reason),
            ("stale", "not_report_ready"),
        )
        self.assertEqual(self.fixture._state()["status"], "HUMAN_REQUIRED")
        self.assertFalse((self.project / "commands" / "command-006.md").exists())

    def test_active_run_still_blocks_owner_resume(self) -> None:
        self._set_human_required(
            active_run={"run_id": "unexpected-active-run", "command_id": 5}
        )
        self.fixture._stage_request("resume-active")

        result = self.fixture._result(self._poll(), "resume-active")

        self.assertEqual(
            (result.outcome, result.reason),
            ("active_project", "active_project"),
        )
        self.assertEqual(self.fixture._state()["status"], "HUMAN_REQUIRED")


if __name__ == "__main__":
    unittest.main()
