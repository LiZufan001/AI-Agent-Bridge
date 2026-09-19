"""Focused proof for the Worker-owned post-settlement exit-75 boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge_worker as bw
import bridge_worker_hardened as hardened
from codex_lifecycle import CodexRunResult
from vnext_runtime.adoption import AdoptionMode, RollbackIdentity
from vnext_runtime.handoff import (
    HandoffEvidenceStore,
    HandoffPhase,
    HandoffIdentity,
    OuterControllerHandoff,
)


SHA_STABLE = "a" * 40
SHA_MAIN = "b" * 40
SHA_CANDIDATE = "c" * 40
SHA_ADOPTED = "d" * 40


def _run_result() -> CodexRunResult:
    return CodexRunResult(
        exit_code=0,
        final_message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
        stdout_tail="",
        stderr_tail="",
        marker={"status": "SUCCESS"},
        launched_at="2001-01-15T00:00:00+00:00",
        final_marker_detected_at="2001-01-15T00:00:01+00:00",
        process_exited_at="2001-01-15T00:00:02+00:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result="VALID",
    )


class PostRunHandoffExitTests(unittest.TestCase):
    def identity(
        self,
        *,
        project_id: str = "p",
        command_id: int = 4,
        run_id: str = "run-004-exact",
        claim_generation: int = 8,
        attempt_id: str = "attempt-fresh",
        deadline: datetime | None = None,
        mode: AdoptionMode = AdoptionMode.MANUAL,
    ) -> HandoffIdentity:
        started = datetime(2001, 1, 15, 0, 0, tzinfo=timezone.utc)
        return HandoffIdentity(
            attempt_id=attempt_id,
            initiated_at=started.isoformat(),
            previous_stable_sha=SHA_STABLE,
            latest_main_sha=SHA_MAIN,
            accepted_candidate_sha=SHA_CANDIDATE,
            reconciled_adoption_sha=SHA_ADOPTED,
            initiating_project_id=project_id,
            initiating_run_id=run_id,
            initiating_command_id=command_id,
            initiating_claim_generation=claim_generation,
            expected_launcher_identity="outer-launcher-v1",
            expected_worker_identity="worker-v1",
            startup_contract=("outer_launcher_job", "exit_75_restart"),
            health_deadline=(
                deadline or datetime(2099, 1, 1, tzinfo=timezone.utc)
            ).isoformat(),
            health_criteria=(
                "launcher_alive",
                "worker_healthy",
                "protocol_ready",
                "recovery_clear",
            ),
            probation_seconds=2,
            rollback_identity=RollbackIdentity(known_good_sha=SHA_STABLE),
            mode=mode,
        )

    def settlement(
        self,
        root: Path,
        *,
        outcome: str = "SUCCESS",
        status: str = "REPORT_READY",
        active_run: object = None,
        project_id: str = "p",
        command_id: int = 4,
        run_id: str = "run-004-exact",
        claim_generation: int = 8,
    ) -> bw.WorkerRunSettlement:
        report_path = (
            root
            / "projects"
            / project_id
            / "reports"
            / f"report-{command_id:03d}.md"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# settled report\n", encoding="utf-8")
        return bw.WorkerRunSettlement(
            project_id=project_id,
            command_id=command_id,
            run_id=run_id,
            claim_generation=claim_generation,
            outcome=outcome,
            final_state={
                "status": status,
                "project_id": project_id,
                "generation": claim_generation + 1,
                "latest_command": command_id,
                "latest_report": command_id,
                "active_run": active_run,
                "worker_pid": None,
            },
        )

    def prepare(self, root: Path, identity: HandoffIdentity) -> HandoffEvidenceStore:
        store = HandoffEvidenceStore(
            root / "worker" / "runtime" / "adoption-handoff.json"
        )
        OuterControllerHandoff(store).prepare(identity)
        return store

    def test_no_handoff_and_stale_attempt_do_not_request_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settled = self.settlement(root)
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

            stale = self.identity(
                run_id="run-020-stale",
                command_id=20,
                claim_generation=20,
                attempt_id="attempt-020",
            )
            self.prepare(root, stale)
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

    def test_each_mismatched_identity_is_fail_closed(self) -> None:
        settled_values = (
            ("project", self.identity(project_id="other")),
            ("command", self.identity(command_id=5, run_id="run-005-other")),
            ("run", self.identity(run_id="run-004-other")),
            ("generation", self.identity(claim_generation=9)),
        )
        for label, identity in settled_values:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settled = self.settlement(root)
                self.prepare(root, identity)
                self.assertFalse(
                    hardened.should_request_post_run_handoff_exit(root, settled)
                )

    def test_corrupt_stale_unattended_and_nonprepared_evidence_do_not_request_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settled = self.settlement(root)
            evidence_path = root / "worker" / "runtime" / "adoption-handoff.json"
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text('{"schema_version":1}\n', encoding="utf-8")
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

            evidence_path.unlink()
            expired = self.identity(
                deadline=datetime(2001, 1, 15, 0, 1, tzinfo=timezone.utc)
            )
            self.prepare(root, expired)
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

            evidence_path.unlink()
            unattended = self.identity(mode=AdoptionMode.UNATTENDED)
            self.prepare(root, unattended)
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

            evidence_path.unlink()
            controller = OuterControllerHandoff(
                HandoffEvidenceStore(evidence_path)
            )
            identity = self.identity()
            controller.prepare(identity)
            controller.begin_draining(identity)
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

            controller.mark_superseded(identity, reason="test_closed")
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, settled))

    def test_active_recovery_blocked_failed_and_unresolved_publication_do_not_request_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = self.identity()
            self.prepare(root, identity)

            active = self.settlement(
                root,
                active_run={
                    "run_id": identity.initiating_run_id,
                    "command_id": identity.initiating_command_id,
                },
            )
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, active))

            recovery = self.settlement(root, status="RECOVERY_REQUIRED")
            self.assertFalse(hardened.should_request_post_run_handoff_exit(root, recovery))

            for outcome in ("BLOCKED", "FAILED"):
                failed = self.settlement(root, outcome=outcome)
                self.assertFalse(
                    hardened.should_request_post_run_handoff_exit(root, failed)
                )

            metadata_path = bw.pending_report.canonical_metadata_path(root, "p", 4)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text("{}\n", encoding="utf-8")
            self.assertFalse(
                hardened.should_request_post_run_handoff_exit(root, self.settlement(root))
            )
            metadata_path.unlink()
            recovery_path = bw.recovery_journal.journal_path(
                root,
                "p",
                identity.initiating_run_id,
            )
            recovery_path.parent.mkdir(parents=True, exist_ok=True)
            recovery_path.write_text("{}\n", encoding="utf-8")
            self.assertFalse(
                hardened.should_request_post_run_handoff_exit(root, self.settlement(root))
            )

    def test_exact_fresh_handoff_is_read_only_and_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = self.identity()
            store = self.prepare(root, identity)
            settled = self.settlement(root)
            before = store.path.read_bytes()
            self.assertTrue(hardened.should_request_post_run_handoff_exit(root, settled))
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.read().phase, HandoffPhase.PREPARED)

            callback = hardened._post_run_handoff_callback(root)
            with self.assertRaises(bw.PostRunHandoffRestartRequested):
                callback(settled)
            callback(settled)

    def test_real_process_settlement_passes_exact_run_to_exit75_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("mission\n", encoding="utf-8")
            initial_state = {
                "protocol_version": 2,
                "project_id": "p",
                "status": "COMMAND_READY",
                "generation": 7,
                "latest_command": 4,
                "latest_report": 3,
                "active_run": None,
            }
            state_path = project / "state.json"
            state_path.write_text(json.dumps(initial_state), encoding="utf-8")
            (project / "commands" / "command-004.md").write_text(
                '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
                '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )

            exact_run_id = "run-004-1234567890ab"
            self.prepare(
                root,
                self.identity(run_id=exact_run_id, claim_generation=8),
            )
            remote_state = dict(initial_state)
            settlements: list[bw.WorkerRunSettlement] = []

            def publish(**kwargs: object) -> dict[str, object]:
                nonlocal remote_state
                expected = kwargs["expected"]
                already_applied = kwargs["already_applied"]
                payload_builder = kwargs["payload_builder"]
                assert callable(expected)
                assert callable(already_applied)
                assert callable(payload_builder)
                if not expected(remote_state) and not already_applied(remote_state):
                    raise bw.CASConflict("fixture CAS conflict")
                payloads = payload_builder(remote_state)
                for path, content in payloads.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                remote_state = json.loads(
                    (state_path.read_text(encoding="utf-8"))
                )
                return remote_state

            callback = hardened._post_run_handoff_callback(root)

            def observe(settlement: bw.WorkerRunSettlement) -> None:
                settlements.append(settlement)
                callback(settlement)

            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
                "network_guard": {"enabled": False},
            }
            fixed_uuid = uuid.UUID("1234567890abcdef1234567890abcdef")
            with patch.object(bw.uuid, "uuid4", return_value=fixed_uuid), patch.object(
                bw,
                "publish_cas",
                side_effect=publish,
            ), patch.object(bw, "get_head", return_value=SHA_MAIN), patch.object(
                bw,
                "codex_run",
                return_value=_run_result(),
            ):
                with self.assertRaises(bw.PostRunHandoffRestartRequested):
                    bw.process_project(
                        root,
                        "p",
                        {"workdir": "__BRIDGE_ROOT__"},
                        config,
                        on_run_settled=observe,
                    )

            self.assertEqual(len(settlements), 1)
            self.assertEqual(settlements[0].run_id, exact_run_id)
            self.assertEqual(settlements[0].claim_generation, 8)
            self.assertEqual(settlements[0].outcome, "SUCCESS")
            self.assertEqual(remote_state["status"], "REPORT_READY")
            self.assertIsNone(remote_state["active_run"])
            self.assertEqual(bw.WORKER_RESTART_CODE, 75)
            self.assertEqual(
                HandoffEvidenceStore(
                    root / "worker" / "runtime" / "adoption-handoff.json"
                ).read().phase,
                HandoffPhase.PREPARED,
            )

    def test_hardened_main_returns_supported_exit75_after_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = self.identity()
            self.prepare(root, identity)
            settled = self.settlement(root)
            config_path = root / "config.json"
            config_path.write_text("{}\n", encoding="utf-8")
            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
                "projects": {"p": {"enabled": True, "workdir": "__BRIDGE_ROOT__"}},
            }

            def process(
                _root: Path,
                _project_id: str,
                _project_cfg: dict[str, object],
                _config: dict[str, object],
                *,
                on_run_settled,
            ) -> bool:
                on_run_settled(settled)
                return True

            from test_state_fixture import make_state
            args = SimpleNamespace(
                state_root=make_state(root),
                config=str(config_path),
                once=True,
                project=None,
            )
            fake_module_file = root / "worker" / "bridge_worker_hardened.py"
            with patch.object(hardened, "__file__", str(fake_module_file)), patch.object(
                hardened.bw,
                "parse_args",
                return_value=args,
            ), patch.object(hardened.bw, "ensure_tool"), patch.object(
                hardened,
                "_resolve_config_path",
                return_value=config_path,
            ), patch.object(
                hardened.rpr,
                "load_runtime_config",
                return_value=config,
            ), patch.object(
                hardened,
                "_validate_codex_config",
            ), patch.object(
                hardened.rpr,
                "git_head",
                return_value=SHA_MAIN,
            ), patch.object(
                hardened.bw,
                "WorkerInstanceLock",
                return_value=nullcontext(),
            ), patch.object(
                hardened.bw,
                "sync_to_remote",
            ), patch.object(
                hardened.bw,
                "reconcile_pending_recoveries",
                return_value=0,
            ), patch.object(
                hardened.bw,
                "reconcile_pending_reports",
                return_value=0,
            ), patch.object(
                hardened.rpr,
                "worker_code_changed",
                return_value=False,
            ), patch.object(
                hardened,
                "_observe_project",
            ), patch.object(
                hardened,
                "_remote_project_is_safe_to_process",
                return_value=True,
            ), patch.object(
                hardened.bw,
                "process_project",
                side_effect=process,
            ):
                self.assertEqual(hardened.main(), 75)

            health = json.loads(
                (root / "worker" / "runtime" / "worker-health.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(health["last_process_exit"], 75)
            self.assertIsNone(health["worker_pid"])




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
