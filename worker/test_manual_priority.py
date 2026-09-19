"""Focused Protocol-v2 tests for ordinary Supervisor manual priority."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
import protocol_core as pc
import vnext_runtime.supervisor_portfolio as sp
from codex_lifecycle import CodexRunResult


def _state(
    *,
    status: str = "COMMAND_READY",
    generation: int = 7,
    latest_command: int = 4,
    latest_report: int = 3,
    active_run: object = None,
) -> dict[str, object]:
    return {
        "protocol_version": 2,
        "project_id": "p",
        "status": status,
        "generation": generation,
        "latest_command": latest_command,
        "latest_report": latest_report,
        "last_reviewed_report": latest_report,
        "active_run": active_run,
    }


def _scheduled_command(
    *,
    command_id: int = 4,
    generation: int = 7,
    latest_report: int = 3,
    source: str = "scheduled_chatgpt",
    kind: str = "EXECUTE",
    extra: dict[str, object] | None = None,
) -> str:
    meta: dict[str, object] = {
        "command_id": command_id,
        "source": source,
        "based_on_report": latest_report,
        "expected_generation": generation,
        "kind": kind,
    }
    if extra:
        meta.update(extra)
    return (
        "<!-- bridge-command: "
        + json.dumps(meta, separators=(",", ":"))
        + " -->\n"
        + "scheduled body\n"
    )


class ManualPriorityTests(unittest.TestCase):
    def _project(self, root: Path, state: dict[str, object] | None = None) -> tuple[Path, str]:
        project = root / "projects" / "p"
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir()
        current = state or _state()
        (project / "state.json").write_text(
            json.dumps(current, indent=2) + "\n", encoding="utf-8"
        )
        old = _scheduled_command(
            command_id=int(current["latest_command"]),
            generation=int(current["generation"]),
            latest_report=int(current["latest_report"]),
        )
        (project / "commands" / f"command-{int(current['latest_command']):03d}.md").write_text(
            old, encoding="utf-8"
        )
        return project, old

    @staticmethod
    def _snapshot(state: dict[str, object]) -> sp.ProjectSnapshot:
        return sp.ProjectSnapshot.from_state("p", state)

    @staticmethod
    def _manual_plan(request_id: str | None = "req-1", **updates: object) -> sp.CommandPlan:
        values: dict[str, object] = {
            "body": "manual body",
            "source": "manual_chatgpt",
            "kind": "EXECUTE",
            "manual_request_id": request_id,
        }
        values.update(updates)
        return sp.CommandPlan(**values)  # type: ignore[arg-type]

    @staticmethod
    def _fake_publish(state_path: Path):
        def publish(**kwargs: object) -> dict[str, object]:
            current = json.loads(state_path.read_text(encoding="utf-8"))
            already = kwargs["already_applied"]
            expected = kwargs["expected"]
            payload_builder = kwargs["payload_builder"]
            assert callable(already)
            assert callable(expected)
            assert callable(payload_builder)
            if already(current):
                return current
            if not expected(current):
                raise sp.git_store.CASConflict("test CAS race")
            payloads = payload_builder(current)
            for path, text in payloads.items():
                assert isinstance(path, Path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return json.loads(state_path.read_text(encoding="utf-8"))

        return publish

    def test_unclaimed_scheduled_command_is_superseded_and_worker_claims_new_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, old_text = self._project(root)
            state = json.loads((project / "state.json").read_text(encoding="utf-8"))
            snapshot = self._snapshot(state)
            publisher = sp.CasProjectPublisher(root)
            with patch.object(sp.git_store, "publish_cas", side_effect=self._fake_publish(project / "state.json")):
                result = publisher.publish(
                    "p", snapshot, self._manual_plan("req-1")
                )
            self.assertTrue(result.published)
            self.assertEqual(result.reason, "published")
            after = json.loads((project / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(after["status"], "COMMAND_READY")
            self.assertEqual(after["generation"], 8)
            self.assertEqual(after["latest_command"], 5)
            self.assertIsNone(after["active_run"])
            self.assertEqual(
                (project / "commands" / "command-004.md").read_text(encoding="utf-8"),
                old_text,
            )
            new_text = (project / "commands" / "command-005.md").read_text(encoding="utf-8")
            new_meta = pc.parse_command_metadata(new_text)
            self.assertEqual(new_meta["source"], "manual_chatgpt")
            self.assertEqual(new_meta["kind"], "EXECUTE")
            self.assertEqual(new_meta["supersedes_command_id"], 4)
            self.assertEqual(new_meta["expected_generation"], 8)

            (project / "MISSION.md").write_text("mission", encoding="utf-8")
            calls: list[dict[str, object]] = []
            worker_state_path = project / "state.json"

            def worker_publish(**kwargs: object) -> dict[str, object]:
                current = json.loads(worker_state_path.read_text(encoding="utf-8"))
                if not kwargs["expected"](current):
                    raise bw.CASConflict("stale Worker claim")
                payloads = kwargs["payload_builder"](current)
                calls.append(payloads)
                for path, text in payloads.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                return json.loads(worker_state_path.read_text(encoding="utf-8"))

            run_result = CodexRunResult(
                exit_code=0,
                final_message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
                stdout_tail="",
                stderr_tail="",
                marker={"status": "SUCCESS"},
                launched_at="2001-01-15T00:00:00+08:00",
                final_marker_detected_at="2001-01-15T00:00:01+08:00",
                process_exited_at="2001-01-15T00:00:02+08:00",
                wrapper_pid=123,
                stdout_log_path=root / "stdout.log",
                stderr_log_path=root / "stderr.log",
                process_scope="test",
                marker_result="VALID",
            )
            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": ["exec", bw.FULL_ACCESS_FLAG],
                "network_guard": {"enabled": False},
            }
            with patch.object(bw, "publish_cas", side_effect=worker_publish), patch.object(
                bw, "get_head", return_value="head"
            ), patch.object(bw, "codex_run", return_value=run_result) as codex:
                self.assertTrue(
                    bw.process_project(root, "p", {"workdir": "__BRIDGE_ROOT__"}, config)
                )
            self.assertEqual(codex.call_count, 1)
            self.assertEqual(len(calls), 2)
            final_state = json.loads(worker_state_path.read_text(encoding="utf-8"))
            self.assertEqual(final_state["status"], "REPORT_READY")
            self.assertEqual(final_state["latest_report"], 5)
            self.assertIsNone(final_state["active_run"])
            self.assertEqual(
                (project / "commands" / "command-004.md").read_text(encoding="utf-8"),
                old_text,
            )

    def test_worker_claim_first_wins_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, old_text = self._project(root)
            state_path = project / "state.json"
            snapshot = self._snapshot(json.loads(state_path.read_text(encoding="utf-8")))
            running = _state(
                status="CODEX_RUNNING",
                generation=8,
                active_run={
                    "run_id": "run-004",
                    "command_id": 4,
                    "claimed_generation": 8,
                },
            )

            def raced_publish(**kwargs: object) -> dict[str, object]:
                current = dict(running)
                self.assertFalse(kwargs["expected"](current))
                self.assertFalse(kwargs["already_applied"](current))
                raise sp.git_store.CASConflict("Worker claimed first")

            with patch.object(sp.git_store, "publish_cas", side_effect=raced_publish):
                result = sp.CasProjectPublisher(root).publish(
                    "p", snapshot, self._manual_plan()
                )
            self.assertFalse(result.published)
            self.assertEqual(result.reason, "cas_race")
            self.assertEqual(
                (project / "commands" / "command-004.md").read_text(encoding="utf-8"),
                old_text,
            )
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), _state())

    def test_manual_wins_stale_old_worker_claim_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _old_text = self._project(root)
            old_state = json.loads((project / "state.json").read_text(encoding="utf-8"))
            snapshot = self._snapshot(old_state)
            with patch.object(sp.git_store, "publish_cas", side_effect=self._fake_publish(project / "state.json")):
                result = sp.CasProjectPublisher(root).publish(
                    "p", snapshot, self._manual_plan()
                )
            self.assertTrue(result.published)
            old_meta = pc.parse_command_metadata(
                (project / "commands" / "command-004.md").read_text(encoding="utf-8")
            )
            with self.assertRaises(bw.CASConflict):
                bw.validate_command(
                    state=json.loads((project / "state.json").read_text(encoding="utf-8")),
                    command_id=4,
                    meta=old_meta,
                )

    def test_retry_is_idempotent_and_does_not_allocate_another_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _old_text = self._project(root)
            snapshot = self._snapshot(json.loads((project / "state.json").read_text(encoding="utf-8")))
            plan = self._manual_plan("stable-request")
            publisher = sp.CasProjectPublisher(root)
            with patch.object(sp.git_store, "publish_cas", side_effect=self._fake_publish(project / "state.json")):
                first = publisher.publish("p", snapshot, plan)
                second = publisher.publish("p", snapshot, plan)
            self.assertTrue(first.published)
            self.assertTrue(second.published)
            self.assertEqual(second.reason, "already_published")
            self.assertEqual(
                json.loads((project / "state.json").read_text(encoding="utf-8"))["latest_command"],
                5,
            )
            self.assertTrue((project / "commands" / "command-005.md").exists())
            self.assertFalse((project / "commands" / "command-006.md").exists())

    def test_monotonic_command_id_and_exact_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _old_text = self._project(root)
            (project / "commands" / "command-010.md").write_text("orphan", encoding="utf-8")
            state = json.loads((project / "state.json").read_text(encoding="utf-8"))
            snapshot = self._snapshot(state)
            with patch.object(sp.git_store, "publish_cas", side_effect=self._fake_publish(project / "state.json")):
                result = sp.CasProjectPublisher(root).publish(
                    "p", snapshot, self._manual_plan()
                )
            self.assertTrue(result.published)
            updated = json.loads((project / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["generation"], state["generation"] + 1)
            self.assertEqual(updated["latest_command"], 11)
            meta = pc.parse_command_metadata(
                (project / "commands" / "command-011.md").read_text(encoding="utf-8")
            )
            self.assertEqual(meta["expected_generation"], 8)
            self.assertEqual(meta["supersedes_command_id"], 4)

    def test_manual_supersede_decision_rejects_ambiguous_targets(self) -> None:
        cases = [
            (_state(status="CODEX_RUNNING", generation=8, active_run={"run_id": "r"}), "scheduled_chatgpt", "EXECUTE", {}),
            (_state(active_run={"run_id": "lease"}), "scheduled_chatgpt", "EXECUTE", {}),
            (_state(), "manual_chatgpt", "EXECUTE", {}),
            (_state(), "user_direct", "EXECUTE", {}),
            (_state(), "finalizer", "FINALIZE", {}),
            (_state(), "scheduled_chatgpt", "FINALIZE", {}),
            (_state(), "scheduled_chatgpt", "EXECUTE", {"supersedes_command_id": 2}),
            (_state(), "scheduled_chatgpt", "EXECUTE", {"manual_request_id": "old"}),
        ]
        for state, source, kind, extra in cases:
            with self.subTest(status=state["status"], source=source, kind=kind):
                meta = {
                    "command_id": 4,
                    "source": source,
                    "based_on_report": 3,
                    "expected_generation": 7,
                    "kind": kind,
                    **extra,
                }
                with self.assertRaises((pc.ProtocolConflict, pc.ProtocolViolation)):
                    pc.manual_supersede_decision(state, meta)

    def test_stale_identity_and_malformed_target_fail_closed(self) -> None:
        good = {
            "command_id": 4,
            "source": "scheduled_chatgpt",
            "based_on_report": 3,
            "expected_generation": 7,
            "kind": "EXECUTE",
        }
        for field, value in (
            ("generation", 8),
            ("latest_report", 4),
            ("latest_command", 5),
        ):
            changed = _state(**{field: value})
            with self.subTest(field=field):
                with self.assertRaises((pc.ProtocolConflict, pc.ProtocolViolation)):
                    pc.manual_supersede_decision(changed, good)
        malformed = dict(good)
        malformed["expected_generation"] = "7"
        with self.assertRaises(pc.ProtocolViolation):
            pc.manual_supersede_decision(_state(), malformed)




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
