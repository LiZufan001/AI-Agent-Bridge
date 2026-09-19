"""Focused tests for the one-focus Supervisor portfolio helper."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vnext_runtime.supervisor_portfolio import (
    CasProjectPublisher,
    CommandPlan,
    ContextIsolationError,
    IsolatedPlanningContextLoader,
    Level1Facts,
    PlanningEvidence,
    PortfolioPolicy,
    ProjectPlanningContext,
    ProjectSnapshot,
    PublicationResult,
    SchedulingClass,
    SupervisorPortfolioPass,
    derive_portfolio_snapshot,
    select_projects,
)


def snapshot(project_id: str, status: str = "REPORT_READY", **facts: object) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id,
        status,
        generation=7,
        latest_command=4,
        latest_report=3,
        facts=Level1Facts(**facts),
    )


def context(project_id: str) -> ProjectPlanningContext:
    return ProjectPlanningContext(
        project_id,
        Path(f"X:/synthetic/c/{project_id}"),
        {"status": "REPORT_READY", "generation": 7},
        f"mission-{project_id}",
        f"policy-{project_id}",
        reports=(PlanningEvidence(project_id, "report", "current report evidence"),),
    )


class FakePublisher:
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, ProjectSnapshot]] = []

    def publish(self, project_id: str, current: ProjectSnapshot, plan: CommandPlan) -> PublicationResult:
        self.calls.append((project_id, current))
        if project_id in self.failures:
            return PublicationResult(False, self.failures[project_id])
        return PublicationResult(True, "published")


class OneFocusPortfolioTests(unittest.TestCase):
    def test_many_projects_deep_load_exactly_one_focus(self) -> None:
        loaded: list[str] = []

        def loader(project_id: str) -> ProjectPlanningContext:
            loaded.append(project_id)
            return context(project_id)

        publisher = FakePublisher()
        runner = SupervisorPortfolioPass(
            context_loader=IsolatedPlanningContextLoader(loader),
            planner=lambda _ctx: CommandPlan("do one bounded unit"),
            publisher=publisher,
        )
        result = runner.run(
            tuple(snapshot(f"p-{i}") for i in range(10)),
            pass_id="pass-1",
            timestamp="2001-01-15T00:00:00Z",
        )
        self.assertEqual(loaded, ["p-0"])
        self.assertEqual(result.diagnostics.projects_scanned, 10)
        self.assertEqual(result.diagnostics.selected_project_ids, ("p-0",))
        self.assertEqual(result.diagnostics.publication_successes, ("p-0",))
        self.assertTrue(
            all(
                result.diagnostics.dispositions[f"p-{i}"] == "deferred_this_pass"
                for i in range(1, 10)
            )
        )

    def test_selection_is_one_focus_and_does_not_mutate_snapshots(self) -> None:
        items = tuple(snapshot(f"p-{i}") for i in range(6))
        decision = select_projects(items, PortfolioPolicy())
        self.assertEqual([item.project_id for item in decision.selected], ["p-0"])
        self.assertEqual(set(decision.deferred), {"p-1", "p-2", "p-3", "p-4", "p-5"})
        self.assertEqual([item.facts.fairness_deferrals for item in items], [0] * 6)
        self.assertEqual(decision.next_fairness_deferrals["p-0"], 0)
        self.assertEqual(decision.next_fairness_deferrals["p-1"], 1)

    def test_material_owner_feedback_outranks_polish(self) -> None:
        selected = select_projects(
            (
                snapshot("polish", discretionary_polish=True),
                snapshot("feedback", material_feedback_hint=True),
            )
        ).selected
        self.assertEqual([item.project_id for item in selected], ["feedback"])

    def test_fairness_breaks_ties_only_within_same_priority_band(self) -> None:
        selected = select_projects(
            (
                snapshot("new"),
                snapshot("aged", fairness_deferrals=2),
            )
        ).selected
        self.assertEqual([item.project_id for item in selected], ["aged"])

    def test_aged_polish_never_overrides_material_owner_blocker(self) -> None:
        selected = select_projects(
            (
                snapshot("polish", discretionary_polish=True, fairness_deferrals=20),
                snapshot("blocker", material_owner_blocker=True),
            )
        ).selected
        self.assertEqual([item.project_id for item in selected], ["blocker"])

    def test_nonordinary_states_are_accounted_but_not_replanned(self) -> None:
        statuses = {
            "CODEX_RUNNING": SchedulingClass.RUNNING,
            "COMMAND_READY": SchedulingClass.COMMAND_PENDING,
            "FINALIZING": SchedulingClass.COMMAND_PENDING,
            "HUMAN_REQUIRED": SchedulingClass.HUMAN_REQUIRED,
            "RECOVERY_REQUIRED": SchedulingClass.RECOVERY_OR_FAILED,
            "DONE": SchedulingClass.TERMINAL,
        }
        items = tuple(snapshot(name.lower(), status=status) for status, name in statuses.items())
        for item, expected in zip(items, statuses.values()):
            self.assertEqual(item.scheduling_class, expected)
        self.assertEqual(select_projects(items).selected, ())

    def test_focus_failure_does_not_fall_through_to_second_project(self) -> None:
        loaded: list[str] = []

        def loader(project_id: str) -> ProjectPlanningContext:
            loaded.append(project_id)
            if project_id == "missing":
                raise ContextIsolationError("missing evidence")
            return context(project_id)

        publisher = FakePublisher()
        runner = SupervisorPortfolioPass(
            context_loader=IsolatedPlanningContextLoader(loader),
            planner=lambda _ctx: CommandPlan("one command"),
            publisher=publisher,
        )
        result = runner.run(
            (snapshot("missing"), snapshot("safe")),
            pass_id="pass",
            timestamp="now",
        )
        self.assertEqual(loaded, ["missing"])
        self.assertEqual(publisher.calls, [])
        self.assertEqual(
            result.diagnostics.dispositions["missing"],
            "planning_context_isolated_or_invalid",
        )
        self.assertEqual(result.diagnostics.dispositions["safe"], "deferred_this_pass")

    def test_publication_race_is_project_local_and_does_not_trigger_second_focus(self) -> None:
        publisher = FakePublisher({"race": "cas_race"})
        runner = SupervisorPortfolioPass(
            context_loader=IsolatedPlanningContextLoader(context),
            planner=lambda _ctx: CommandPlan("one command"),
            publisher=publisher,
        )
        result = runner.run(
            (snapshot("race"), snapshot("safe")),
            pass_id="pass",
            timestamp="now",
        )
        self.assertEqual([item[0] for item in publisher.calls], ["race"])
        self.assertEqual(result.diagnostics.publication_successes, ())
        self.assertEqual(result.diagnostics.dispositions["race"], "cas_race")
        self.assertEqual(result.diagnostics.dispositions["safe"], "deferred_this_pass")

    def test_integrity_uncertainty_stops_publication(self) -> None:
        publisher = FakePublisher()
        runner = SupervisorPortfolioPass(
            context_loader=IsolatedPlanningContextLoader(context),
            planner=lambda _ctx: CommandPlan("one command"),
            publisher=publisher,
            integrity_check=lambda: False,
        )
        result = runner.run((snapshot("safe"),), pass_id="pass", timestamp="now")
        self.assertEqual(publisher.calls, [])
        self.assertEqual(result.diagnostics.result, "control_plane_integrity_uncertain")

    def test_large_accounting_snapshot_contains_no_body_context(self) -> None:
        states = {
            f"p-{i}": {
                "status": "REPORT_READY",
                "generation": i,
                "latest_command": 1,
                "latest_report": 1,
            }
            for i in range(500)
        }
        loaded: list[str] = []

        def state_loader(project_id: str, _config: object) -> dict[str, int | str]:
            loaded.append(project_id)
            return states[project_id]

        result = derive_portfolio_snapshot(
            {project_id: {"enabled": True} for project_id in states},
            state_loader,
        )
        self.assertEqual(len(result), 500)
        self.assertEqual(loaded, sorted(loaded))
        self.assertTrue(all(len(item.__slots__) == 7 for item in result))

    def test_context_evidence_cannot_cross_project(self) -> None:
        bad = ProjectPlanningContext(
            "b",
            Path("X:/synthetic/c/b"),
            {},
            "mission",
            "policy",
            reports=(PlanningEvidence("a", "report", "private evidence"),),
        )
        with self.assertRaises(ContextIsolationError):
            bad.validate_isolated("b")

    def test_supervisor_support_does_not_model_worker_resources(self) -> None:
        self.assertFalse(hasattr(ProjectSnapshot, "exclusive_paths"))
        self.assertFalse(hasattr(ProjectSnapshot, "git_target"))
        self.assertFalse(hasattr(ProjectSnapshot, "max_parallel_runs"))

    def test_diagnostics_are_bounded_and_noncanonical(self) -> None:
        publisher = FakePublisher({"p": "unexpected-secret-bearing-error"})
        runner = SupervisorPortfolioPass(
            context_loader=IsolatedPlanningContextLoader(context),
            planner=lambda _ctx: CommandPlan("one command"),
            publisher=publisher,
        )
        data = runner.run((snapshot("p"),), pass_id="pass", timestamp="now").diagnostics.to_dict()
        encoded = str(data)
        self.assertNotIn("unexpected-secret-bearing-error", encoded)
        self.assertNotIn("canonical_state", encoded)
        self.assertLess(len(encoded), 4096)

    def test_cas_publisher_revalidates_snapshot_and_renders_v2_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-one-focus-cas-") as temp:
            root = Path(temp)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            state_path = project / "state.json"
            current = {
                "protocol_version": 2,
                "status": "REPORT_READY",
                "generation": 7,
                "latest_command": 4,
                "latest_report": 3,
                "active_run": None,
            }
            captured: dict[str, object] = {}

            def fake_publish_cas(**kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                expected = kwargs["expected"]
                payload_builder = kwargs["payload_builder"]
                assert callable(expected)
                assert callable(payload_builder)
                self.assertTrue(expected(current))
                payloads = payload_builder(current)
                command_text = next(
                    value for path, value in payloads.items() if path.name == "command-005.md"
                )
                self.assertIn('"expected_generation":8', command_text)
                state = json.loads(payloads[state_path])
                self.assertEqual(state["status"], "COMMAND_READY")
                self.assertEqual(state["last_reviewed_report"], 3)
                return {**current, "status": "COMMAND_READY", "generation": 8, "latest_command": 5}

            with patch("vnext_runtime.supervisor_portfolio.git_store.publish_cas", fake_publish_cas):
                result = CasProjectPublisher(root).publish(
                    "p",
                    snapshot("p"),
                    CommandPlan("bounded command"),
                )
            self.assertTrue(result.published)
            self.assertEqual(captured["message"], "bridge: supervisor portfolio command p")

    def test_cas_publisher_maps_finalizer_plan_to_finalizing(self) -> None:
        current = {
            "protocol_version": 2,
            "status": "REPORT_READY",
            "generation": 7,
            "latest_command": 4,
            "latest_report": 3,
            "active_run": None,
        }

        def fake_publish_cas(**kwargs: object) -> dict[str, object]:
            builder = kwargs["payload_builder"]
            assert callable(builder)
            payloads = builder(current)
            state_payload = next(
                value for path, value in payloads.items() if path.name == "state.json"
            )
            state = json.loads(state_payload)
            self.assertEqual(state["last_reviewed_report"], 3)
            self.assertEqual(state["status"], "FINALIZING")
            return {**current, "status": "FINALIZING", "generation": 8, "latest_command": 5}

        with patch("vnext_runtime.supervisor_portfolio.git_store.publish_cas", fake_publish_cas):
            result = CasProjectPublisher(Path("X:/synthetic/c/disposable")).publish(
                "p",
                snapshot("p"),
                CommandPlan("finalize safely", kind="FINALIZE", source="finalizer"),
            )
        self.assertTrue(result.published)
        self.assertEqual(result.state["status"], "FINALIZING")


if __name__ == "__main__":
    unittest.main()
