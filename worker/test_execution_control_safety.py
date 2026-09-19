import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bridge_manual as bm
import bridge_worker as bw
import worker_execution_control as c

AUTO = c.ExecutionControlDecision(
    c.AUTO,
    "owner_resume",
    "owner",
    "2026-01-01T00:00:00Z",
    True,
    "valid",
)
PAUSED = c.ExecutionControlDecision(
    c.PAUSED,
    "owner_manual_pause",
    "owner",
    "2026-01-01T00:00:00Z",
    True,
    "valid",
)


class ControlTests(unittest.TestCase):
    def control(self, **changes: object) -> bytes:
        value = {
            "schema_version": 1,
            "execution_mode": c.PAUSED,
            "reason": "owner_quota_saving",
            "set_by": "owner",
            "set_at": "2026-01-01T00:00:00Z",
        }
        value.update(changes)
        return json.dumps(value).encode()

    def test_durable_pause_never_expires_to_auto(self) -> None:
        self.assertFalse(c.parse_control(self.control()).execution_allowed)

    def test_explicit_owner_auto(self) -> None:
        decision = c.parse_control(self.control(execution_mode=c.AUTO, reason="owner_resume"))
        self.assertTrue(decision.execution_allowed)

    def test_legacy_shadow_only_mode_is_rejected(self) -> None:
        with self.assertRaises(c.ExecutionControlError):
            c.parse_control(self.control(execution_mode="shadow_only"))

    def test_missing_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            decision = c.read_execution_control(Path(d), token="test")
        self.assertFalse(decision.execution_allowed)
        self.assertEqual(decision.execution_mode, c.PAUSED)
        self.assertEqual(decision.source_status, "contract_absent")

    def test_invalid_fields_and_future_time_are_rejected(self) -> None:
        invalid = (
            {"set_by": "supervisor"},
            {"execution_mode": c.AUTO},
            {"schema_version": True},
            {"execution_mode": []},
            {"set_at": "9999-01-01T00:00:00Z"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(c.ExecutionControlError):
                c.parse_control(self.control(**changes))

    def test_duplicate_key_refused(self) -> None:
        raw = self.control()[:-1] + b',"execution_mode":"auto"}'
        with self.assertRaisesRegex(c.ExecutionControlError, "duplicate"):
            c.parse_control(raw)

    def test_network_timeout_fails_closed(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic-state"
        with (
            patch.object(c, "_github_token", return_value="unit-test"),
            patch.object(c.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.side_effect = TimeoutError()
            decision = c.read_execution_control(root)
        self.assertFalse(decision.execution_allowed)
        self.assertEqual(decision.execution_mode, c.PAUSED)
        self.assertEqual(decision.source_status, "github_read_failed")

    def test_missing_credentials_fail_closed(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic-state"
        with patch.object(c, "_github_token", return_value=None):
            decision = c.read_execution_control(root)
        self.assertFalse(decision.execution_allowed)

    def test_redirect_refused(self) -> None:
        with self.assertRaises(c.ExecutionControlError):
            c._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")

    def test_all_command_sources_block_before_claim(self) -> None:
        for source in ("scheduled_chatgpt", "manual_chatgpt", "future_source"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                project = root / "projects/p"
                (project / "commands").mkdir(parents=True)
                (project / "reports").mkdir()
                (project / "MISSION.md").write_text("fixture", encoding="utf-8")
                state = {
                    "protocol_version": 2,
                    "project_id": "p",
                    "status": "COMMAND_READY",
                    "generation": 7,
                    "latest_command": 4,
                    "latest_report": 3,
                    "active_run": None,
                }
                path = project / "state.json"
                path.write_text(json.dumps(state), encoding="utf-8")
                before = path.read_bytes()
                (project / "commands/command-004.md").write_text(
                    "<!-- bridge-command: "
                    + json.dumps({"command_id": 4, "source": source})
                    + " -->",
                    encoding="utf-8",
                )
                with (
                    patch.object(c, "read_execution_control", return_value=PAUSED),
                    patch.object(bw, "publish_cas") as publish,
                    patch.object(bw, "_record_worker_health"),
                ):
                    self.assertFalse(bw.process_project(root, "p", {}, {}))
                    publish.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_running_recovery_is_not_blocked_or_terminated_by_pause(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / "projects/p"
            project.mkdir(parents=True)
            (project / "MISSION.md").write_text("fixture", encoding="utf-8")
            (project / "state.json").write_text(
                json.dumps({"protocol_version": 2, "status": "CODEX_RUNNING"}),
                encoding="utf-8",
            )
            with (
                patch.object(bw, "maybe_mark_expired_lease") as recovery,
                patch.object(c, "read_execution_control", return_value=PAUSED) as reader,
                patch.object(bw, "_record_worker_health"),
            ):
                self.assertFalse(bw.process_project(root, "p", {}, {}))
                recovery.assert_called_once()
                reader.assert_not_called()

    def test_manual_start_denied_before_state_payload(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = root / "projects/p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            state = {
                "protocol_version": 2,
                "project_id": "p",
                "status": "REPORT_READY",
                "generation": 7,
                "latest_command": 3,
                "latest_report": 3,
                "active_run": None,
            }
            path = project / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            clone = MagicMock()
            clone.__enter__.return_value = root

            def publish(**kwargs: object) -> object:
                return kwargs["payload_builder"](state)

            with (
                patch.object(bm, "TemporaryBridgeClone", return_value=clone),
                patch.object(bm.git_store, "publish_cas", side_effect=publish),
                patch.object(c, "read_execution_control", return_value=PAUSED),
                self.assertRaisesRegex(bm.WorkerError, "blocks new work"),
            ):
                bm.start_manual(
                    source_root=root,
                    project_id="p",
                    body="fixture",
                    kind="EXECUTE",
                    lease_hours=1,
                )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), state)
            self.assertEqual(list((project / "commands").iterdir()), [])

    def test_atomic_withdraw_and_start_denied_without_withdrawal(self) -> None:
        import test_bridge_manual_withdraw_and_start as fixtures

        fixture = fixtures.AtomicWithdrawAndStartFixtureTests()
        fixture.setUp()
        try:
            before = fixture.state_path.read_bytes()
            with (
                patch.object(c, "read_execution_control", return_value=PAUSED),
                self.assertRaisesRegex(bm.WorkerError, "blocks new work"),
            ):
                fixture._invoke()
            self.assertEqual(fixture.state_path.read_bytes(), before)
            self.assertEqual(fixture.target_path.read_bytes(), fixture.target_bytes_before)
        finally:
            fixture.tearDown()

    def test_gateway_denied_without_canonical_write(self) -> None:
        import test_supervisor_publication_gateway as fixtures

        fixture = fixtures.SupervisorPublicationGatewayIntegrationTests()
        fixture.setUp()
        try:
            fixture._stage_request("paused")
            before = fixture._blob("projects/p/state.json")
            with patch.object(c, "read_execution_control", return_value=PAUSED):
                results = fixture._gateway().poll()
            self.assertEqual(results[0].reason, "owner_execution_blocked")
            self.assertEqual(fixture._blob("projects/p/state.json"), before)
            self.assertFalse((fixture.worker / "projects/p/commands/command-006.md").exists())
        finally:
            fixture.tearDown()

    def test_gateway_rechecks_control_in_cas_payload(self) -> None:
        import test_supervisor_publication_gateway as fixtures

        fixture = fixtures.SupervisorPublicationGatewayIntegrationTests()
        fixture.setUp()
        try:
            fixture._stage_request("mode-changed")
            before = fixture._blob("projects/p/state.json")
            with patch.object(c, "read_execution_control", side_effect=[AUTO, PAUSED]) as reads:
                results = fixture._gateway().poll()
            self.assertEqual(reads.call_count, 2)
            self.assertNotEqual(results[0].outcome, "published")
            self.assertEqual(fixture._blob("projects/p/state.json"), before)
            self.assertFalse((fixture.worker / "projects/p/commands/command-006.md").exists())
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
