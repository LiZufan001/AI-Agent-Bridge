import unittest
from pathlib import Path

import phased_task


ROOT = Path(__file__).resolve().parents[1]


class SupervisorPolicyCharacterizationTests(unittest.TestCase):
    def test_generic_policy_keeps_clean_role_and_one_focus_boundary(self):
        policy = (ROOT / "policies" / "supervisor.md").read_text(encoding="utf-8")
        for required in (
            "one focus project",
            "Supervisor decides **what should happen next**",
            "Worker: local admission",
            "Codex Executor",
            "Cloud-to-local publication boundary",
            "expected_generation = G + 1",
            "Do not split tightly coupled",
            "planning heuristic, not a protocol constant",
        ):
            with self.subTest(required=required):
                self.assertIn(required, policy)
        for retired in (
            "max_plans_per_pass",
            "max_ready_buffer",
            "incremental-review",
            "review ledger",
            "CLAIMED",
        ):
            self.assertNotIn(retired, policy)

    def test_attention_policy_is_round_robin_not_priority_first(self):
        attention = (ROOT / "policies" / "portfolio-attention.md").read_text(
            encoding="utf-8"
        )
        entrypoint = (ROOT / "SUPERVISOR_ENTRYPOINT.md").read_text(encoding="utf-8")
        prompt = (
            ROOT / "docs" / "automation" / "bridge-portfolio-supervisor-current-prompt.md"
        ).read_text(encoding="utf-8")
        portfolio = (ROOT / "tests/fixtures/synthetic-state/supervisor/portfolio.json").read_text(encoding="utf-8")
        example_planner = (
            ROOT / "tests/fixtures/synthetic-state/projects" / "example-planner" / "SUPERVISOR.md"
        ).read_text(encoding="utf-8")

        for text in (attention, entrypoint, prompt, portfolio):
            self.assertIn("priority_rank", text)
        self.assertIn("round-robin first", attention)
        self.assertIn("rotation_bucket", attention)
        self.assertIn("ROUND-ROBIN FOCUS", prompt)
        self.assertNotIn("rotation_rank", attention)
        self.assertNotIn("rotation_rank", entrypoint)
        self.assertNotIn("rotation_rank", prompt)
        self.assertNotIn("rotation_rank", portfolio)
        self.assertIn("stable ring order", attention)
        self.assertIn("not ordinary work priority", prompt)
        self.assertNotIn("highest-priority autonomous development project", example_planner)
        self.assertNotIn("prefer useful ExamplePlanner progress ahead", example_planner)

    def test_resolved_human_required_reenters_ordinary_rotation(self):
        attention = (ROOT / "policies" / "portfolio-attention.md").read_text(
            encoding="utf-8"
        )
        entrypoint = (ROOT / "SUPERVISOR_ENTRYPOINT.md").read_text(encoding="utf-8")

        for required in (
            "newest linked owner-action event/status",
            "`OWNER_REPORTED_DONE + PENDING` is no longer blocked on the Owner",
            "`OWNER_REPORTED_DONE + VERIFIED` is no longer blocked on the Owner",
            "ordinary eligible resume/reconciliation candidate",
            "Do not invent a new state",
        ):
            with self.subTest(required=required):
                self.assertIn(required, attention)

        self.assertIn("bounded owner-action freshness check", entrypoint)
        self.assertIn("Canonical `HUMAN_REQUIRED` by itself is not enough", entrypoint)
        self.assertIn("`HUMAN_REQUIRED -> COMMAND_READY` resume", entrypoint)

    def test_generic_policy_characterizes_luna_bug_escalation(self):
        policy = (ROOT / "policies" / "supervisor.md").read_text(encoding="utf-8")
        for required in (
            "Luna High",
            "model = gpt-5.6-luna",
            "reasoning_effort = high",
            "Luna Max",
            "reasoning_effort=max",
            "unresolved root cause",
            "production",
            "device",
            "time-to-correct-resolution",
            "Max does not imply larger scope",
        ):
            with self.subTest(required=required):
                self.assertIn(required, policy)
        self.assertNotIn("gpt-5.6-luna-max", policy)

    def test_synthetic_service_policy_characterizes_neutral_escalation(self):
        supervisor = (
            ROOT / "tests/fixtures/synthetic-state/projects" / "example-device" / "SUPERVISOR.md"
        ).read_text(encoding="utf-8")
        for required in (
            "synthetic service-only",
            "scheduler startup",
            "QueueCoordinator",
            "EventRouter",
            "TimerService",
            "synthetic maximum tier",
            "reviewer verification",
            "demo bundle refresh",
            "runtime evidence",
            "deterministic regression",
            "high tier",
        ):
            with self.subTest(required=required):
                self.assertIn(required, supervisor)

    def test_synthetic_service_declaration_example_is_accepted_by_worker_parser(self):
        supervisor = (
            ROOT / "tests/fixtures/synthetic-state/projects" / "example-device" / "SUPERVISOR.md"
        ).read_text(encoding="utf-8")
        declaration = (
            '<!-- bridge-phased-task: {"schema_version":1,"phases":["A","B"]} -->'
        )
        self.assertIn(declaration, supervisor)
        self.assertEqual(phased_task.parse_declaration(declaration), ("A", "B"))
        self.assertEqual(len(phased_task.PHASE_IDS), 4)
        self.assertIn("Do not publish command N+1 in advance", supervisor)
        self.assertIn("HUMAN_REQUIRED", supervisor)


if __name__ == "__main__":
    unittest.main()
