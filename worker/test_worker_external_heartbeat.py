from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import worker_health


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _limit: int) -> bytes:
        return b"{}"


class WorkerExternalHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        binding = patch.dict(worker_health.os.environ, {"BRIDGE_LOCAL_WORKER_HOST": "SYNTHETIC-HOST"})
        binding.start()
        self.addCleanup(binding.stop)
        worker_health._EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC = 0.0
        worker_health._EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC = 0.0

    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "supervisor").mkdir(parents=True)
        (root / "supervisor" / "bootstrap.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repository": "example-owner/AI-Agent-Bridge",
                    "entrypoint": "SUPERVISOR_ENTRYPOINT.md",
                    "portfolio_config": "supervisor/portfolio.json",
                    "heartbeat": {
                        "issue_number": 6,
                        "comment_id": 10001,
                        "worker_id": "synthetic-worker-alias",
                        "heartbeat_interval_seconds": 120,
                        "ttl_seconds": 300,
                        "max_future_skew_seconds": 60,
                    },
                    "staged_publication": {
                        "request_directory": "worker/staged-publications/requests",
                        "max_lifetime_seconds": 600,
                        "max_future_skew_seconds": 60,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return root

    def test_contract_is_loaded_only_from_exact_bootstrap_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            contract = worker_health._read_external_heartbeat_contract(root)
            self.assertIsNotNone(contract)
            assert contract is not None
            self.assertEqual(contract["comment_id"], 10001)
            self.assertEqual(contract["heartbeat_interval_seconds"], 120)
            self.assertEqual(contract["ttl_seconds"], 300)

            data = json.loads((root / "supervisor" / "bootstrap.json").read_text())
            data["heartbeat"]["extra"] = True
            (root / "supervisor" / "bootstrap.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            self.assertIsNone(worker_health._read_external_heartbeat_contract(root))

    def test_only_running_exact_worker_can_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            with patch.object(worker_health.platform, "node", return_value="SYNTHETIC-HOST"), patch.object(
                worker_health, "_patch_external_heartbeat", return_value=True
            ) as publish:
                worker_health._maybe_publish_external_heartbeat(
                    root, {"coordinator_lifecycle": "DRAINING"}
                )
                publish.assert_not_called()
                worker_health._maybe_publish_external_heartbeat(
                    root, {"coordinator_lifecycle": "RUNNING"}
                )
                publish.assert_called_once()

            worker_health._EXTERNAL_HEARTBEAT_LAST_SUCCESS_MONOTONIC = 0.0
            worker_health._EXTERNAL_HEARTBEAT_LAST_ATTEMPT_MONOTONIC = 0.0
            with patch.object(worker_health.platform, "node", return_value="OTHER-HOST"), patch.object(
                worker_health, "_patch_external_heartbeat", return_value=True
            ) as publish:
                worker_health._maybe_publish_external_heartbeat(
                    root, {"coordinator_lifecycle": "RUNNING"}
                )
                publish.assert_not_called()

    def test_success_throttles_to_configured_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            with patch.object(worker_health.platform, "node", return_value="SYNTHETIC-HOST"), patch.object(
                worker_health.time, "monotonic", side_effect=[100.0, 100.1, 150.0]
            ), patch.object(
                worker_health, "_patch_external_heartbeat", return_value=True
            ) as publish:
                worker_health._maybe_publish_external_heartbeat(
                    root, {"coordinator_lifecycle": "RUNNING"}
                )
                worker_health._maybe_publish_external_heartbeat(
                    root, {"coordinator_lifecycle": "RUNNING"}
                )
                self.assertEqual(publish.call_count, 1)

    def test_patch_updates_exact_comment_with_minimal_payload(self) -> None:
        contract = {
            "repository": "example-owner/AI-Agent-Bridge",
            "issue_number": 6,
            "comment_id": 10001,
            "worker_id": "synthetic-worker-alias",
            "heartbeat_interval_seconds": 120,
            "ttl_seconds": 300,
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        with patch.object(worker_health, "_github_token", return_value="test-token"), patch.object(
            worker_health.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            self.assertTrue(worker_health._patch_external_heartbeat(contract))

        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/example-owner/AI-Agent-Bridge/issues/comments/10001",
        )
        self.assertEqual(request.get_method(), "PATCH")
        outer = json.loads(request.data.decode("utf-8"))
        body = outer["body"]
        self.assertTrue(body.startswith("```json\n"))
        self.assertTrue(body.endswith("\n```"))
        payload = json.loads(body[len("```json\n") : -len("\n```")])
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "worker_id",
                "last_seen_at",
                "heartbeat_interval_seconds",
                "ttl_seconds",
            },
        )
        self.assertEqual(payload["worker_id"], "synthetic-worker-alias")
        self.assertEqual(payload["heartbeat_interval_seconds"], 120)
        self.assertEqual(payload["ttl_seconds"], 300)
        self.assertNotIn("test-token", body)

    def test_worker_health_calls_external_boundary_only_after_successful_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            runtime = root / "runtime"
            with patch.object(worker_health, "_maybe_publish_external_heartbeat") as publish:
                worker_health.update_worker_health(
                    root,
                    runtime_root=runtime,
                    coordinator_lifecycle="RUNNING",
                    last_poll_at="x",
                )
                publish.assert_not_called()
                data = worker_health.update_worker_health(
                    root,
                    runtime_root=runtime,
                    last_successful_poll_at="y",
                )
                publish.assert_called_once()
                self.assertEqual(
                    publish.call_args.args[1]["coordinator_lifecycle"], "RUNNING"
                )
                self.assertEqual(data["last_successful_poll_at"], "y")

    def test_credentials_are_not_required_for_worker_correctness(self) -> None:
        contract = {
            "repository": "example-owner/AI-Agent-Bridge",
            "issue_number": 6,
            "comment_id": 10001,
            "worker_id": "synthetic-worker-alias",
            "heartbeat_interval_seconds": 120,
            "ttl_seconds": 300,
        }
        with patch.object(worker_health, "_github_token", return_value=None), patch.object(
            worker_health.urllib.request, "urlopen"
        ) as urlopen:
            self.assertFalse(worker_health._patch_external_heartbeat(contract))
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
