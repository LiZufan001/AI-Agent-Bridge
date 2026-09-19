import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import supervisor_publication_gateway as gateway
import supervisor_publication_gateway_core as core


class StagedPublicationExpiryTests(unittest.TestCase):
    def test_ttl_contract_is_ten_minutes(self) -> None:
        self.assertEqual(gateway.MAX_STAGED_REQUEST_AGE_SECONDS, 600)
        self.assertEqual(gateway.MAX_STAGED_REQUEST_FUTURE_SKEW_SECONDS, 60)

    def test_fresh_expired_and_future_commit_times_are_classified(self) -> None:
        now = datetime(2001, 1, 15, 0, 0, tzinfo=timezone.utc)
        root = Path("X:/synthetic/c/bridge")
        request = root / "worker/staged-publications/requests/request-r.json"
        cases = (
            (now - timedelta(seconds=599), None),
            (now - timedelta(seconds=601), "request_expired"),
            (now + timedelta(seconds=61), "request_commit_time_future"),
        )
        for committed_at, expected in cases:
            with self.subTest(expected=expected), patch.object(
                gateway,
                "_tracked_request_commit_time",
                return_value=committed_at,
            ):
                self.assertEqual(
                    gateway._request_freshness_reason(root, request, now=now),
                    expected,
                )

    def test_tracked_commit_time_uses_git_history_not_checkout_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "worker/staged-publications/requests/request-r.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf-8")
            timestamp = "2001-01-15T00:00:00+00:00"
            with patch.object(
                gateway.git_store,
                "git",
                return_value=SimpleNamespace(stdout=timestamp + "\n"),
            ) as git:
                observed = gateway._tracked_request_commit_time(root, path)
            self.assertEqual(observed, datetime(2001, 1, 15, 0, 0, tzinfo=timezone.utc))
            relative = gateway.git_store.relative_path(path, root)
            git.assert_called_once_with(
                root,
                "log",
                "-1",
                "--format=%cI",
                "--",
                relative,
            )

    def test_expired_request_stops_before_any_project_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_directory = root / "worker/staged-publications/requests"
            request_directory.mkdir(parents=True)
            request_path = request_directory / "request-r.json"
            request = core.StagedPublicationRequest(
                schema_version=1,
                request_id="r",
                project_id="p",
                command_id=1,
                source="scheduled_chatgpt",
                kind="EXECUTE",
                based_on_report=0,
                expected_generation=1,
                command_sha256="0" * 64,
                command_content="x",
                created_at="2001-01-15T00:00:00+00:00",
                portfolio_pass_id=None,
                command_bytes=b"x",
                metadata={},
                raw_bytes=b"{}",
            )
            subject = gateway.SupervisorPublicationGateway(
                root,
                request_directory=request_directory,
            )
            with patch.object(
                gateway,
                "_request_freshness_reason",
                return_value="request_expired",
            ), patch.object(
                subject,
                "_project_root",
                side_effect=AssertionError("expired request must not inspect project state"),
            ):
                result = subject._consume_one(request_path, request)
            self.assertEqual(result.outcome, "stale")
            self.assertEqual(result.reason, "request_expired")
            self.assertEqual(result.command_id, 1)

    def test_unavailable_commit_time_fails_closed(self) -> None:
        with patch.object(gateway.git_store, "git", side_effect=gateway.git_store.WorkerError("x")):
            self.assertIsNone(
                gateway._tracked_request_commit_time(
                    Path("X:/synthetic/c/bridge"),
                    Path("X:/synthetic/c/bridge/worker/staged-publications/requests/request-r.json"),
                )
            )


if __name__ == "__main__":
    unittest.main()
