"""Focused tests for command-scoped Codex Fast request configuration."""

from __future__ import annotations

import unittest
from pathlib import Path

import bridge_worker as bw
import executor
import protocol_core as pc
import report_builder


DEFAULTS_WITH_TIER = [
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ephemeral",
    "-m",
    "gpt-5.6-luna",
    "-c",
    'model_reasoning_effort="high"',
    "-c",
    'service_tier="default"',
    "-c",
    "features.example=true",
]


def metadata(**executor_values: object) -> dict[str, object]:
    return {
        "command_id": 2,
        "source": "manual_chatgpt",
        "based_on_report": 1,
        "expected_generation": 3,
        "kind": "EXECUTE",
        "executor": executor_values,
    }


class FastMetadataTests(unittest.TestCase):
    def test_fast_is_validated_as_a_narrow_command_property(self) -> None:
        parsed = bw.command_executor_override(
            metadata(
                model="gpt-5.6-luna",
                reasoning_effort="max",
                service_tier="fast",
            )
        )
        self.assertEqual(
            parsed,
            {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "service_tier": "fast",
            },
        )

    def test_invalid_service_tiers_and_types_fail_closed(self) -> None:
        for value in ("", "priority", "flex", "fast --config evil=true", True, [], {"value": "fast"}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(bw.WorkerError):
                    bw.command_executor_override(
                        metadata(
                            model="gpt-5.6-luna",
                            reasoning_effort="max",
                            service_tier=value,
                        )
                    )

    def test_missing_service_tier_remains_backward_compatible(self) -> None:
        old = {
            "command_id": 2,
            "source": "scheduled_chatgpt",
            "based_on_report": 1,
            "expected_generation": 3,
            "kind": "EXECUTE",
            "executor": {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
        }
        self.assertEqual(
            pc.command_executor_override(old),
            {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
        )


class FastArgvTests(unittest.TestCase):
    def test_fast_maps_to_run_local_config_and_keeps_max_reasoning(self) -> None:
        effective, profile = executor.effective_codex_args(
            DEFAULTS_WITH_TIER,
            {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "service_tier": "fast",
            },
        )
        self.assertEqual(effective.count("-m"), 1)
        self.assertEqual(effective[effective.index("-m") + 1], "gpt-5.6-luna")
        self.assertEqual(
            [arg for arg in effective if arg.startswith("service_tier=")],
            ['service_tier="fast"'],
        )
        self.assertNotIn('service_tier="default"', effective)
        self.assertIn('model_reasoning_effort="max"', effective)
        self.assertEqual(profile["service_tier"], "fast")
        self.assertEqual(profile["reasoning_effort"], "max")

    def test_override_without_tier_preserves_existing_default_tier(self) -> None:
        effective, profile = executor.effective_codex_args(
            DEFAULTS_WITH_TIER,
            {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        )
        self.assertIn('service_tier="default"', effective)
        self.assertEqual(profile["service_tier"], "default")
        self.assertEqual(profile["model"], "gpt-5.6-sol")

    def test_no_tier_run_does_not_leak_fast_to_next_run(self) -> None:
        original = list(DEFAULTS_WITH_TIER)
        fast, _ = executor.effective_codex_args(
            DEFAULTS_WITH_TIER,
            {"model": "gpt-5.6-luna", "reasoning_effort": "max", "service_tier": "fast"},
        )
        ordinary, ordinary_profile = executor.effective_codex_args(
            DEFAULTS_WITH_TIER, None
        )
        self.assertIn('service_tier="fast"', fast)
        self.assertEqual(ordinary, original)
        self.assertNotIn('service_tier="fast"', ordinary)
        self.assertEqual(ordinary_profile["service_tier"], "default")
        self.assertEqual(DEFAULTS_WITH_TIER, original)

    def test_profile_parser_handles_config_equals_forms(self) -> None:
        profile = executor.executor_profile_from_args(
            ["-c=service_tier=fast", "--config", 'service_tier="default"'],
            source="worker_default",
        )
        self.assertEqual(profile["service_tier"], "default")


class FastReportTests(unittest.TestCase):
    def test_report_distinguishes_requested_and_effective_cli_tier(self) -> None:
        rendered = report_builder.build_worker_report(
            project_id="p",
            command_id=2,
            outcome="SUCCESS",
            exit_code=0,
            workdir=Path("X:/synthetic/d/candidate"),
            head_before="a" * 40,
            head_after="b" * 40,
            final_message="Verified.",
            stderr="",
            meta={"source": "manual_chatgpt", "based_on_report": 1},
            run_id="run-002-fast",
            claim_generation=4,
            executor_profile={
                "source": "command_override",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "service_tier": "fast",
            },
            completed_at="2024-01-01T00:00:00Z",
            worker_host="worker",
        )
        self.assertIn("- requested_service_tier: `fast`", rendered)
        self.assertIn("- effective_cli_service_tier: `fast`", rendered)
        self.assertIn(
            "- service_tier_observation: request configuration only; server-served tier not observed",
            rendered,
        )

    def test_legacy_profile_without_tier_has_no_false_server_claim(self) -> None:
        rendered = report_builder.build_worker_report(
            project_id="p",
            command_id=4,
            outcome="SUCCESS",
            exit_code=0,
            workdir=Path("X:/synthetic/d/candidate"),
            head_before=None,
            head_after=None,
            final_message="Verified.",
            stderr="",
            meta={"source": "scheduled_chatgpt", "based_on_report": 3},
            run_id="run-004",
            claim_generation=8,
            executor_profile={
                "source": "worker_default",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
            },
            completed_at="now",
            worker_host="worker",
        )
        self.assertNotIn("service_tier_observation", rendered)
        self.assertNotIn("served Fast", rendered)


if __name__ == "__main__":
    unittest.main()
