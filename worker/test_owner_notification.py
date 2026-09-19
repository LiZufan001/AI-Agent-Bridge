import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_lifecycle import CodexRunResult
import owner_notification as notification
import report_builder
from vnext_runtime.models import ExecutionOutcome, ExecutionProfile, ExecutionRequest, RunIdentity
from vnext_runtime.providers.executors.codex import CodexExecutorProvider, CodexProviderSettings
from vnext_runtime.run_scope import RunScope


FULL_ACCESS_ARGS = (
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ephemeral",
    "-m",
    "gpt-5.6-luna",
    "-c",
    'model_reasoning_effort="low"',
)
WORKDIR = Path("X:/synthetic/c/candidate")
BODY = "Please inspect the exact current blocker and reply with the requested decision.\n"
MARKER = (
    '<!-- bridge-owner-notification: '
    '{"schema_version":2,"owner_action_id":"owner-action-001",'
    '"blocker_key":"synthetic-review-blocker",'
    '"transport":"worker-email","subject":"Synthetic service review"} -->'
)
COMMAND_TEXT = (
    MARKER
    + "\n"
    + notification.BODY_START
    + "\n"
    + BODY
    + notification.BODY_END
    + "\nVerify the blocker only; the Worker owns email delivery."
)


def _codex_result(**updates: object) -> CodexRunResult:
    values = {
        "exit_code": 0,
        "final_message": 'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
        "stdout_tail": "",
        "stderr_tail": "",
        "marker": {"status": "SUCCESS"},
        "launched_at": "2024-01-01T00:00:00+00:00",
        "final_marker_detected_at": "2024-01-01T00:00:02+00:00",
        "process_exited_at": "2024-01-01T00:00:02+00:00",
        "wrapper_pid": 123,
        "stdout_log_path": Path("stdout.log"),
        "stderr_log_path": Path("stderr.log"),
        "process_scope": "test",
        "contract_error": None,
        "runtime_error": None,
        "cleanup_error": None,
        "timed_out": False,
    }
    values.update(updates)
    return CodexRunResult(**values)  # type: ignore[arg-type]


def _request(command_text: str = COMMAND_TEXT) -> ExecutionRequest:
    return ExecutionRequest(
        project_id="example-service",
        command_id=1,
        run_id="run-001-0123456789ab",
        workdir=WORKDIR,
        mission="mission",
        command_text=command_text,
        kind="EXECUTE",
        profile=ExecutionProfile(),
    )


def _scope() -> RunScope:
    value = RunScope(RunIdentity("example-service", 1, "run-001-0123456789ab", 2))
    value.activate()
    return value


def _settings() -> CodexProviderSettings:
    return CodexProviderSettings(
        codex_command=sys.executable,
        codex_execution_mode="full_access",
        codex_args=FULL_ACCESS_ARGS,
        output_file=Path("run/final-message.txt"),
    )


class OwnerNotificationGatewayTests(unittest.TestCase):
    def test_parser_requires_schema_v2_identity_and_exact_body(self) -> None:
        spec = notification.parse_spec(COMMAND_TEXT)
        assert spec is not None
        self.assertEqual(spec.owner_action_id, "owner-action-001")
        self.assertEqual(
            spec.blocker_key,
            "synthetic-review-blocker",
        )
        self.assertEqual(spec.transport, "worker-email")
        self.assertEqual(spec.subject, "Synthetic service review")
        self.assertEqual(spec.body, BODY)

    def test_legacy_or_bodyless_marker_is_rejected(self) -> None:
        legacy = MARKER.replace('"schema_version":2', '"schema_version":1')
        with self.assertRaises(notification.OwnerNotificationError):
            notification.parse_spec(legacy + "\n" + notification.BODY_START + "\nx\n" + notification.BODY_END)
        with self.assertRaises(notification.OwnerNotificationError):
            notification.parse_spec(MARKER)

    def test_worker_gateway_persists_pending_before_send_then_sent_receipt(self) -> None:
        spec = notification.parse_spec(COMMAND_TEXT)
        assert spec is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed_state: list[str] = []

            def send(_settings: object, *, subject: str, body: str) -> None:
                path = notification.record_path("example-service", spec, bridge_root=root)
                record = json.loads(path.read_text(encoding="utf-8"))
                observed_state.append(record["state"])
                self.assertEqual(subject, "Synthetic service review")
                self.assertEqual(body, BODY)

            with patch.object(notification.bridge_alerts, "send_test_email", side_effect=send) as sender:
                assessment = notification.deliver(
                    project_id="example-service",
                    command_id=1,
                    run_id="run-001-0123456789ab",
                    spec=spec,
                    bridge_root=root,
                )

            self.assertEqual(sender.call_count, 1)
            self.assertEqual(observed_state, ["pending"])
            self.assertIsNone(assessment.error)
            assert assessment.receipt is not None
            self.assertEqual(assessment.receipt["state"], "SENT")
            self.assertEqual(
                assessment.receipt["evidence"]["source"],
                "worker-owned-smtp-gateway",
            )
            final_record = json.loads(
                notification.record_path("example-service", spec, bridge_root=root).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(final_record["state"], "sent")
            self.assertEqual(final_record["receipt"]["notification_id"], assessment.receipt["notification_id"])

    def test_existing_sent_record_dedupes_without_second_send(self) -> None:
        spec = notification.parse_spec(COMMAND_TEXT)
        assert spec is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(notification.bridge_alerts, "send_test_email") as sender:
                first = notification.deliver(
                    project_id="example-service",
                    command_id=1,
                    run_id="run-001-0123456789ab",
                    spec=spec,
                    bridge_root=root,
                )
                second = notification.deliver(
                    project_id="example-service",
                    command_id=2,
                    run_id="run-002-synthetic",
                    spec=spec,
                    bridge_root=root,
                )

            self.assertEqual(sender.call_count, 1)
            self.assertEqual(first.receipt, second.receipt)
            self.assertIsNone(second.error)

    def test_pending_or_failed_record_suppresses_resend(self) -> None:
        spec = notification.parse_spec(COMMAND_TEXT)
        assert spec is not None
        for state in ("pending", "failed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = notification.record_path("example-service", spec, bridge_root=root)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "notification_id": notification.notification_id("example-service", spec),
                            "state": state,
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.object(notification.bridge_alerts, "send_test_email") as sender:
                    assessment = notification.deliver(
                        project_id="example-service",
                        command_id=2,
                        run_id="run-002-synthetic",
                        spec=spec,
                        bridge_root=root,
                    )
                sender.assert_not_called()
                self.assertIsNone(assessment.receipt)
                self.assertIn("reconciliation required", assessment.error or "")

    def test_provider_sends_only_after_codex_semantic_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_result = _codex_result()
            captured_prompt: list[str] = []

            def runner(**kwargs: object) -> CodexRunResult:
                captured_prompt.append(str(kwargs["prompt"]))
                return raw_result

            provider = CodexExecutorProvider(_settings(), runner=runner)
            with (
                patch.object(notification, "_bridge_root", return_value=root),
                patch.object(notification.bridge_alerts, "send_test_email") as sender,
            ):
                result = provider.execute(_request(), _scope())

        self.assertEqual(result.outcome, ExecutionOutcome.SUCCESS)
        self.assertEqual(sender.call_count, 1)
        self.assertIn("Do not invoke an external mail sender", captured_prompt[0])
        receipt = getattr(raw_result, "owner_notification_receipt")
        self.assertEqual(receipt["source_command_id"], 1)
        self.assertEqual(receipt["source_run_id"], "run-001-0123456789ab")

    def test_provider_does_not_send_when_codex_blocks(self) -> None:
        raw_result = _codex_result(
            final_message='BRIDGE_EXECUTION_JSON: {"status":"BLOCKED"}',
            marker={"status": "BLOCKED", "reason": "blocker changed"},
        )
        provider = CodexExecutorProvider(_settings(), runner=Mock(return_value=raw_result))
        with patch.object(notification.bridge_alerts, "send_test_email") as sender:
            result = provider.execute(_request(), _scope())
        self.assertEqual(result.outcome, ExecutionOutcome.BLOCKED)
        sender.assert_not_called()

    def test_provider_fails_closed_when_worker_gateway_has_ambiguous_prior_attempt(self) -> None:
        spec = notification.parse_spec(COMMAND_TEXT)
        assert spec is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = notification.record_path("example-service", spec, bridge_root=root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"state":"pending"}\n', encoding="utf-8")
            raw_result = _codex_result()
            provider = CodexExecutorProvider(_settings(), runner=Mock(return_value=raw_result))
            with patch.object(notification, "_bridge_root", return_value=root):
                result = provider.execute(_request(), _scope())

        self.assertEqual(result.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(raw_result.marker["reason"], "OWNER_NOTIFICATION_RECEIPT_MISSING")
        self.assertIn("automatic resend is suppressed", raw_result.contract_error or "")
        self.assertIn(
            "reconciliation required",
            getattr(raw_result, "owner_notification_receipt_error"),
        )

    def test_worker_report_persists_gateway_receipt_after_codex_response(self) -> None:
        raw_result = _codex_result()
        receipt = {
            "schema_version": 2,
            "notification_id": "owner-notify-test",
            "project_id": "example-service",
            "owner_action_id": "owner-action-001",
            "blocker_key": "synthetic-review-blocker",
            "state": "SENT",
            "transport": "worker-email",
            "evidence_type": "provider_accepted",
            "provider_result": "accepted",
            "source_command_id": 1,
            "source_run_id": "run-001-0123456789ab",
            "provider_event_at": "2024-01-01T00:23:31+00:00",
            "evidence": {
                "source": "worker-owned-smtp-gateway",
                "binding": "immutable-command-marker+worker-pre-send-journal",
                "journal_path": "worker/runtime/owner-notifications/test.json",
                "journal_persisted_sent": True,
            },
        }
        setattr(raw_result, "owner_notification_receipt", receipt)
        setattr(raw_result, "owner_notification_receipt_error", None)

        report = report_builder.build_worker_report(
            project_id="example-service",
            command_id=1,
            outcome="SUCCESS",
            exit_code=0,
            workdir=WORKDIR,
            head_before="a",
            head_after="a",
            final_message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
            stderr="",
            meta={"source": "scheduled_chatgpt", "based_on_report": 1},
            run_id="run-001-0123456789ab",
            claim_generation=2,
            executor_profile={"source": "command_override"},
            completed_at="2024-01-01T00:23:31+00:00",
            worker_host="test-host",
            run_result=raw_result,
        )

        self.assertIn("## Worker owner-notification receipt", report)
        self.assertIn("BRIDGE_OWNER_NOTIFICATION_RECEIPT:", report)
        self.assertIn("worker-owned-smtp-gateway", report)


if __name__ == "__main__":
    unittest.main()
