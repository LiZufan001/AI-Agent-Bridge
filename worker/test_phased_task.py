import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_common
import bridge_worker as bw
import phased_task
from codex_lifecycle import CodexRunResult


def phased_command(
    phases=("A",),
    *,
    global_text="global instructions",
    final_text="final checks",
    title=None,
    leading_blank_lines=0,
):
    lines = [""] * leading_blank_lines
    if title is not None:
        lines.append(title)
    lines.extend(
        [
            '<!-- bridge-command: {"command_id":14,"source":"scheduled_chatgpt",'
            '"based_on_report":13,"expected_generation":40,"kind":"EXECUTE"} -->',
            f'<!-- bridge-phased-task: {json.dumps({"schema_version": 1, "phases": list(phases)})} -->',
            "",
            "<!-- bridge-global:start -->",
            global_text,
            "<!-- bridge-global:end -->",
        ]
    )
    for phase in phases:
        lines.extend(
            [
                f"<!-- bridge-phase:{phase}:start -->",
                f"phase {phase} work",
                f"<!-- bridge-phase:{phase}:end -->",
            ]
        )
    lines.extend(
        [
            "<!-- bridge-final-acceptance:start -->",
            final_text,
            "<!-- bridge-final-acceptance:end -->",
        ]
    )
    return "\n".join(lines) + "\n"


def run_result(status="SUCCESS"):
    message = f'BRIDGE_EXECUTION_JSON: {{"status":"{status}"}}'
    return CodexRunResult(
        exit_code=0,
        final_message=message,
        stdout_tail="",
        stderr_tail="",
        marker={"status": status},
        launched_at="2001-01-15T00:00:00+08:00",
        final_marker_detected_at="2001-01-15T00:00:01+08:00",
        process_exited_at="2001-01-15T00:00:02+08:00",
        wrapper_pid=123,
        stdout_log_path=Path("stdout.log"),
        stderr_log_path=Path("stderr.log"),
        process_scope="test",
        marker_result="VALID",
    )


class PhasedTaskParserTests(unittest.TestCase):
    def test_one_to_four_phases_are_valid(self):
        for count in range(1, 5):
            with self.subTest(count=count):
                parsed = phased_task.parse_phased_task(
                    phased_command(tuple("ABCD"[:count]))
                )
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.phases, tuple("ABCD"[:count]))

    def test_one_strict_command_title_is_accepted_in_the_header_envelope(self):
        text = phased_command(
            ("A", "B", "C"),
            leading_blank_lines=1,
            title="# Command 014 — 修复中文标题 and improve the user experience",
        )
        parsed = phased_task.parse_phased_task(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.phases, ("A", "B", "C"))
        self.assertEqual(parsed.global_text, "global instructions")
        self.assertEqual(
            parsed.phase_texts,
            {"A": "phase A work", "B": "phase B work", "C": "phase C work"},
        )
        self.assertEqual(parsed.final_acceptance_text, "final checks")
        self.assertEqual(parsed.source_text, text)

    def test_only_a_single_header_title_is_allowed_and_unmarked_prose_stays_rejected(self):
        valid = phased_command(("A", "B"))
        cases = {
            "random title": valid.replace(
                "<!-- bridge-command:", "# Random title\n<!-- bridge-command:"
            ),
            "h2 command title": valid.replace(
                "<!-- bridge-command:", "## Command 014 — not an H1\n<!-- bridge-command:"
            ),
            "arbitrary header prose": valid.replace(
                "<!-- bridge-command:", "Some instructions here\n<!-- bridge-command:"
            ),
            "prose between phases": valid.replace(
                "<!-- bridge-phase:B:start -->",
                "intervening prose\n<!-- bridge-phase:B:start -->",
            ),
            "late command title": valid.replace(
                "<!-- bridge-phase:B:start -->",
                "# Command 014 — too late\n<!-- bridge-phase:B:start -->",
            ),
            "prose after final": valid + "after final acceptance\n",
            "late command title after final": valid + "# Command 014 — too late\n",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(phased_task.PhasedTaskError):
                    phased_task.parse_phased_task(text)

        duplicate = phased_command(("A",), title="# Command 014 — first")
        duplicate = duplicate.replace(
            "# Command 014 — first\n",
            "# Command 014 — first\n# Command 014 — second\n",
        )
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.parse_phased_task(duplicate)

    def test_five_phases_and_non_prefix_phases_are_rejected(self):
        for phases in (("A", "B", "C", "D", "E"), ("A", "C"), ("B",)):
            with self.subTest(phases=phases):
                text = phased_command(("A",))
                text = text.replace(
                    '{"schema_version": 1, "phases": ["A"]}',
                    json.dumps({"schema_version": 1, "phases": list(phases)}),
                )
                with self.assertRaises(phased_task.PhasedTaskError):
                    phased_task.parse_phased_task(text)

    def test_malformed_and_unsupported_declarations_are_rejected(self):
        cases = (
            '<!-- bridge-phased-task: {bad} -->',
            '<!-- bridge-phased-task: {"schema_version":2,"phases":["A"]} -->',
            '<!-- bridge-phased-task: ["A"] -->',
            '<!-- bridge-phased-task -->',
            '<!-- bridge-phased-task: {"schema_version":1,"phases":["A"],"x":1} -->',
            '<!-- bridge-phased-task: {"schema_version":1,"phases":["A"],"phases":["B"]} -->',
        )
        for declaration in cases:
            with self.subTest(declaration=declaration):
                with self.assertRaises(phased_task.PhasedTaskError):
                    phased_task.parse_phased_task(declaration)

    def test_duplicate_declaration_and_unmarked_body_content_are_rejected(self):
        declaration = '<!-- bridge-phased-task: {"schema_version":1,"phases":["A"]} -->'
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.parse_phased_task(
                declaration + "\n" + declaration + "\n" + phased_command(("A",))
            )
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.parse_phased_task(
                phased_command(("A",)).replace(
                    "<!-- bridge-global:start -->",
                    "unmarked task prose\n<!-- bridge-global:start -->",
                )
            )

    def test_missing_or_empty_sections_are_rejected(self):
        valid = phased_command(("A", "B"))
        cases = {
            "missing global": valid.replace(
                "<!-- bridge-global:start -->\nglobal instructions\n<!-- bridge-global:end -->\n",
                "",
            ),
            "empty global": valid.replace("global instructions", "   "),
            "missing declared phase": valid.replace(
                "<!-- bridge-phase:B:start -->\nphase B work\n<!-- bridge-phase:B:end -->\n",
                "",
            ),
            "missing final": valid.replace(
                "<!-- bridge-final-acceptance:start -->\nfinal checks\n<!-- bridge-final-acceptance:end -->\n",
                "",
            ),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(phased_task.PhasedTaskError):
                    phased_task.parse_phased_task(text)

    def test_undeclared_duplicate_and_order_errors_are_rejected(self):
        valid = phased_command(("A",))
        cases = {
            "undeclared": valid.replace(
                "<!-- bridge-phase:A:start -->", "<!-- bridge-phase:B:start -->"
            ).replace("<!-- bridge-phase:A:end -->", "<!-- bridge-phase:B:end -->"),
            "duplicate phase": valid.replace(
                "<!-- bridge-final-acceptance:start -->",
                "<!-- bridge-phase:A:start -->\nsecond A\n<!-- bridge-phase:A:end -->\n"
                "<!-- bridge-final-acceptance:start -->",
            ),
            "duplicate final": valid.replace(
                "<!-- bridge-final-acceptance:end -->\n",
                "<!-- bridge-final-acceptance:end -->\n"
                "<!-- bridge-final-acceptance:start -->\nsecond\n"
                "<!-- bridge-final-acceptance:end -->\n",
            ),
            "order mismatch": valid.replace(
                "<!-- bridge-global:start -->\nglobal instructions\n<!-- bridge-global:end -->\n"
                "<!-- bridge-phase:A:start -->\nphase A work\n<!-- bridge-phase:A:end -->\n",
                "<!-- bridge-phase:A:start -->\nphase A work\n<!-- bridge-phase:A:end -->\n"
                "<!-- bridge-global:start -->\nglobal instructions\n<!-- bridge-global:end -->\n",
            ),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(phased_task.PhasedTaskError):
                    phased_task.parse_phased_task(text)

    def test_nested_overlapping_and_malformed_markers_are_rejected(self):
        valid = phased_command(("A",))
        cases = (
            valid.replace(
                "<!-- bridge-global:end -->",
                "<!-- bridge-phase:A:start -->\n<!-- bridge-global:end -->",
            ),
            valid.replace(
                "<!-- bridge-phase:A:end -->",
                "<!-- bridge-phase:B:end -->",
            ),
            valid.replace(
                "<!-- bridge-final-acceptance:start -->",
                "<!-- bridge-final-acceptance:begin -->",
            ),
        )
        for text in cases:
            with self.assertRaises(phased_task.PhasedTaskError):
                phased_task.parse_phased_task(text)

    def test_ordinary_command_is_not_phased_and_metadata_is_untouched(self):
        ordinary = (
            '<!-- bridge-command: {"command_id":14,"source":"scheduled_chatgpt",'
            '"based_on_report":13,"expected_generation":40,"kind":"EXECUTE"} -->\n'
            "# ordinary command\n"
        )
        self.assertIsNone(phased_task.parse_phased_task(ordinary))
        self.assertEqual(bw.command_metadata(ordinary)["command_id"], 14)
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.parse_phased_task(
                ordinary + "<!-- bridge-global:start -->\ntext\n<!-- bridge-global:end -->\n"
            )

    def test_synthetic_three_phase_command_has_no_projection_contamination(self):
        command_text = phased_command(("A", "B", "C"),
            title="# Command 014 - synthetic acceptance", global_text="Synthetic Owner evidence",
            final_text="Final acceptance")
        command_bytes = command_text.encode("utf-8")
        parsed = phased_task.parse_phased_task(command_text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.phases, ("A", "B", "C"))
        self.assertIn("Synthetic Owner evidence", parsed.global_text)
        for phase in ("A", "B", "C"):
            self.assertIn(f"phase {phase} work", parsed.phase_text(phase))
        self.assertIn("Final acceptance", parsed.final_acceptance_text)
        for projection in (parsed.global_text, *(text for _phase, text in parsed.phase_sections),
                           parsed.final_acceptance_text):
            self.assertNotIn("# Command 014", projection)
        self.assertEqual(parsed.source_text.encode("utf-8"), command_bytes)
        self.assertEqual(bw.command_metadata(command_text)["command_id"], 14)


class PhasedTaskRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from test_state_fixture import make_state
        make_state(self.root)
        self.run_dir = self.root / "worker" / "runtime" / "p" / "runs" / "run-014-test"
        self.identity = phased_task.ExecutionIdentity(
            project_id="p", command_id=14, run_id="run-014-test", claim_generation=41
        )

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, phases=("A", "B", "C", "D")):
        command = phased_command(phases).replace(
            '"expected_generation":40', '"expected_generation":41'
        )
        return phased_task.prepare_runtime(
            self.run_dir, command.replace("\n", "\r\n").encode("utf-8"), self.identity
        )

    def test_snapshot_is_exact_and_manifest_binds_identity_hashes(self):
        inspection = self.prepare(("A", "B"))
        snapshot = inspection.paths.snapshot.read_bytes()
        self.assertIn(b"\r\n", snapshot)
        self.assertEqual(
            inspection.manifest["snapshot_sha256"],
            __import__("hashlib").sha256(snapshot).hexdigest(),
        )
        self.assertEqual(inspection.manifest["project_id"], "p")
        self.assertEqual(inspection.manifest["command_id"], 14)
        self.assertEqual(inspection.manifest["run_id"], "run-014-test")
        self.assertEqual(inspection.manifest["claim_generation"], 41)

    def test_command_title_is_kept_only_in_exact_snapshot_not_projections(self):
        command = phased_command(
            ("A", "B"),
            title="# Command 014 — Unicode 标题 remains human-readable",
        )
        inspection = phased_task.prepare_runtime(
            self.run_dir,
            command.encode("utf-8"),
            self.identity,
        )
        self.assertEqual(
            inspection.paths.snapshot.read_bytes(), command.encode("utf-8")
        )
        self.assertNotIn(
            "# Command 014", inspection.paths.global_projection.read_text()
        )
        self.assertNotIn(
            "# Command 014", inspection.paths.current_phase.read_text()
        )

        phased_task.advance_phase(self.run_dir, "A")
        inspection = phased_task.advance_phase(self.run_dir, "B")
        self.assertEqual(
            inspection.progress["current_phase"], phased_task.FINAL_ACCEPTANCE
        )
        self.assertNotIn(
            "# Command 014", inspection.paths.current_phase.read_text()
        )

    def test_initial_projection_materializes_only_global_and_phase_a(self):
        inspection = self.prepare(("A", "B", "C", "D"))
        self.assertEqual(inspection.progress["current_phase"], "A")
        self.assertEqual(inspection.progress["completed_phases"], [])
        self.assertEqual(inspection.paths.current_phase.read_text().strip(), "phase A work")
        self.assertFalse((self.run_dir / "phase-A.md").exists())
        self.assertFalse((self.run_dir / "phase-B.md").exists())
        self.assertEqual(
            {path.name for path in self.run_dir.iterdir()},
            {
                "task-snapshot.md",
                "task-manifest.json",
                "global.md",
                "current-phase.md",
                "phase-progress.json",
            },
        )

    def test_forward_checkpoint_projects_one_next_phase_then_final(self):
        inspection = self.prepare()
        for phase, next_phase in (("A", "B"), ("B", "C"), ("C", "D"), ("D", "FINAL_ACCEPTANCE")):
            inspection = phased_task.advance_phase(self.run_dir, phase)
            self.assertEqual(inspection.progress["current_phase"], next_phase)
            self.assertEqual(inspection.progress["completed_phases"], list("ABCD"[: "ABCD".index(phase) + 1]))
            self.assertFalse((self.run_dir / f"phase-{phase}.md").exists())
        self.assertFalse(inspection.progress["final_acceptance_verified"])
        self.assertEqual(inspection.paths.current_phase.read_text().strip(), "final checks")
        inspection = phased_task.complete_final(self.run_dir)
        self.assertTrue(inspection.progress["final_acceptance_verified"])
        self.assertEqual(inspection.progress["current_phase"], "FINAL_ACCEPTANCE")

    def test_checkpoint_cannot_skip_repeat_or_go_backward(self):
        self.prepare(("A", "B", "C"))
        for phase in ("B", "D", "X"):
            with self.subTest(phase=phase):
                with self.assertRaises(phased_task.PhasedTaskError):
                    phased_task.advance_phase(self.run_dir, phase)
        phased_task.advance_phase(self.run_dir, "A")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.advance_phase(self.run_dir, "A")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.complete_final(self.run_dir)

    def test_identity_snapshot_manifest_and_progress_tamper_fail_closed(self):
        self.prepare(("A", "B"))
        self.run_dir.joinpath("task-snapshot.md").write_text("tampered", encoding="utf-8")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.verify_runtime(self.run_dir)

        self.temp.cleanup()
        self.setUp()
        self.prepare(("A", "B"))
        manifest = json.loads(self.run_dir.joinpath("task-manifest.json").read_text())
        manifest["command_id"] = 99
        self.run_dir.joinpath("task-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.verify_runtime(self.run_dir)

        self.temp.cleanup()
        self.setUp()
        self.prepare(("A", "B"))
        progress = json.loads(self.run_dir.joinpath("phase-progress.json").read_text())
        progress["current_phase"] = "B"
        self.run_dir.joinpath("phase-progress.json").write_text(json.dumps(progress), encoding="utf-8")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.verify_runtime(self.run_dir)

    def test_invalid_runtime_path_and_path_traversal_are_rejected(self):
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.validate_run_dir(self.root / "worker" / "runtime" / "p" / "runs" / "../escape")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.validate_run_dir(self.root / "worker" / "not-runtime" / "p" / "runs" / "run-1")

    def test_runtime_identity_cannot_use_wrong_project_or_run(self):
        command = phased_command(("A",))
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.prepare_runtime(
                self.run_dir,
                command,
                phased_task.ExecutionIdentity(
                    project_id="other",
                    command_id=14,
                    run_id="run-014-test",
                    claim_generation=41,
                ),
            )
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.prepare_runtime(
                self.run_dir,
                command,
                phased_task.ExecutionIdentity(
                    project_id="p",
                    command_id=14,
                    run_id="run-015-test",
                    claim_generation=41,
                ),
            )

    def test_atomic_checkpoint_leaves_no_temporary_files_and_lock_is_fail_closed(self):
        self.prepare(("A", "B"))
        self.assertFalse(list(self.run_dir.glob("*.tmp")))
        lock = self.run_dir / "phase-progress.lock"
        lock.write_text("held", encoding="ascii")
        with self.assertRaises(phased_task.PhasedTaskError):
            phased_task.advance_phase(self.run_dir, "A")
        lock.unlink()
        phased_task.advance_phase(self.run_dir, "A")
        self.assertFalse(lock.exists())
        self.assertFalse(list(self.run_dir.glob("*.tmp")))

    def test_complete_final_and_compaction_require_safe_gates(self):
        self.prepare(("A",))
        for gates in (
            dict(canonical_publication_succeeded=False),
            dict(canonical_publication_succeeded=True, pending_publication=True),
            dict(canonical_publication_succeeded=True, recovery_unresolved=True),
            dict(canonical_publication_succeeded=True, conflict=True),
        ):
            with self.subTest(gates=gates):
                effective_gates = {
                    "canonical_publication_succeeded": False,
                    "pending_publication": False,
                    "recovery_unresolved": False,
                    "conflict": False,
                }
                effective_gates.update(gates)
                self.assertFalse(
                    phased_task.safe_compact_successful_run(
                        self.run_dir,
                        outcome="SUCCESS",
                        **effective_gates,
                    )
                )
        self.assertTrue(self.run_dir.joinpath("task-snapshot.md").exists())
        phased_task.advance_phase(self.run_dir, "A")
        phased_task.complete_final(self.run_dir)
        self.assertTrue(
            phased_task.safe_compact_successful_run(
                self.run_dir,
                outcome="SUCCESS",
                canonical_publication_succeeded=True,
                pending_publication=False,
                recovery_unresolved=False,
                conflict=False,
            )
        )
        for name in ("task-snapshot.md", "global.md", "current-phase.md"):
            self.assertFalse((self.run_dir / name).exists())
        for name in ("task-manifest.json", "phase-progress.json"):
            self.assertTrue((self.run_dir / name).exists())

    def test_cli_advance_and_complete_final_use_absolute_run_dir(self):
        self.prepare(("A",))
        helper = Path(__file__).resolve().with_name("phased_task.py")
        advance = subprocess.run(
            [
                sys.executable,
                str(helper),
                "advance",
                "--run-dir",
                str(self.run_dir),
                "--bridge-root",
                str(self.root),
                "--phase",
                "A",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        self.assertEqual(json.loads(advance.stdout)["current_phase"], "FINAL_ACCEPTANCE")
        complete = subprocess.run(
            [
                sys.executable,
                str(helper),
                "complete-final",
                "--run-dir",
                str(self.run_dir),
                "--bridge-root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertTrue(json.loads(complete.stdout)["final_acceptance_verified"])

    def test_bounded_pruning_keeps_current_and_unresolved_phased_evidence(self):
        runs = self.root / "worker" / "runtime" / "p" / "runs"
        current = runs / "run-014-current"
        old = runs / "run-013-old"
        pending = runs / "run-012-pending"
        recovery = runs / "run-011-recovery"
        for path in (current, old, pending, recovery):
            path.mkdir(parents=True)
        os.utime(old, (100, 100))
        os.utime(pending, (200, 200))
        os.utime(recovery, (300, 300))
        runtime = runs.parent
        (runtime / "pending-report-014.meta.json").write_text(
            json.dumps(
                {
                    "run_id": pending.name,
                    "journal_status": "pending",
                    "remote_publish_pending": True,
                }
            ),
            encoding="utf-8",
        )
        (runtime / "recovery").mkdir()
        (runtime / "recovery" / f"{recovery.name}.json").write_text(
            json.dumps(
                {
                    "run_id": recovery.name,
                    "journal_status": "pending",
                    "remote_publish_pending": True,
                }
            ),
            encoding="utf-8",
        )
        bw.prune_run_directories(runs, current, keep=1)
        self.assertTrue(current.exists())
        self.assertTrue(pending.exists())
        self.assertTrue(recovery.exists())
        self.assertFalse(old.exists())


class PhasedWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "p"
        (self.project / "commands").mkdir(parents=True)
        (self.project / "reports").mkdir()
        (self.project / "MISSION.md").write_text("mission", encoding="utf-8")
        self.state_path = self.project / "state.json"
        self.initial_state = {
            "protocol_version": 2,
            "project_id": "p",
            "status": "COMMAND_READY",
            "generation": 40,
            "latest_command": 14,
            "latest_report": 13,
            "last_reviewed_report": 13,
            "active_run": None,
        }
        self.state_path.write_text(json.dumps(self.initial_state), encoding="utf-8")
        self.command_path = self.project / "commands" / "command-014.md"
        self.command_path.write_text(phased_command(("A", "B")), encoding="utf-8")
        self.config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "network_guard": {"enabled": False},
        }

    def tearDown(self):
        self.temp.cleanup()

    def fake_publish(self, *, fail_report=False):
        remote_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        calls = []

        def publish(**kwargs):
            nonlocal remote_state
            calls.append(kwargs)
            payloads = kwargs["payload_builder"](remote_state)
            updated = json.loads(payloads[kwargs["state_path"]])
            if len(calls) == 2 and fail_report:
                self.state_path.write_text(json.dumps(updated), encoding="utf-8")
                raise bridge_common.WorkerError("temporary publication failure")
            for path, text in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            remote_state = updated
            return remote_state

        return publish, calls

    def run_worker(self, *, status="SUCCESS", advance=True, tamper=False, fail_report=False):
        run_dir = self.root / "worker" / "runtime" / "p" / "runs"
        codex_calls = []

        def codex(**kwargs):
            codex_calls.append(kwargs)
            actual_run = next(run_dir.iterdir())
            if advance:
                phased_task.advance_phase(actual_run, "A")
                if status == "SUCCESS" and "B" in phased_task.load_progress(actual_run)["phases"]:
                    phased_task.advance_phase(actual_run, "B")
                if status == "SUCCESS":
                    phased_task.complete_final(actual_run)
            if tamper:
                (actual_run / "task-snapshot.md").write_text("tampered", encoding="utf-8")
            return run_result(status)

        publish, calls = self.fake_publish(fail_report=fail_report)
        with patch.object(bw, "publish_cas", side_effect=publish), patch.object(
            bw, "get_head", return_value="head"
        ), patch.object(bw, "codex_run", side_effect=codex):
            processed = bw.process_project(
                self.root, "p", {"workdir": "__BRIDGE_ROOT__"}, self.config
            )
        return processed, codex_calls, calls, run_dir

    def test_ordinary_command_keeps_prompt_path_and_creates_no_phased_artifacts(self):
        ordinary = (
            '<!-- bridge-command: {"command_id":14,"source":"scheduled_chatgpt",'
            '"based_on_report":13,"expected_generation":40,"kind":"EXECUTE"} -->\n'
            "# ordinary command\nDo the ordinary work.\n"
        )
        self.command_path.write_text(ordinary, encoding="utf-8")
        publish, _calls = self.fake_publish()
        captured = []
        with patch.object(bw, "publish_cas", side_effect=publish), patch.object(
            bw, "get_head", return_value="head"
        ), patch.object(
            bw, "codex_run", side_effect=lambda **kwargs: (captured.append(kwargs) or run_result())
        ) as codex:
            self.assertTrue(
                bw.process_project(
                    self.root, "p", {"workdir": "__BRIDGE_ROOT__"}, self.config
                )
            )
        codex.assert_called_once()
        self.assertEqual(
            captured[0]["prompt"], bw.build_prompt("p", "mission", ordinary)
        )
        self.assertFalse((self.root / "worker" / "runtime" / "p" / "runs").exists())

    def test_phased_worker_uses_one_short_bootstrap_and_compacts_only_after_success(self):
        processed, codex_calls, calls, runs = self.run_worker()
        self.assertTrue(processed)
        self.assertEqual(len(codex_calls), 1)
        prompt = codex_calls[0]["prompt"]
        actual_run = next(runs.iterdir())
        self.assertIn(str(actual_run.resolve()), prompt)
        self.assertIn("global.md", prompt)
        self.assertIn("current-phase.md", prompt)
        self.assertIn("phase-progress.json", prompt)
        self.assertIn("complete-final", prompt)
        self.assertNotIn("phase B work", prompt)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "REPORT_READY")
        report = (self.project / "reports" / "report-014.md").read_text(encoding="utf-8")
        self.assertIn("outcome: SUCCESS", report)
        self.assertFalse((actual_run / "task-snapshot.md").exists())
        self.assertTrue((actual_run / "task-manifest.json").exists())
        self.assertEqual(len(calls), 2)

    def test_success_marker_with_incomplete_progress_fails_contract_without_rerun(self):
        processed, codex_calls, _calls, runs = self.run_worker(advance=False)
        self.assertTrue(processed)
        self.assertEqual(len(codex_calls), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "REPORT_READY")
        self.assertIn("PHASE_CONTRACT_FAILED", state["last_execution_error"])
        report = (self.project / "reports" / "report-014.md").read_text(encoding="utf-8")
        self.assertIn("outcome: FAILED", report)
        self.assertIn("marker_status: SUCCESS", report)
        self.assertIn("PHASE_CONTRACT_FAILED", report)
        actual_run = next(runs.iterdir())
        self.assertTrue((actual_run / "task-snapshot.md").exists())

    def test_success_marker_with_snapshot_tamper_fails_contract(self):
        processed, codex_calls, _calls, _runs = self.run_worker(tamper=True)
        self.assertTrue(processed)
        self.assertEqual(len(codex_calls), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "REPORT_READY")
        self.assertIn("PHASE_CONTRACT_FAILED", state["last_execution_error"])

    def test_failed_and_blocked_partial_runs_preserve_progress_without_rerun(self):
        for status in ("FAILED", "BLOCKED"):
            with self.subTest(status=status):
                self.temp.cleanup()
                self.setUp()
                processed, codex_calls, _calls, runs = self.run_worker(
                    status=status, advance=True
                )
                self.assertTrue(processed)
                self.assertEqual(len(codex_calls), 1)
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "REPORT_READY")
                actual_run = next(runs.iterdir())
                progress = json.loads(
                    (actual_run / "phase-progress.json").read_text(encoding="utf-8")
                )
                self.assertEqual(progress["current_phase"], "B")
                self.assertFalse(progress["final_acceptance_verified"])
                report = (self.project / "reports" / "report-014.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn(f"outcome: {status}", report)
                self.assertIn(f"marker_status: {status}", report)

    def test_publication_failure_preserves_phased_evidence_and_pending_report(self):
        with self.assertRaises(bridge_common.WorkerError):
            self.run_worker(fail_report=True)
        pending = self.root / "worker" / "runtime" / "p" / "pending-report-014.md"
        metadata = self.root / "worker" / "runtime" / "p" / "pending-report-014.meta.json"
        self.assertTrue(pending.exists())
        self.assertTrue(metadata.exists())
        actual_run = next((self.root / "worker" / "runtime" / "p" / "runs").iterdir())
        for name in (
            "task-snapshot.md",
            "task-manifest.json",
            "global.md",
            "current-phase.md",
            "phase-progress.json",
        ):
            self.assertTrue((actual_run / name).exists())

    def test_malformed_phased_command_fails_before_claim_or_codex(self):
        malformed = phased_command(("A", "B")).replace(
            "<!-- bridge-phase:B:end -->", "<!-- bridge-phase:B:begin -->"
        )
        self.command_path.write_text(malformed, encoding="utf-8")
        with patch.object(bw, "publish_cas") as publish, patch.object(
            bw, "codex_run"
        ) as codex:
            with self.assertRaises(phased_task.PhasedTaskError):
                bw.process_project(
                    self.root, "p", {"workdir": "__BRIDGE_ROOT__"}, self.config
                )
        publish.assert_not_called()
        codex.assert_not_called()
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8")),
            self.initial_state,
        )
        health = json.loads(
            (self.root / "worker" / "runtime" / "worker-health.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(health["last_failure_kind"], "PhasedTaskError")
        self.assertEqual(health["last_failure_stage"], "pre_claim_validation")
        self.assertEqual(health["last_failure_project"], "p")
        self.assertEqual(health["last_failure_command_id"], 14)
        self.assertEqual(health["last_failure_state"], "COMMAND_READY")
        self.assertIsInstance(health["last_failure_at"], str)

    def test_pending_reconciliation_does_not_replay_codex(self):
        with self.assertRaises(bridge_common.WorkerError):
            self.run_worker(fail_report=True)
        with patch.object(bw, "codex_run") as codex:
            self.assertEqual(
                bw.reconcile_pending_reports(self.root, project_id="p"),
                0,
            )
        codex.assert_not_called()
        metadata = json.loads(
            (
                self.root
                / "worker"
                / "runtime"
                / "p"
                / "pending-report-014.meta.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["journal_status"], "conflict")

    def test_network_recovery_keeps_phased_evidence_without_compaction(self):
        run_id = "run-014-recovery"
        run_dir = self.root / "worker" / "runtime" / "p" / "runs" / run_id
        phased_task.prepare_runtime(
            run_dir,
            self.command_path.read_bytes(),
            phased_task.ExecutionIdentity(
                project_id="p",
                command_id=14,
                run_id=run_id,
                claim_generation=41,
            ),
            bridge_root=self.root,
        )
        running = dict(self.initial_state)
        running.update(
            {
                "status": "CODEX_RUNNING",
                "generation": 41,
                "active_run": {
                    "run_id": run_id,
                    "command_id": 14,
                    "claimed_generation": 41,
                    "source": "scheduled_chatgpt",
                },
            }
        )
        self.state_path.write_text(json.dumps(running), encoding="utf-8")

        def publish(**kwargs):
            current = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.assertTrue(kwargs["expected"](current))
            payloads = kwargs["payload_builder"](current)
            updated = json.loads(payloads[self.state_path])
            self.state_path.write_text(json.dumps(updated), encoding="utf-8")
            return updated

        with patch.object(bw, "publish_cas", side_effect=publish):
            result = bw.publish_network_recovery(
                bridge_root=self.root,
                state_path=self.state_path,
                project_id="p",
                command_id=14,
                run_id=run_id,
                claim_generation=41,
                reason="network guard interruption",
                report_text="recovery report",
                runtime_dir=self.root / "worker" / "runtime" / "p",
                workdir=self.root,
                report_path=self.project / "reports" / "report-014.md",
                run_result=run_result("FAILED"),
            )
        self.assertTrue(result["remote_recovery_cas_success"])
        journal = bw.recovery_journal.read_journal(
            self.root / "worker" / "runtime" / "p" / "recovery" / f"{run_id}.json"
        )
        self.assertEqual(journal["journal_status"], "reconciled")
        self.assertEqual(json.loads(self.state_path.read_text())["status"], "RECOVERY_REQUIRED")
        for name in (
            "task-snapshot.md",
            "task-manifest.json",
            "global.md",
            "current-phase.md",
            "phase-progress.json",
        ):
            self.assertTrue((run_dir / name).exists())
        self.assertTrue(
            (self.root / "worker" / "runtime" / "p" / "pending-report-014.md").exists()
        )




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
