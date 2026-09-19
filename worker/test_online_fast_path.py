"""Focused integration checks for the current Scheduled Supervisor publication path."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "supervisor"))
import worker_execution_control as control
import test_supervisor_publication_gateway as fixtures
from vnext_runtime.supervisor_portfolio import (
    CommandPlan,
    ContextIsolationError,
    IsolatedPlanningContextLoader,
    PlanningEvidence,
    ProjectPlanningContext,
    ProjectSnapshot,
    PublicationResult,
    SupervisorPortfolioPass,
)

AUTO = control.ExecutionControlDecision(
    control.AUTO,
    "owner_resume",
    "owner",
    "2026-01-01T00:00:00Z",
    True,
    "fixture",
)


class OnlineFastPathTests(unittest.TestCase):
    def context(self, root: Path) -> ProjectPlanningContext:
        return ProjectPlanningContext(
            "p",
            root / "projects/p",
            {},
            "mission",
            "policy",
            reports=(PlanningEvidence("p", "report", "Previous run complete; next criterion remains."),),
            goals=(PlanningEvidence("p", "goal", "ACTIVE: return the missing value."),),
            sources=(PlanningEvidence("p", "source", "def result(): pass"),),
        )

    def test_cold_intent_reaches_real_staged_gateway_without_auxiliary_control_plane(self):
        f = fixtures.SupervisorPublicationGatewayIntegrationTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        context = replace(self.context(f.worker), canonical_state=f._state())
        snapshot = ProjectSnapshot.from_state("p", f._state())

        class StagedFixturePublisher:
            def publish(self, project_id, current, plan):
                f._stage_request("one-focus", command=f._command(body=plan.body))
                result = f._gateway().poll()[0]
                return PublicationResult(result.outcome == "published", result.reason)

        def planner(ctx):
            self.assertIn("ACTIVE", ctx.goals[0].body)
            self.assertIn("pass", ctx.sources[0].body)
            return CommandPlan("Implement result() return value; verify with focused unit test.")

        with patch.object(control, "read_execution_control", return_value=AUTO):
            runner = SupervisorPortfolioPass(
                context_loader=IsolatedPlanningContextLoader(lambda _: context),
                planner=planner,
                publisher=StagedFixturePublisher(),
            )
            result = runner.run((snapshot,), pass_id="one-focus", timestamp="fixture")

        self.assertEqual(result.diagnostics.publication_successes, ("p",))
        self.assertEqual(f._state()["status"], "COMMAND_READY")
        self.assertIsNone(f._state()["active_run"])

    def test_owner_paused_and_unreadable_block_gateway(self):
        for decision in (
            replace(AUTO, execution_mode=control.PAUSED, reason="owner_manual_pause"),
            control._fail_closed("unreadable"),
        ):
            with self.subTest(reason=decision.reason):
                f = fixtures.SupervisorPublicationGatewayIntegrationTests()
                f.setUp()
                try:
                    f._stage_request("owner-blocked")
                    before = f._blob("projects/p/state.json")
                    with patch.object(control, "read_execution_control", return_value=decision):
                        result = f._gateway().poll()[0]
                    self.assertEqual(result.reason, "owner_execution_blocked")
                    self.assertEqual(before, f._blob("projects/p/state.json"))
                finally:
                    f.tearDown()

    def test_current_generation_is_rejected_because_publication_requires_g_plus_one(self):
        f = fixtures.SupervisorPublicationGatewayIntegrationTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        f._init_project(f.worker, "p", generation=35, latest_command=13, latest_report=13)
        f._commit_push(f.worker, "projects/p/state.json", "fixture generation 35")
        f._stage_request(
            "wrong-generation",
            command_id=14,
            based_on_report=13,
            expected_generation=35,
        )
        before = f._blob("projects/p/state.json")
        with patch.object(control, "read_execution_control", return_value=AUTO):
            result = f._gateway().poll()[0]
        self.assertEqual(result.reason, "wrong_generation")
        self.assertEqual(f._blob("projects/p/state.json"), before)
        self.assertFalse((f.worker / "projects/p/commands/command-014.md").exists())

    def test_context_isolation_rejects_cross_project_evidence(self):
        ctx = self.context(Path.cwd())
        bad = replace(ctx, sources=(PlanningEvidence("other", "source", "wrong"),))
        with self.assertRaises(ContextIsolationError):
            IsolatedPlanningContextLoader(lambda _: bad).load("p")

    def test_prompt_encodes_current_positive_architecture_only(self):
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "docs/automation/bridge-portfolio-supervisor-current-prompt.md").read_text(
            encoding="utf-8-sig"
        )
        for required in (
            "ROUND-ROBIN FOCUS:",
            "NON-FOCUS PROJECTS:",
            "WORKER AVAILABILITY:",
            "STAGED GENERATION CONTRACT:",
            "expected_generation = G + 1",
            "deferred-this-pass",
        ):
            self.assertIn(required, prompt)
        for retired in (
            "CLAIMED",
            "review-ledger",
            "LEGACY REVIEW",
            "LEGACY SHADOW",
            "12 body files",
            "256 KiB",
            "pass_deadline=pass_start+900s",
            "shadow_only",
            "bridge-shadow-transport",
        ):
            self.assertNotIn(retired, prompt)


if __name__ == "__main__":
    unittest.main()
