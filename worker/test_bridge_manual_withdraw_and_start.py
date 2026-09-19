import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bridge_common as common
import bridge_manual as bm
import protocol_core as pc


class AtomicWithdrawAndStartFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="bridge-withdraw-and-start-fixture-"
        )
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "example-device"
        self.commands = self.project / "commands"
        self.reports = self.project / "reports"
        self.commands.mkdir(parents=True)
        self.reports.mkdir(parents=True)
        self.state_path = self.project / "state.json"
        self.state = {
            "protocol_version": 2,
            "project_id": "example-device",
            "status": "COMMAND_READY",
            "generation": 10,
            "latest_command": 1,
            "latest_report": 0,
            "last_reviewed_report": 0,
            "active_run": None,
            "finalized": False,
            "human_required": False,
        }
        self.state_path.write_text(common.json_text(self.state), encoding="utf-8")
        self.target_path = self.commands / "command-001.md"
        self.target_text = (
            '<!-- bridge-command: {"command_id":1,"source":"manual_chatgpt",'
            '"based_on_report":0,"expected_generation":10,"kind":"EXECUTE"} -->\n'
            "# Command 001 — synthetic owner request\n\nOriginal owner request.\n"
        )
        self.target_path.write_text(self.target_text, encoding="utf-8")
        self.target_bytes_before = self.target_path.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def _clone(self):
        clone = MagicMock()
        clone.__enter__.return_value = self.root
        clone.__exit__.return_value = False
        return clone

    def _fake_publish(self, calls, *, race=None):
        def publish(**kwargs):
            calls.append(kwargs)
            if race is not None:
                race()
            current = common.load_json(kwargs["state_path"])
            if not kwargs["expected"](current):
                raise bm.CASConflict("fixture CAS lost race")
            payloads = kwargs["payload_builder"](current)
            self.assertEqual(set(payloads), {
                self.project / "commands" / "command-002.md",
                self.state_path,
            })
            for path, text in payloads.items():
                path.write_text(text, encoding="utf-8")
            return common.load_json(kwargs["state_path"])

        return publish

    def _invoke(self, *, publish=None, target_id=1):
        calls = []
        if publish is None:
            publish = self._fake_publish(calls)
        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()), patch.object(
            bm.git_store, "publish_cas", side_effect=publish
        ):
            result = bm.withdraw_and_manual_start(
                source_root=self.root,
                project_id="example-device",
                target_command_id=target_id,
                body=(
                    "Implement the updated owner-approved request.\n\n"
                    "Keep the original command immutable."
                ),
                reason="owner_withdrawn / requirements_changed",
                lease_hours=6,
            )
        return result, calls

    def test_happy_path_is_one_cas_commit_and_never_report_ready(self):
        result, calls = self._invoke()

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["event"], "operator.withdraw_and_manual_start")
        self.assertEqual(result["replacement_command_id"], 2)
        self.assertEqual(result["generation_before"], 10)
        self.assertEqual(result["publication_generation"], 11)
        self.assertEqual(result["generation_after"], 12)
        self.assertEqual(result["status"], "CODEX_RUNNING")
        self.assertFalse(result["report_created"])
        self.assertFalse(result["already_applied"])

        final_state = common.load_json(self.state_path)
        self.assertEqual(final_state["status"], "CODEX_RUNNING")
        self.assertEqual(final_state["generation"], 12)
        self.assertEqual(final_state["latest_command"], 2)
        self.assertEqual(final_state["latest_report"], 0)
        self.assertIsInstance(final_state["active_run"], dict)
        self.assertEqual(final_state["active_run"]["command_id"], 2)
        self.assertEqual(final_state["active_run"]["claimed_generation"], 12)
        self.assertEqual(final_state["active_run"]["withdraws_command_id"], 1)
        self.assertEqual(len(final_state["withdrawn_commands"]), 1)
        record = final_state["withdrawn_commands"][0]
        self.assertEqual(record["command_id"], 1)
        self.assertEqual(record["replacement_command_id"], 2)
        self.assertEqual(record["resolution"], bm.WITHDRAWAL_RESOLUTION)
        self.assertEqual(record["command_sha256"], result["target_command_sha256"])

        self.assertEqual(self.target_path.read_bytes(), self.target_bytes_before)
        replacement_path = self.commands / "command-002.md"
        replacement_meta = pc.parse_command_metadata(
            replacement_path.read_text(encoding="utf-8")
        )
        self.assertEqual(replacement_meta["withdraws_command_id"], 1)
        self.assertNotIn("supersedes_command_id", replacement_meta)
        self.assertFalse((self.reports / "report-001.md").exists())
        self.assertFalse((self.reports / "report-002.md").exists())

    def test_active_run_is_rejected(self):
        self.state["active_run"] = {"run_id": "already-running"}
        self.state_path.write_text(common.json_text(self.state), encoding="utf-8")
        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()):
            with self.assertRaises(bm.CASConflict):
                bm.withdraw_and_manual_start(
                    source_root=self.root,
                    project_id="example-device",
                    target_command_id=1,
                    body="replacement",
                    reason="owner_withdrawn / requirements_changed",
                    lease_hours=6,
                )

    def test_report_for_target_is_rejected(self):
        (self.reports / "report-001.md").write_text("old report", encoding="utf-8")
        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()):
            with self.assertRaises(bm.CASConflict):
                bm.withdraw_and_manual_start(
                    source_root=self.root,
                    project_id="example-device",
                    target_command_id=1,
                    body="replacement",
                    reason="owner_withdrawn / requirements_changed",
                    lease_hours=6,
                )

    def test_generation_and_target_identity_are_rechecked_before_payload(self):
        def mutate_state():
            changed = dict(self.state)
            changed["generation"] = 11
            self.state_path.write_text(common.json_text(changed), encoding="utf-8")

        calls = []
        publish = self._fake_publish(calls, race=mutate_state)
        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()), patch.object(
            bm.git_store, "publish_cas", side_effect=publish
        ):
            with self.assertRaises(bm.CASConflict):
                bm.withdraw_and_manual_start(
                    source_root=self.root,
                    project_id="example-device",
                    target_command_id=1,
                    body="replacement",
                    reason="owner_withdrawn / requirements_changed",
                    lease_hours=6,
                )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.target_path.read_text(encoding="utf-8"), self.target_text)

    def test_state_sha_change_with_same_semantics_is_fail_closed(self):
        def mutate_state():
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )

        calls = []
        publish = self._fake_publish(calls, race=mutate_state)
        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()), patch.object(
            bm.git_store, "publish_cas", side_effect=publish
        ):
            with self.assertRaises(bm.CASConflict):
                bm.withdraw_and_manual_start(
                    source_root=self.root,
                    project_id="example-device",
                    target_command_id=1,
                    body="replacement",
                    reason="owner_withdrawn / requirements_changed",
                    lease_hours=6,
                )

    def test_command_sha_change_is_fail_closed(self):
        def mutate_command():
            self.target_path.write_text(self.target_text + "\nchanged", encoding="utf-8")

        calls = []
        publish = self._fake_publish(calls, race=mutate_command)
        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()), patch.object(
            bm.git_store, "publish_cas", side_effect=publish
        ):
            with self.assertRaises(bm.CASConflict):
                bm.withdraw_and_manual_start(
                    source_root=self.root,
                    project_id="example-device",
                    target_command_id=1,
                    body="replacement",
                    reason="owner_withdrawn / requirements_changed",
                    lease_hours=6,
                )
        self.assertNotEqual(self.target_path.read_bytes(), self.target_bytes_before)
        self.assertFalse((self.commands / "command-002.md").exists())

    def test_cas_lost_race_is_rejected_without_publishing_replacement(self):
        calls = []

        def lost_race(**_kwargs):
            calls.append(1)
            raise bm.CASConflict("fixture remote CAS lost race")

        with patch.object(bm, "TemporaryBridgeClone", return_value=self._clone()), patch.object(
            bm.git_store, "publish_cas", side_effect=lost_race
        ):
            with self.assertRaises(bm.CASConflict):
                bm.withdraw_and_manual_start(
                    source_root=self.root,
                    project_id="example-device",
                    target_command_id=1,
                    body="replacement",
                    reason="owner_withdrawn / requirements_changed",
                    lease_hours=6,
                )
        self.assertEqual(calls, [1])
        self.assertEqual(self.target_path.read_bytes(), self.target_bytes_before)
        self.assertFalse((self.commands / "command-002.md").exists())
class AtomicWithdrawAndStartGitIntegrationTests(unittest.TestCase):
    """Exercise the real temporary-clone/CAS/push path against a fixture remote."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-withdraw-git-fixture-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.observer = self.root / "observer"
        self._git(self.root, "init", "--bare", str(self.remote))
        self._git(self.root, "clone", str(self.remote), str(self.source))
        self._configure(self.source, "Fixture Source", "source@example.test")
        project = self.source / "projects" / "p"
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir(parents=True)
        (project / "commands" / "command-001.md").write_text(
            '<!-- bridge-command: {"command_id":1,"source":"manual_chatgpt",'
            '"based_on_report":0,"expected_generation":10,"kind":"EXECUTE"} -->\n'
            "# Command 001 — synthetic owner request\n\nOriginal owner request.\n",
            encoding="utf-8",
        )
        (project / "state.json").write_text(
            common.json_text(
                {
                    "protocol_version": 2,
                    "project_id": "p",
                    "status": "COMMAND_READY",
                    "generation": 10,
                    "latest_command": 1,
                    "latest_report": 0,
                    "last_reviewed_report": 0,
                    "active_run": None,
                }
            ),
            encoding="utf-8",
        )
        self._git(self.source, "add", ".")
        self._git(self.source, "commit", "-m", "fixture command 001")
        self._git(self.source, "branch", "-M", "main")
        self._git(self.source, "push", "-u", "origin", "main")
        self._git(self.root, "clone", "--branch", "main", str(self.remote), str(self.observer))

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, cwd: Path, *args: str):
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed ({result.returncode})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _configure(self, repository: Path, name: str, email: str):
        self._git(repository, "config", "user.name", name)
        self._git(repository, "config", "user.email", email)

    def test_real_git_cas_commits_replacement_state_and_lease_together(self):
        result = bm.withdraw_and_manual_start(
            source_root=self.source,
            project_id="p",
            target_command_id=1,
            body="Fixture replacement body.",
            reason="owner_withdrawn / requirements_changed",
            lease_hours=6,
        )

        self._git(self.observer, "fetch", "origin")
        self._git(self.observer, "reset", "--hard", "origin/main")
        project = self.observer / "projects" / "p"
        final_state = json.loads((project / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "CODEX_RUNNING")
        self.assertEqual(final_state["generation"], 12)
        self.assertEqual(final_state["latest_command"], 2)
        self.assertIsNotNone(final_state["active_run"])
        self.assertEqual(len(final_state["withdrawn_commands"]), 1)
        self.assertEqual(final_state["withdrawn_commands"][0]["replacement_command_id"], 2)
        self.assertEqual(
            self._git(self.observer, "log", "-1", "--format=%s").stdout.strip(),
            "bridge: withdraw command 001 and manual claim 002 p",
        )
        self.assertFalse((project / "reports" / "report-001.md").exists())
        self.assertEqual(
            (project / "commands" / "command-001.md").read_bytes(),
            (self.source / "projects" / "p" / "commands" / "command-001.md").read_bytes(),
        )




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
