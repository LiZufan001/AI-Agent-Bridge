import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge_common
import bridge_manual as bm
import bridge_worker as bw
import report_builder


COMPLETED_AT = "2001-01-15T08:00:00+08:00"
WORKER_HOST = "worker-fixture"
EXECUTOR_HOST = "manual-fixture"


def _worker_kwargs(**overrides):
    values = {
        "project_id": "demo",
        "command_id": 4,
        "outcome": "SUCCESS",
        "exit_code": 0,
        "workdir": Path("X:/synthetic/d/fixture/worktree"),
        "head_before": "a" * 40,
        "head_after": "b" * 40,
        "final_message": "Verified result.",
        "stderr": "",
        "meta": {"source": "scheduled_chatgpt", "based_on_report": 3},
        "run_id": "run-004-fixture",
        "claim_generation": 8,
        "executor_profile": {
            "source": "command_override",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        "completed_at": COMPLETED_AT,
        "worker_host": WORKER_HOST,
    }
    values.update(overrides)
    return values


def _lifecycle_result(**overrides):
    values = {
        "process_scope": "fixture_process_scope",
        "wrapper_pid": 1234,
        "codex_root_pid": 5678,
        "launched_at": "2001-01-15T07:59:00+08:00",
        "final_marker_detected_at": "2001-01-15T07:59:30+08:00",
        "marker": {"status": "SUCCESS"},
        "marker_result": "VALID",
        "process_exited_at": "2001-01-15T07:59:45+08:00",
        "forced_cleanup": False,
        "forced_cleanup_after_final": False,
        "timed_out": False,
        "termination_reason": None,
        "log_threshold_exceeded": True,
        "cleanup_error": "fixture cleanup warning",
        "stdout_log_path": Path("X:/synthetic/d/fixture/runs/stdout.log"),
        "stderr_log_path": Path("X:/synthetic/d/fixture/runs/stderr.log"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ReportBuilderTests(unittest.TestCase):
    def test_worker_success_report_has_exact_historical_structure(self):
        rendered = report_builder.build_worker_report(**_worker_kwargs())
        workdir = Path("X:/synthetic/d/fixture/worktree")
        expected = (
            "# Report 004 — demo\n\n"
            "- command_id: 4\n"
            "- outcome: SUCCESS\n"
            "- source: scheduled_chatgpt\n"
            "- based_on_report: 3\n"
            "- run_id: `run-004-fixture`\n"
            "- claim_generation: 8\n"
            "- executor_profile_source: command_override\n"
            "- requested_model: `gpt-5.6-sol`\n"
            "- requested_reasoning_effort: `high`\n"
            "- effective_cli_model: `gpt-5.6-sol`\n"
            "- effective_cli_reasoning_effort: `high`\n"
            "- codex_exit_code: 0\n"
            f"- completed_at: {COMPLETED_AT}\n"
            f"- worker_host: {WORKER_HOST}\n"
            f"- workdir: `{workdir}`\n"
            f"- git_head_before: `{'a' * 40}`\n"
            f"- git_head_after: `{'b' * 40}`\n\n"
            "## Codex final response\n\n"
            "Verified result.\n"
        )
        self.assertEqual(rendered, expected)

    def test_worker_failed_and_blocked_reports_include_diagnostics(self):
        for outcome in ("FAILED", "BLOCKED"):
            with self.subTest(outcome=outcome):
                rendered = report_builder.build_worker_report(
                    **_worker_kwargs(
                        outcome=outcome,
                        exit_code=9,
                        final_message="The final response was captured.",
                        stderr="diagnostic line\n",
                    )
                )
                self.assertEqual(
                    rendered.rsplit("\n## Worker diagnostics\n\n", 1)[1],
                    "```text\ndiagnostic line\n```\n",
                )

    def test_nonzero_exit_includes_diagnostics_even_for_success_marker(self):
        rendered = report_builder.build_worker_report(
            **_worker_kwargs(exit_code=17, stderr="nonzero diagnostic")
        )
        self.assertIn(
            "\n## Worker diagnostics\n\n```text\nnonzero diagnostic\n```\n",
            rendered,
        )

    def test_empty_final_message_uses_worker_fallback_and_empty_stderr(self):
        rendered = report_builder.build_worker_report(
            **_worker_kwargs(final_message="", stderr="")
        )
        self.assertIn(
            "## Codex final response\n\n(No final Codex message was captured.)\n",
            rendered,
        )
        self.assertIn(
            "## Worker diagnostics\n\n```text\n(no stderr)\n```\n",
            rendered,
        )

    def test_lifecycle_result_adds_all_existing_lifecycle_fields(self):
        rendered = report_builder.build_worker_report(
            **_worker_kwargs(run_result=_lifecycle_result())
        )
        expected_fields = (
            "- process_scope: fixture_process_scope",
            "- wrapper_pid: 1234",
            "- codex_root_pid: 5678",
            "- process_launched_at: 2001-01-15T07:59:00+08:00",
            "- final_marker_detected_at: 2001-01-15T07:59:30+08:00",
            "- marker_status: SUCCESS",
            "- marker_result: VALID",
            "- process_exited_at: 2001-01-15T07:59:45+08:00",
            "- forced_cleanup: false",
            "- forced_cleanup_after_final: false",
            "- execution_timed_out: false",
            "- termination_reason: N/A",
            "- log_threshold_exceeded: true",
            "- runtime_cleanup_error: fixture cleanup warning",
            f"- stdout_log: `{Path('X:/synthetic/d/fixture/runs/stdout.log')}`",
            f"- stderr_log: `{Path('X:/synthetic/d/fixture/runs/stderr.log')}`",
        )
        for field in expected_fields:
            self.assertIn(field, rendered)

    def test_missing_lifecycle_result_omits_lifecycle_fields(self):
        rendered = report_builder.build_worker_report(**_worker_kwargs())
        for field in (
            "process_scope:",
            "wrapper_pid:",
            "codex_root_pid:",
            "process_launched_at:",
            "final_marker_detected_at:",
            "marker_status:",
            "marker_result:",
            "process_exited_at:",
            "forced_cleanup:",
            "execution_timed_out:",
            "stdout_log:",
            "stderr_log:",
        ):
            self.assertNotIn(field, rendered)

    def test_forced_cleanup_adds_diagnostics_even_after_success(self):
        rendered = report_builder.build_worker_report(
            **_worker_kwargs(
                run_result=_lifecycle_result(
                    forced_cleanup=True,
                    forced_cleanup_after_final=True,
                ),
                stderr="forced cleanup diagnostic",
            )
        )
        self.assertIn(
            "\n## Worker diagnostics\n\n```text\nforced cleanup diagnostic\n```\n",
            rendered,
        )

    def test_stderr_uses_only_the_existing_bounded_tail(self):
        stderr = "TRUNCATED_SENTINEL\n" + ("x" * 5000) + "TAIL"
        rendered = report_builder.build_worker_report(
            **_worker_kwargs(outcome="FAILED", exit_code=1, stderr=stderr)
        )
        expected_tail = stderr[-report_builder.DIAGNOSTIC_TAIL_CHARS :].strip()
        expected = (
            "```text\n"
            + report_builder.redact_diagnostics(expected_tail)
            + "\n```\n"
        )
        self.assertTrue(rendered.endswith(expected))
        self.assertNotIn("TRUNCATED_SENTINEL", rendered)

    def test_redaction_patterns_match_existing_worker_behavior(self):
        diagnostic = (
            "Authorization: Bearer bearer-secret\n"
            "Authorization: plain-secret\n"
            "Cookie: session=cookie-secret\n"
            "api-key=api-secret token:token-secret password = password-secret "
            "secret:secret-secret\n"
            "sk-1234567890 ghp_1234567890 github_pat_1234567890 "
            "xoxb-1234567890"
        )
        expected = (
            "Authorization: Bearer [REDACTED]\n"
            "Authorization: [REDACTED]\n"
            "Cookie: [REDACTED]\n"
            "api-key=[REDACTED] token:[REDACTED] password = [REDACTED] "
            "secret:[REDACTED]\n"
            "[REDACTED_TOKEN] [REDACTED_TOKEN] [REDACTED_TOKEN] "
            "[REDACTED_TOKEN]"
        )
        self.assertEqual(report_builder.redact_diagnostics(diagnostic), expected)

    def test_final_response_and_diagnostics_are_both_redacted(self):
        rendered = report_builder.build_worker_report(
            **_worker_kwargs(
                outcome="FAILED",
                exit_code=1,
                final_message="Authorization: Bearer final-secret",
                stderr="Cookie: session=diagnostic-secret",
            )
        )
        self.assertNotIn("final-secret", rendered)
        self.assertNotIn("diagnostic-secret", rendered)
        self.assertIn("Authorization: Bearer [REDACTED]", rendered)
        self.assertIn("Cookie: [REDACTED]", rendered)

    def test_manual_report_has_exact_fields_and_preserves_final_response_behavior(self):
        active_run = {
            "source": "manual_chatgpt",
            "kind": "FINALIZE",
            "based_on_report": 12,
            "run_id": "manual-run-013",
            "claimed_generation": 14,
        }
        rendered = report_builder.build_manual_report(
            project_id="demo",
            command_id=13,
            outcome="SUCCESS",
            final_message="Manual result.  \n",
            active_run=active_run,
            completed_at=COMPLETED_AT,
            executor_host=EXECUTOR_HOST,
        )
        expected = (
            "# Report 013 — demo\n\n"
            "- command_id: 13\n"
            "- outcome: SUCCESS\n"
            "- source: manual_chatgpt\n"
            "- kind: FINALIZE\n"
            "- based_on_report: 12\n"
            "- run_id: `manual-run-013`\n"
            "- claim_generation: 14\n"
            f"- completed_at: {COMPLETED_AT}\n"
            f"- executor_host: {EXECUTOR_HOST}\n\n"
            "## Codex final response\n\n"
            "Manual result.\n"
        )
        self.assertEqual(rendered, expected)
        self.assertIn("Manual result.", rendered)

    def test_manual_report_defaults_and_empty_message_fallback(self):
        rendered = report_builder.build_manual_report(
            project_id="demo",
            command_id=4,
            outcome="FAILED",
            final_message="",
            active_run={},
            completed_at=COMPLETED_AT,
            executor_host=EXECUTOR_HOST,
        )
        self.assertIn("- source: manual_chatgpt\n", rendered)
        self.assertIn("- kind: EXECUTE\n", rendered)
        self.assertIn("- run_id: `unknown`\n", rendered)
        self.assertIn("- claim_generation: unknown\n", rendered)
        self.assertTrue(rendered.endswith("(No final Codex message was supplied.)\n"))

    def test_manual_final_response_redacts_credentials(self):
        rendered = report_builder.build_manual_report(
            project_id="demo",
            command_id=4,
            outcome="SUCCESS",
            final_message="Authorization: Bearer manual-secret",
            active_run={},
            completed_at=COMPLETED_AT,
            executor_host=EXECUTOR_HOST,
        )
        # CP05: the old assertion enshrined a credential leak, not safe compatibility.
        self.assertNotIn("manual-secret", rendered)
        self.assertIn("Authorization: Bearer [REDACTED]", rendered)

    def test_legacy_worker_and_manual_wrappers_delegate_with_runtime_values(self):
        worker_args = _worker_kwargs()
        with patch.object(bw, "now_iso", return_value=COMPLETED_AT), patch.object(
            bw.platform, "node", return_value=WORKER_HOST
        ):
            worker_rendered = bw.report_markdown(
                project_id=worker_args["project_id"],
                command_id=worker_args["command_id"],
                outcome=worker_args["outcome"],
                exit_code=worker_args["exit_code"],
                workdir=worker_args["workdir"],
                head_before=worker_args["head_before"],
                head_after=worker_args["head_after"],
                final_message=worker_args["final_message"],
                stderr=worker_args["stderr"],
                meta=worker_args["meta"],
                run_id=worker_args["run_id"],
                claim_generation=worker_args["claim_generation"],
                executor_profile=worker_args["executor_profile"],
            )
        self.assertEqual(worker_rendered, report_builder.build_worker_report(**worker_args))
        self.assertEqual(
            bw.redact_diagnostics("Authorization: Bearer wrapper-secret"),
            report_builder.redact_diagnostics(
                "Authorization: Bearer wrapper-secret"
            ),
        )

        active_run = {
            "source": "manual_chatgpt",
            "kind": "EXECUTE",
            "based_on_report": 3,
            "run_id": "manual-run-004",
            "claimed_generation": 8,
        }
        manual_args = {
            "project_id": "demo",
            "command_id": 4,
            "outcome": "SUCCESS",
            "final_message": "Manual result.",
            "active_run": active_run,
        }
        with patch.object(bm.common, "now_iso", return_value=COMPLETED_AT), patch.object(
            bm.platform, "node", return_value=EXECUTOR_HOST
        ):
            manual_rendered = bm.manual_report_markdown(**manual_args)
        self.assertEqual(
            manual_rendered,
            report_builder.build_manual_report(
                **manual_args,
                completed_at=COMPLETED_AT,
                executor_host=EXECUTOR_HOST,
            ),
        )

    def test_renderers_do_not_create_files_or_other_local_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before = sorted(root.rglob("*"))
            report_builder.build_worker_report(
                **_worker_kwargs(workdir=root / "worktree")
            )
            report_builder.build_manual_report(
                project_id="demo",
                command_id=4,
                outcome="SUCCESS",
                final_message="Manual result.",
                active_run={},
                completed_at=COMPLETED_AT,
                executor_host=EXECUTOR_HOST,
            )
            self.assertEqual(sorted(root.rglob("*")), before)

    def test_pending_report_saves_the_already_rendered_text_unchanged(self):
        rendered = report_builder.build_worker_report(**_worker_kwargs())
        with tempfile.TemporaryDirectory() as temp:
            runtime_dir = Path(temp) / "runtime" / "demo"
            bridge_common.save_pending_report(
                runtime_dir,
                4,
                rendered,
                "publication failed",
            )
            self.assertEqual(
                (runtime_dir / "pending-report-004.md").read_text(encoding="utf-8"),
                rendered,
            )


if __name__ == "__main__":
    unittest.main()
