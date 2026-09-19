import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import command_repair as cr
import protocol_core as pc


ROOT = Path(__file__).resolve().parents[1]


def ready_state(
    *,
    generation=1,
    latest_command=1,
    latest_report=0,
    status="COMMAND_READY",
    active_run=None,
):
    return {
        "protocol_version": 2,
        "project_id": "p",
        "status": status,
        "generation": generation,
        "latest_command": latest_command,
        "latest_report": latest_report,
        "last_reviewed_report": latest_report,
        "active_run": active_run,
        "finalized": False,
        "human_required": False,
    }


def command_meta(**updates):
    value = {
        "command_id": 1,
        "source": "manual_chatgpt",
        "based_on_report": 0,
        "expected_generation": 0,
        "kind": "EXECUTE",
    }
    value.update(updates)
    return value


def command_text(meta, body="# Command\n"):
    rendered = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- bridge-command: {rendered} -->\n{body}"


def replacement_meta(**updates):
    value = {
        "command_id": 2,
        "source": "manual_chatgpt",
        "based_on_report": 0,
        "expected_generation": 2,
        "kind": "EXECUTE",
        "supersedes_command_id": 1,
    }
    value.update(updates)
    return value


class ProtocolCoreRepairTests(unittest.TestCase):
    def assess(self, *, state=None, target=None, replacement=None, existing=(1,), **kwargs):
        return pc.assess_unclaimed_command_repair(
            state=state or ready_state(),
            target_command_id=1,
            target_meta=target if target is not None else command_meta(),
            target_filename_command_id=1,
            replacement_meta=replacement,
            replacement_filename_command_id=(
                replacement.get("command_id") if replacement is not None else None
            ),
            existing_command_ids=existing,
            **kwargs,
        )

    def test_malformed_expected_generation_is_repairable(self):
        result = self.assess()
        self.assertTrue(result.eligible)
        self.assertIn("expected_generation must be >= 1", result.target_errors)
        self.assertIn(
            "target expected_generation must equal canonical generation",
            result.target_errors,
        )

    def test_valid_command_ready_is_not_repairable(self):
        result = self.assess(target=command_meta(expected_generation=1))
        self.assertFalse(result.eligible)
        self.assertIn(
            "target command has no deterministic Protocol-v2 contract error",
            result.errors,
        )

    def test_codex_running_is_rejected(self):
        result = self.assess(
            state=ready_state(
                generation=2,
                status="CODEX_RUNNING",
                active_run={"run_id": "run-001", "command_id": 1, "claimed_generation": 2},
            )
        )
        self.assertFalse(result.eligible)
        self.assertIn("canonical status must be COMMAND_READY", result.state_errors)

    def test_active_run_non_null_is_rejected(self):
        result = self.assess(
            state=ready_state(
                active_run={"run_id": "run-001", "command_id": 1, "claimed_generation": 1}
            )
        )
        self.assertFalse(result.eligible)
        self.assertIn("canonical active_run must be null", result.state_errors)

    def test_target_must_equal_canonical_latest_command(self):
        result = pc.assess_unclaimed_command_repair(
            state=ready_state(latest_command=2),
            target_command_id=1,
            target_meta=command_meta(),
            target_filename_command_id=1,
        )
        self.assertFalse(result.eligible)
        self.assertIn(
            "repair target command id must equal canonical latest_command",
            result.state_errors,
        )

    def test_replacement_expected_generation_must_be_next(self):
        result = self.assess(replacement=replacement_meta(expected_generation=3))
        self.assertFalse(result.eligible)
        self.assertIn(
            "replacement expected_generation must equal canonical generation + 1",
            result.replacement_errors,
        )

    def test_replacement_based_on_report_must_match(self):
        result = self.assess(replacement=replacement_meta(based_on_report=4))
        self.assertFalse(result.eligible)
        self.assertIn(
            "replacement based_on_report must equal canonical latest_report",
            result.replacement_errors,
        )

    def test_replacement_must_supersede_exact_target(self):
        result = self.assess(replacement=replacement_meta(supersedes_command_id=9))
        self.assertFalse(result.eligible)
        self.assertIn(
            "replacement supersedes_command_id must equal the exact target command id",
            result.replacement_errors,
        )

    def test_replacement_id_must_be_monotonic(self):
        result = self.assess(
            replacement=replacement_meta(command_id=1),
            existing=(1,),
        )
        self.assertFalse(result.eligible)
        self.assertTrue(
            any("greater than every existing command id" in error for error in result.replacement_errors)
        )

    def test_replacement_preserves_readable_source_and_kind(self):
        result = self.assess(
            replacement=replacement_meta(source="user_direct", kind="FINALIZE")
        )
        self.assertFalse(result.eligible)
        self.assertIn(
            "replacement source must preserve the target command source",
            result.replacement_errors,
        )
        self.assertIn(
            "replacement kind must preserve the target command kind",
            result.replacement_errors,
        )

    def test_repair_rejects_valid_target_before_store(self):
        with patch.object(cr.git_store, "publish_cas", side_effect=AssertionError("must not publish")):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = root / "projects" / "p"
                (project / "commands").mkdir(parents=True)
                (project / "reports").mkdir()
                (project / "state.json").write_text(
                    json.dumps(ready_state(), indent=2) + "\n", encoding="utf-8"
                )
                (project / "commands" / "command-001.md").write_text(
                    command_text(command_meta(expected_generation=1)),
                    encoding="utf-8",
                )
                replacement = root / "command-002.md"
                replacement.write_text(command_text(replacement_meta()), encoding="utf-8")
                with self.assertRaises(cr.CommandRepairError):
                    cr.repair_unclaimed_command(
                        bridge_root=root,
                        project_id="p",
                        command_id=1,
                        replacement_command=replacement,
                    )


class CommandRepairFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-command-repair-")
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "p"
        (self.project / "commands").mkdir(parents=True)
        (self.project / "reports").mkdir()
        self.state_path = self.project / "state.json"
        self.command_path = self.project / "commands" / "command-001.md"
        self.state_path.write_text(
            json.dumps(ready_state(), indent=2) + "\n", encoding="utf-8"
        )
        self.command_path.write_text(command_text(command_meta()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def replacement_file(self, command_id=2, **updates):
        path = self.root / f"command-{command_id:03d}.md"
        path.write_text(
            command_text(replacement_meta(command_id=command_id, **updates)),
            encoding="utf-8",
        )
        return path

    def test_inspect_is_read_only_and_reports_deterministic_errors(self):
        before = self.command_path.read_bytes()
        evidence = cr.inspect_command_repair(
            bridge_root=self.root,
            project_id="p",
            command_id=1,
        )
        self.assertTrue(evidence.assessment.eligible)
        self.assertEqual(evidence.state["status"], "COMMAND_READY")
        self.assertFalse(evidence.state["active_run"] is not None)
        self.assertEqual(self.command_path.read_bytes(), before)
        self.assertFalse((self.project / "commands" / "command-002.md").exists())

    def test_successful_repair_preserves_old_bytes_and_advances_once(self):
        self._init_remote()
        before = self.command_path.read_bytes()
        replacement = self.replacement_file()
        result = cr.repair_unclaimed_command(
            bridge_root=self.root,
            project_id="p",
            command_id=1,
            replacement_command=replacement,
        )
        self.assertEqual(result["result"], "REPAIRED")
        self.assertEqual(self.command_path.read_bytes(), before)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "COMMAND_READY")
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["latest_command"], 2)
        self.assertEqual(state["latest_report"], 0)
        self.assertIsNone(state["active_run"])
        self.assertTrue((self.project / "commands" / "command-002.md").exists())
        self.assertFalse((self.project / "reports" / "report-001.md").exists())
        self.assertFalse((self.project / "reports" / "report-002.md").exists())
        self.assertFalse((self.root / "worker" / "runtime" / "p" / "recovery").exists())
        self.assertNotIn("executor", cr.__dict__)

    def test_cas_race_aborts_without_overwriting_or_publishing_replacement(self):
        self._init_remote()
        competitor = self._clone_competitor()
        before = self.command_path.read_bytes()
        replacement = self.replacement_file()
        original_git = cr.git_store.git
        raced = False

        def race(repository, *args, **kwargs):
            nonlocal raced
            if args and args[0] == "push" and not raced:
                raced = True
                state = json.loads(
                    (competitor / "projects" / "p" / "state.json").read_text(
                        encoding="utf-8"
                    )
                )
                state["status"] = "REPORT_READY"
                state["generation"] = 2
                (competitor / "projects" / "p" / "state.json").write_text(
                    json.dumps(state, indent=2) + "\n", encoding="utf-8"
                )
                self._git(competitor, "add", "projects/p/state.json")
                self._git(competitor, "commit", "-m", "canonical race")
                self._git(competitor, "push", "origin", "HEAD:main")
            return original_git(repository, *args, **kwargs)

        with patch.object(cr.git_store, "git", side_effect=race):
            with self.assertRaises(cr.CommandRepairConflict):
                cr.repair_unclaimed_command(
                    bridge_root=self.root,
                    project_id="p",
                    command_id=1,
                    replacement_command=replacement,
                )

        self.assertTrue(raced)
        self.assertEqual(self.command_path.read_bytes(), before)
        self.assertFalse((self.project / "commands" / "command-002.md").exists())
        self._git(self.root, "fetch", "origin")
        self._git(self.root, "reset", "--hard", "origin/main")
        final_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["status"], "REPORT_READY")
        self.assertEqual(final_state["generation"], 2)

    def _init_remote(self):
        remote = self.root / "remote.git"
        self._git(self.root, "init", "--bare", str(remote))
        self._git(self.root, "init", "--initial-branch=main")
        self._git(self.root, "config", "user.name", "Repair Test")
        self._git(self.root, "config", "user.email", "repair@example.invalid")
        self._git(self.root, "remote", "add", "origin", str(remote))
        self._git(self.root, "add", "projects/p/state.json", "projects/p/commands/command-001.md")
        self._git(self.root, "commit", "-m", "initial state")
        self._git(self.root, "push", "-u", "origin", "main")

    def _clone_competitor(self):
        competitor = self.root.parent / f"{self.root.name}-competitor"
        self._git(
            self.root.parent,
            "clone",
            "--branch",
            "main",
            str(self.root / "remote.git"),
            str(competitor),
        )
        self._git(competitor, "config", "user.name", "Competitor")
        self._git(competitor, "config", "user.email", "competitor@example.invalid")
        return competitor

    @staticmethod
    def _git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(
                f"git {' '.join(args)} failed: {result.stdout}\n{result.stderr}"
            )
        return result


class StagedCommandRepairFilesystemTests(unittest.TestCase):
    """Git-backed tests for adopting an already-written replacement command."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-staged-repair-")
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "p"
        (self.project / "commands").mkdir(parents=True)
        (self.project / "reports").mkdir()
        self.state_path = self.project / "state.json"
        self.target_path = self.project / "commands" / "command-009.md"
        self.replacement_path = self.project / "commands" / "command-010.md"
        self._write_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def _write_fixture(self, *, state=None, target=None, replacement=None):
        self.state_path.write_text(
            json.dumps(
                state
                or ready_state(
                    generation=26,
                    latest_command=9,
                    latest_report=8,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.target_path.write_text(
            command_text(
                target
                or command_meta(
                    command_id=9,
                    source="scheduled_chatgpt",
                    based_on_report=8,
                    expected_generation=25,
                ),
                "# Command 009\n",
            ),
            encoding="utf-8",
        )
        self.replacement_path.write_text(
            command_text(
                replacement
                or replacement_meta(
                    command_id=10,
                    source="manual_chatgpt",
                    based_on_report=8,
                    expected_generation=27,
                    supersedes_command_id=9,
                ),
                "# Command 010\n",
            ),
            encoding="utf-8",
        )

    def adopt(self):
        return cr.adopt_staged_repair(
            bridge_root=self.root,
            project_id="p",
            command_id=9,
            replacement_command_id=10,
        )

    def _init_remote(self):
        remote = self.root / "remote.git"
        self._git(self.root, "init", "--bare", str(remote))
        self._git(self.root, "init", "--initial-branch=main")
        self._git(self.root, "config", "user.name", "Staged Repair Test")
        self._git(self.root, "config", "user.email", "staged-repair@example.invalid")
        self._git(self.root, "remote", "add", "origin", str(remote))
        self._git(
            self.root,
            "add",
            "projects/p/state.json",
            "projects/p/commands/command-009.md",
            "projects/p/commands/command-010.md",
        )
        self._git(self.root, "commit", "-m", "initial staged repair state")
        self._git(self.root, "push", "-u", "origin", "main")

    def _clone_competitor(self):
        competitor = self.root.parent / f"{self.root.name}-competitor"
        self._git(
            self.root.parent,
            "clone",
            "--branch",
            "main",
            str(self.root / "remote.git"),
            str(competitor),
        )
        self._git(competitor, "config", "user.name", "Staged Competitor")
        self._git(
            competitor,
            "config",
            "user.email",
            "staged-competitor@example.invalid",
        )
        return competitor

    @staticmethod
    def _git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(
                f"git {' '.join(args)} failed: {result.stdout}\n{result.stderr}"
            )
        return result

    def _assert_rejected_before_cas(self):
        self._init_remote()
        with patch.object(
            cr.git_store,
            "publish_cas",
            side_effect=AssertionError("staged guard must fail before CAS"),
        ) as publish:
            with self.assertRaises(cr.CommandRepairError):
                self.adopt()
        publish.assert_not_called()

    def _run_remote_race(self, mutate):
        snapshots = {
            "target": self.target_path.read_bytes(),
            "replacement": self.replacement_path.read_bytes(),
            "state": self.state_path.read_bytes(),
        }
        self._init_remote()
        competitor = self._clone_competitor()
        original_git = cr.git_store.git
        raced = False

        def race(repository, *args, **kwargs):
            nonlocal raced
            if args and args[0] == "push" and not raced:
                raced = True
                self._git(competitor, "fetch", "origin")
                self._git(competitor, "reset", "--hard", "origin/main")
                mutate(competitor)
                changed = [
                    str(path.relative_to(competitor)).replace("\\", "/")
                    for path in competitor.glob("projects/p/**/*.md")
                    if path.is_file()
                ]
                if (competitor / "projects" / "p" / "state.json").read_bytes():
                    changed.append("projects/p/state.json")
                self._git(competitor, "add", "--", *changed)
                self._git(competitor, "commit", "-m", "staged repair race")
                self._git(competitor, "push", "origin", "HEAD:main")
            return original_git(repository, *args, **kwargs)

        with patch.object(cr.git_store, "git", side_effect=race):
            with self.assertRaises(cr.CommandRepairConflict):
                self.adopt()
        self.assertTrue(raced)
        self._git(self.root, "fetch", "origin")
        self._git(self.root, "reset", "--hard", "origin/main")
        return competitor, snapshots

    def test_happy_path_adopts_staged_009_to_010_and_writes_only_state(self):
        target_before = self.target_path.read_bytes()
        replacement_before = self.replacement_path.read_bytes()
        self._init_remote()

        result = self.adopt()

        self.assertEqual(result["event"], "operator.adopt_staged_repair")
        self.assertEqual(result["result"], "ADOPTED")
        self.assertEqual(result["generation_before"], 26)
        self.assertEqual(result["generation_after"], 27)
        self.assertEqual(result["latest_command"], 10)
        self.assertEqual(result["latest_report"], 8)
        self.assertFalse(result["active_run_present"])
        self.assertFalse(result["run_created"])
        self.assertFalse(result["lease_created"])
        self.assertFalse(result["report_created"])
        self.assertFalse(result["recovery_created"])
        self.assertFalse(result["executor_invoked"])
        self.assertTrue(result["command_bytes_unchanged"])
        self.assertEqual(self.target_path.read_bytes(), target_before)
        self.assertEqual(self.replacement_path.read_bytes(), replacement_before)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "COMMAND_READY")
        self.assertEqual(state["generation"], 27)
        self.assertEqual(state["latest_command"], 10)
        self.assertEqual(state["latest_report"], 8)
        self.assertIsNone(state["active_run"])
        self.assertNotIn("run_id", state)
        self.assertFalse((self.project / "reports" / "report-009.md").exists())
        self.assertFalse((self.project / "reports" / "report-010.md").exists())
        self.assertFalse(
            (self.root / "worker" / "runtime" / "p" / "recovery").exists()
        )
        changed_paths = self._git(
            self.root,
            "show",
            "--format=",
            "--name-only",
            "HEAD",
        ).stdout.splitlines()
        self.assertEqual(changed_paths, ["projects/p/state.json"])

    def test_active_run_non_null_is_rejected(self):
        state = ready_state(
            generation=26,
            latest_command=9,
            latest_report=8,
            active_run={
                "run_id": "run-009",
                "command_id": 9,
                "claimed_generation": 26,
            },
        )
        self._write_fixture(state=state)
        self._assert_rejected_before_cas()

    def test_non_command_ready_status_is_rejected(self):
        self._write_fixture(
            state=ready_state(
                generation=26,
                latest_command=9,
                latest_report=8,
                status="REPORT_READY",
            )
        )
        self._assert_rejected_before_cas()

    def test_generation_change_is_rejected(self):
        self._write_fixture(
            state=ready_state(
                generation=27,
                latest_command=9,
                latest_report=8,
            )
        )
        self._assert_rejected_before_cas()

    def test_target_report_is_rejected(self):
        (self.project / "reports" / "report-009.md").write_text(
            "report exists\n", encoding="utf-8"
        )
        self._assert_rejected_before_cas()

    def test_replacement_report_is_rejected(self):
        (self.project / "reports" / "report-010.md").write_text(
            "report exists\n", encoding="utf-8"
        )
        self._assert_rejected_before_cas()

    def test_replacement_expected_generation_is_rejected(self):
        self._write_fixture(
            replacement=replacement_meta(
                command_id=10,
                source="manual_chatgpt",
                based_on_report=8,
                expected_generation=26,
                supersedes_command_id=9,
            )
        )
        self._assert_rejected_before_cas()

    def test_replacement_based_on_report_is_rejected(self):
        self._write_fixture(
            replacement=replacement_meta(
                command_id=10,
                source="manual_chatgpt",
                based_on_report=7,
                expected_generation=27,
                supersedes_command_id=9,
            )
        )
        self._assert_rejected_before_cas()

    def test_replacement_supersedes_id_is_rejected(self):
        self._write_fixture(
            replacement=replacement_meta(
                command_id=10,
                source="manual_chatgpt",
                based_on_report=8,
                expected_generation=27,
                supersedes_command_id=8,
            )
        )
        self._assert_rejected_before_cas()

    def test_staged_replacement_id_must_be_newer_than_all_other_history(self):
        (self.project / "commands" / "command-011.md").write_text(
            command_text(
                command_meta(
                    command_id=11,
                    source="manual_chatgpt",
                    based_on_report=8,
                    expected_generation=27,
                ),
                "# Command 011\n",
            ),
            encoding="utf-8",
        )
        self._assert_rejected_before_cas()

    def test_staged_replacement_must_pass_complete_command_contract(self):
        self.replacement_path.write_text(
            command_text(
                {
                    "command_id": 10,
                    "source": "manual_chatgpt",
                    "based_on_report": 8,
                    "expected_generation": 27,
                },
                "# Command 010\n",
            ),
            encoding="utf-8",
        )
        self._assert_rejected_before_cas()

    def test_self_supersede_relation_is_rejected(self):
        self._write_fixture(
            replacement=replacement_meta(
                command_id=10,
                source="manual_chatgpt",
                based_on_report=8,
                expected_generation=27,
                supersedes_command_id=10,
            )
        )
        self._assert_rejected_before_cas()

    def test_future_supersede_relation_is_rejected(self):
        self._write_fixture(
            replacement=replacement_meta(
                command_id=10,
                source="manual_chatgpt",
                based_on_report=8,
                expected_generation=27,
                supersedes_command_id=11,
            )
        )
        self._assert_rejected_before_cas()

    def test_cycle_supersede_relation_is_rejected(self):
        self._write_fixture(
            target=command_meta(
                command_id=9,
                source="scheduled_chatgpt",
                based_on_report=8,
                expected_generation=25,
                supersedes_command_id=10,
            )
        )
        self._assert_rejected_before_cas()

    def test_target_sha_change_race_is_rejected(self):
        def mutate(competitor):
            path = competitor / "projects" / "p" / "commands" / "command-009.md"
            path.write_bytes(path.read_bytes() + b"\n# target race\n")

        competitor, snapshots = self._run_remote_race(mutate)
        self.assertNotEqual(
            (competitor / "projects" / "p" / "commands" / "command-009.md").read_bytes(),
            snapshots["target"],
        )

    def test_replacement_sha_and_bytes_change_race_is_rejected(self):
        def mutate(competitor):
            path = competitor / "projects" / "p" / "commands" / "command-010.md"
            path.write_bytes(path.read_bytes() + b"\n# replacement race\n")

        competitor, snapshots = self._run_remote_race(mutate)
        self.assertNotEqual(
            (competitor / "projects" / "p" / "commands" / "command-010.md").read_bytes(),
            snapshots["replacement"],
        )

    def test_semantically_same_state_with_different_sha_race_is_rejected(self):
        def mutate(competitor):
            path = competitor / "projects" / "p" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps(state, indent=4) + "\n", encoding="utf-8")

        competitor, snapshots = self._run_remote_race(mutate)
        self.assertNotEqual(
            (competitor / "projects" / "p" / "state.json").read_bytes(),
            snapshots["state"],
        )

    def test_cas_state_race_is_rejected_without_replacement_publication(self):
        def mutate(competitor):
            path = competitor / "projects" / "p" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["generation"] = 27
            path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        self._run_remote_race(mutate)
        final_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["generation"], 27)
        self.assertEqual(final_state["latest_command"], 9)


class RepairedHistoryConformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-conformance-repair-")
        self.root = Path(self.temp.name)
        from test_state_fixture import make_state
        make_state(self.root, git=False)

    def tearDown(self):
        self.temp.cleanup()

    def run_conformance(self):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "protocol" / "v2" / "check_conformance.py"),
                "--state-root", str(self.root),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def write_project(self, commands, *, latest_command, generation, latest_report=0):
        project = self.root / "projects" / "p"
        (project / "commands").mkdir(parents=True, exist_ok=True)
        (project / "reports").mkdir(parents=True, exist_ok=True)
        (project / "state.json").write_text(
            json.dumps(
                ready_state(
                    generation=generation,
                    latest_command=latest_command,
                    latest_report=latest_report,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for command_id, meta in commands.items():
            (project / "commands" / f"command-{command_id:03d}.md").write_text(
                command_text(meta, f"# Command {command_id:03d}\n"),
                encoding="utf-8",
            )
        if latest_report:
            (project / "reports" / f"report-{latest_report:03d}.md").write_text(
                f"- command_id: {latest_report}\n",
                encoding="utf-8",
            )

    def test_unsuperseded_invalid_historical_command_fails(self):
        self.write_project({1: command_meta()}, latest_command=1, generation=1)
        result = self.run_conformance()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected_generation must be >= 1", result.stdout)

    def test_valid_superseder_allows_only_its_old_invalid_command(self):
        self.write_project(
            {
                1: command_meta(),
                2: replacement_meta(),
            },
            latest_command=2,
            generation=2,
        )
        result = self.run_conformance()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("invalid command retained as immutable history", result.stdout)

    def test_staged_manual_superseder_history_is_conformant(self):
        self.write_project(
            {
                9: command_meta(
                    command_id=9,
                    source="scheduled_chatgpt",
                    based_on_report=8,
                    expected_generation=25,
                ),
                10: replacement_meta(
                    command_id=10,
                    source="manual_chatgpt",
                    based_on_report=8,
                    expected_generation=27,
                    supersedes_command_id=9,
                ),
            },
            latest_command=10,
            latest_report=8,
            generation=27,
        )
        result = self.run_conformance()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unrelated_invalid_historical_command_still_fails(self):
        self.write_project(
            {
                1: command_meta(),
                2: replacement_meta(),
                3: command_meta(command_id=3, expected_generation=3, source="unsupported"),
            },
            latest_command=3,
            generation=3,
        )
        result = self.run_conformance()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("command-003.md", result.stdout)

    def test_duplicate_conflicting_superseders_fail(self):
        self.write_project(
            {
                1: command_meta(),
                2: replacement_meta(),
                3: replacement_meta(command_id=3, expected_generation=3),
            },
            latest_command=3,
            generation=3,
        )
        result = self.run_conformance()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple conflicting superseders", result.stdout)

    def test_self_supersede_fails(self):
        self.write_project(
            {1: command_meta(supersedes_command_id=1)},
            latest_command=1,
            generation=1,
        )
        result = self.run_conformance()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not self-reference", result.stdout)

    def test_supersede_cycle_fails(self):
        self.write_project(
            {
                1: command_meta(supersedes_command_id=2, expected_generation=1),
                2: replacement_meta(supersedes_command_id=1),
            },
            latest_command=2,
            generation=2,
        )
        result = self.run_conformance()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("supersede relation contains a cycle", result.stdout)

    def test_malformed_bridge_marker_is_not_reclassified_as_legacy(self):
        self.write_project(
            {1: command_meta()},
            latest_command=1,
            generation=1,
        )
        command = self.root / "projects" / "p" / "commands" / "command-001.md"
        command.write_text(
            "<!-- bridge-command: not-json -->\n# Broken\n",
            encoding="utf-8",
        )
        result = self.run_conformance()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lacks bridge-command metadata", result.stdout)

    def _copy_conformance_runtime(self):
        for relative in (
            "protocol/v2/check_conformance.py",
            "protocol/v2/state.schema.json",
            "protocol/v2/command.schema.json",
            "protocol/v2/owner-action.schema.json",
            "protocol/v2/transitions.json",
            "worker/protocol_core.py",
        ):
            source = ROOT / relative
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


if __name__ == "__main__":
    unittest.main()
