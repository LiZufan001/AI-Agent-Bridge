import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_common as common
import bridge_manual as bm
import bridge_worker as bw
from codex_lifecycle import CodexRunResult
import git_store
import protocol_core


class GitStoreIntegrationTests(unittest.TestCase):
    """Exercise the store against a real bare remote and multiple clones."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-git-store-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.worker = self.root / "worker"
        self.competitor = self.root / "competitor"
        self._git(self.root, "init", "--bare", str(self.remote))
        self._git(self.root, "clone", str(self.remote), str(self.worker))
        self._configure_identity(self.worker, "Integration Worker", "worker@example.test")

        project = self.worker / "projects" / "p"
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir(parents=True)
        (project / "commands" / ".keep").write_text("\n", encoding="utf-8")
        (project / "reports" / ".keep").write_text("\n", encoding="utf-8")
        (project / "MISSION.md").write_text("integration mission\n", encoding="utf-8")
        self.state_path = project / "state.json"
        self._write_state(
            {
                "protocol_version": 2,
                "project_id": "p",
                "status": "REPORT_READY",
                "generation": 10,
                "latest_command": 5,
                "latest_report": 5,
                "last_reviewed_report": 5,
                "active_run": None,
                "value": "base",
            }
        )
        self._git(self.worker, "add", ".")
        self._git(self.worker, "commit", "-m", "initial bridge state")
        self._git(self.worker, "branch", "-M", "main")
        self._git(self.worker, "push", "-u", "origin", "main")

        self._git(self.root, "clone", "--branch", "main", str(self.remote), str(self.competitor))
        self._configure_identity(
            self.competitor,
            "Integration Competitor",
            "competitor@example.test",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, cwd: Path, *args: str, check: bool = True):
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed ({result.returncode})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _configure_identity(self, repository: Path, name: str, email: str):
        self._git(repository, "config", "user.name", name)
        self._git(repository, "config", "user.email", email)

    def _write_state(self, state: dict):
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_state(self, repository: Path | None = None) -> dict:
        path = (repository or self.worker) / "projects" / "p" / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _commit_and_push(self, repository: Path, relative: str, message: str):
        self._git(repository, "add", "--", relative)
        self._git(repository, "commit", "-m", message)
        self._git(repository, "push", "origin", "HEAD:main")

    def _publication_payload(self, current: dict, *, marker: str = "published"):
        updated = dict(current)
        updated["generation"] = int(current["generation"]) + 1
        updated["value"] = marker
        return {self.state_path: common.json_text(updated)}

    def test_clean_sync_adopts_remote_advance(self):
        (self.competitor / "remote-only.txt").write_text("remote\n", encoding="utf-8")
        self._commit_and_push(self.competitor, "remote-only.txt", "remote advance")

        git_store.sync_to_remote(self.worker)

        self.assertTrue((self.worker / "remote-only.txt").is_file())
        self.assertEqual(
            self._git(self.worker, "rev-parse", "HEAD").stdout.strip(),
            self._git(self.competitor, "rev-parse", "HEAD").stdout.strip(),
        )

    def test_tracked_dirty_checkout_is_rejected_without_discard(self):
        tracked = self.worker / "projects" / "p" / "MISSION.md"
        tracked.write_text("local uncommitted edit\n", encoding="utf-8")

        with self.assertRaises(common.WorkerError):
            git_store.sync_to_remote(self.worker)

        self.assertEqual(tracked.read_text(encoding="utf-8"), "local uncommitted edit\n")
        self.assertTrue(git_store.tracked_dirty(self.worker))

    def test_normal_polling_refuses_to_discard_local_ahead_commit(self):
        ahead = self.worker / "local-ahead.txt"
        ahead.write_text("must survive\n", encoding="utf-8")
        self._git(self.worker, "add", "--", "local-ahead.txt")
        self._git(self.worker, "commit", "-m", "unpublished local work")
        local_head = self._git(self.worker, "rev-parse", "HEAD").stdout.strip()

        with self.assertRaises(common.WorkerError):
            git_store.sync_to_remote(self.worker)

        self.assertEqual(
            self._git(self.worker, "rev-parse", "HEAD").stdout.strip(), local_head
        )
        self.assertTrue(ahead.is_file())

    def test_already_applied_is_idempotent_before_expected_check(self):
        build_calls = []

        def build(current):
            build_calls.append(dict(current))
            return self._publication_payload(current)

        def expected(current):
            return current.get("generation") == 10

        def already(current):
            return current.get("value") == "published"

        first = git_store.publish_cas(
            bridge_root=self.worker,
            state_path=self.state_path,
            expected=expected,
            already_applied=already,
            payload_builder=build,
            message="bridge: integration publication",
        )
        head_after_first = self._git(self.worker, "rev-parse", "HEAD").stdout.strip()

        second = git_store.publish_cas(
            bridge_root=self.worker,
            state_path=self.state_path,
            expected=lambda _current: self.fail("stale expected predicate was evaluated"),
            already_applied=already,
            payload_builder=lambda _current: self.fail("idempotent publish rebuilt payload"),
            message="bridge: should not commit",
        )

        self.assertEqual(first["value"], "published")
        self.assertEqual(second, first)
        self.assertEqual(
            self._git(self.worker, "rev-parse", "HEAD").stdout.strip(), head_after_first
        )
        self.assertEqual(len(build_calls), 1)

    def test_stale_state_raises_cas_conflict_without_payload_write(self):
        competitor_state_path = self.competitor / "projects" / "p" / "state.json"
        competitor_state = self._read_state(self.competitor)
        competitor_state.update({"generation": 11, "value": "remote"})
        competitor_state_path.write_text(
            common.json_text(competitor_state), encoding="utf-8"
        )
        self._commit_and_push(
            self.competitor,
            "projects/p/state.json",
            "remote canonical advance",
        )
        payload_calls = []

        def build(current):
            payload_calls.append(current)
            return self._publication_payload(current)

        with self.assertRaises(common.CASConflict):
            git_store.publish_cas(
                bridge_root=self.worker,
                state_path=self.state_path,
                expected=lambda current: current.get("generation") == 10,
                already_applied=lambda _current: False,
                payload_builder=build,
                message="bridge: stale publication",
            )

        self.assertEqual(payload_calls, [])
        self.assertEqual(self._read_state()["value"], "remote")
        self.assertEqual(self._read_state()["generation"], 11)

    def test_payload_is_written_committed_and_pushed(self):
        report_path = self.worker / "projects" / "p" / "reports" / "report-006.md"

        def build(current):
            updated = dict(current)
            updated["generation"] = 11
            updated["latest_report"] = 6
            updated["value"] = "published"
            return {
                report_path: "# Report 006\n\nverified\n",
                self.state_path: common.json_text(updated),
            }

        result = git_store.publish_cas(
            bridge_root=self.worker,
            state_path=self.state_path,
            expected=lambda current: current.get("generation") == 10,
            already_applied=lambda current: current.get("latest_report") == 6,
            payload_builder=build,
            message="bridge: report p command 006",
        )
        self._git(self.competitor, "fetch", "origin")
        self._git(self.competitor, "reset", "--hard", "origin/main")

        self.assertEqual(result["latest_report"], 6)
        self.assertEqual(
            (self.competitor / "projects" / "p" / "reports" / "report-006.md").read_text(
                encoding="utf-8"
            ),
            "# Report 006\n\nverified\n",
        )
        self.assertEqual(
            self._git(self.worker, "log", "-1", "--format=%s").stdout.strip(),
            "bridge: report p command 006",
        )

    def test_non_fast_forward_retry_replays_payload_on_latest_remote(self):
        build_generations = []
        race_triggered = False
        original_git = git_store.git

        def build(current):
            build_generations.append(current["generation"])
            return self._publication_payload(current)

        def raced_git(repository, *args, **kwargs):
            nonlocal race_triggered
            if args and args[0] == "push" and not race_triggered:
                race_triggered = True
                (self.competitor / "unrelated.txt").write_text(
                    "remote commit must survive\n", encoding="utf-8"
                )
                self._commit_and_push(self.competitor, "unrelated.txt", "unrelated advance")
            return original_git(repository, *args, **kwargs)

        with patch.object(git_store, "git", side_effect=raced_git):
            result = git_store.publish_cas(
                bridge_root=self.worker,
                state_path=self.state_path,
                expected=lambda current: current.get("generation") == 10,
                already_applied=lambda current: current.get("value") == "published",
                payload_builder=build,
                message="bridge: retry publication",
            )

        self.assertTrue(race_triggered)
        self.assertEqual(build_generations, [10, 10])
        self.assertEqual(result["value"], "published")
        self.assertTrue((self.worker / "unrelated.txt").is_file())
        self.assertEqual(
            self._git(self.competitor, "fetch", "origin").returncode,
            0,
        )
        self._git(self.competitor, "reset", "--hard", "origin/main")
        self.assertTrue((self.competitor / "unrelated.txt").is_file())

    def test_remote_advance_that_invalidates_cas_is_not_overwritten_on_retry(self):
        build_calls = []
        race_triggered = False
        original_git = git_store.git

        def build(current):
            build_calls.append(dict(current))
            return self._publication_payload(current)

        def raced_git(repository, *args, **kwargs):
            nonlocal race_triggered
            if args and args[0] == "push" and not race_triggered:
                race_triggered = True
                remote_state = self._read_state(self.competitor)
                remote_state.update({"generation": 11, "value": "remote-wins"})
                (self.competitor / "projects" / "p" / "state.json").write_text(
                    common.json_text(remote_state), encoding="utf-8"
                )
                self._commit_and_push(
                    self.competitor,
                    "projects/p/state.json",
                    "remote canonical wins",
                )
            return original_git(repository, *args, **kwargs)

        with patch.object(git_store, "git", side_effect=raced_git):
            with self.assertRaises(common.CASConflict):
                git_store.publish_cas(
                    bridge_root=self.worker,
                    state_path=self.state_path,
                    expected=lambda current: current.get("generation") == 10,
                    already_applied=lambda _current: False,
                    payload_builder=build,
                    message="bridge: stale retry must abort",
                )

        self.assertTrue(race_triggered)
        self.assertEqual(len(build_calls), 1)
        self.assertEqual(self._read_state()["value"], "remote-wins")
        self.assertEqual(self._read_state()["generation"], 11)

    def test_worker_report_retry_never_reruns_codex(self):
        project = self.worker / "projects" / "p"
        state = self._read_state()
        state.update(
            {
                "status": "COMMAND_READY",
                "generation": 10,
                "latest_command": 6,
                "latest_report": 5,
                "active_run": None,
            }
        )
        self.state_path.write_text(common.json_text(state), encoding="utf-8")
        command = project / "commands" / "command-006.md"
        command.write_text(
            '<!-- bridge-command: {"command_id":6,"source":"scheduled_chatgpt",'
            '"based_on_report":5,"expected_generation":10,"kind":"EXECUTE"} -->\n'
            "# Command 006\n",
            encoding="utf-8",
        )
        self._git(self.worker, "add", "--", "projects/p/state.json", str(command.relative_to(self.worker)))
        self._git(self.worker, "commit", "-m", "add integration command")
        self._git(self.worker, "push", "origin", "HEAD:main")

        codex_calls = []
        report_push_race = False
        original_git = git_store.git
        fake_result = CodexRunResult(
            exit_code=0,
            final_message='BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}',
            stdout_tail="",
            stderr_tail="",
            marker={"status": "SUCCESS"},
            launched_at="2001-01-15T00:00:00+08:00",
            final_marker_detected_at="2001-01-15T00:00:01+08:00",
            process_exited_at="2001-01-15T00:00:02+08:00",
            wrapper_pid=123,
            stdout_log_path=Path("stdout.log"),
            stderr_log_path=Path("stderr.log"),
            process_scope="integration-test",
            marker_result="VALID",
        )

        def raced_git(repository, *args, **kwargs):
            nonlocal report_push_race
            if args and args[0] == "push" and not report_push_race:
                # The first push is the claim.  Race only the later report
                # publication, after the competitor has based itself on the
                # claimed canonical state.
                if len(codex_calls) == 1:
                    report_push_race = True
                    self._git(self.competitor, "fetch", "origin")
                    self._git(self.competitor, "reset", "--hard", "origin/main")
                    (self.competitor / "worker-unrelated.txt").write_text(
                        "preserve this remote commit\n", encoding="utf-8"
                    )
                    self._commit_and_push(
                        self.competitor,
                        "worker-unrelated.txt",
                        "unrelated worker commit",
                    )
            return original_git(repository, *args, **kwargs)

        def fake_codex_run(**_kwargs):
            codex_calls.append(1)
            return fake_result

        config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "network_guard": {"enabled": False},
        }
        with patch.object(bw, "codex_run", side_effect=fake_codex_run), patch.object(
            git_store, "git", side_effect=raced_git
        ):
            processed = bw.process_project(
                self.worker,
                "p",
                {"workdir": "__BRIDGE_ROOT__"},
                config,
            )

        self.assertTrue(processed)
        self.assertTrue(report_push_race)
        self.assertEqual(codex_calls, [1])
        self._git(self.competitor, "fetch", "origin")
        self._git(self.competitor, "reset", "--hard", "origin/main")
        self.assertTrue((self.competitor / "worker-unrelated.txt").is_file())
        final_state = self._read_state(self.competitor)
        self.assertEqual(final_state["status"], "REPORT_READY")
        self.assertEqual(final_state["latest_report"], 6)
        self.assertIsNone(final_state["active_run"])

    def test_retry_exhaustion_fails_and_leaves_remote_unchanged(self):
        push_attempts = []
        build_calls = []
        original_git = git_store.git

        def always_reject_push(repository, *args, **kwargs):
            if args and args[0] == "push":
                push_attempts.append(1)
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    stdout="",
                    stderr="simulated non-fast-forward",
                )
            return original_git(repository, *args, **kwargs)

        def build(current):
            build_calls.append(dict(current))
            return self._publication_payload(current)

        with patch.object(git_store, "git", side_effect=always_reject_push):
            with self.assertRaises(common.WorkerError) as caught:
                git_store.publish_cas(
                    bridge_root=self.worker,
                    state_path=self.state_path,
                    expected=lambda current: current.get("generation") == 10,
                    already_applied=lambda _current: False,
                    payload_builder=build,
                    message="bridge: exhausted publication",
                    retries=3,
                )

        self.assertEqual(len(push_attempts), 3)
        self.assertEqual(len(build_calls), 3)
        self.assertIn("bounded retries", str(caught.exception))
        self.assertEqual(self._read_state()["value"], "base")
        self.assertFalse(git_store.tracked_dirty(self.worker))

    def test_manual_fast_lane_uses_temp_clone_and_publishes_claim_and_report(self):
        claim = bm.start_manual(
            source_root=self.worker,
            project_id="p",
            body="Perform the bounded manual task.",
            kind="EXECUTE",
            lease_hours=6,
        )

        self._git(self.competitor, "fetch", "origin")
        self._git(self.competitor, "reset", "--hard", "origin/main")
        claimed_state = self._read_state(self.competitor)
        active = claimed_state["active_run"]
        command_path = (
            self.competitor
            / "projects"
            / "p"
            / "commands"
            / f"command-{claim['command_id']:03d}.md"
        )
        command_meta = protocol_core.parse_command_metadata(
            command_path.read_text(encoding="utf-8")
        )

        self.assertEqual(claim["status"], "CODEX_RUNNING")
        self.assertEqual(claim["claimed_generation"], 12)
        self.assertEqual(claim["based_on_report"], 5)
        self.assertEqual(claimed_state["generation"], 12)
        self.assertEqual(active["publication_generation"], 11)
        self.assertEqual(active["claimed_generation"], 12)
        self.assertEqual(active["run_id"], claim["run_id"])
        self.assertEqual(command_meta["expected_generation"], 11)
        self.assertEqual(command_meta["source"], "manual_chatgpt")
        self.assertEqual(
            self._git(self.competitor, "log", "-1", "--format=%an").stdout.strip(),
            bm.MANUAL_GIT_USER_NAME,
        )

        report = bm.finish_manual(
            source_root=self.worker,
            project_id="p",
            run_id=claim["run_id"],
            command_id=claim["command_id"],
            claimed_generation=claim["claimed_generation"],
            outcome="SUCCESS",
            final_message="Manual result was verified.",
        )
        self._git(self.competitor, "fetch", "origin")
        self._git(self.competitor, "reset", "--hard", "origin/main")
        final_state = self._read_state(self.competitor)
        report_path = (
            self.competitor
            / "projects"
            / "p"
            / "reports"
            / f"report-{claim['command_id']:03d}.md"
        )

        self.assertFalse(report["already_published"])
        self.assertEqual(final_state["status"], "REPORT_READY")
        self.assertEqual(final_state["latest_report"], claim["command_id"])
        self.assertEqual(final_state["generation"], 13)
        self.assertIsNone(final_state["active_run"])
        self.assertIn("Manual result was verified.", report_path.read_text(encoding="utf-8"))

    def test_manual_report_failure_preserves_pending_report_without_rerun(self):
        claim = bm.start_manual(
            source_root=self.worker,
            project_id="p",
            body="Perform the manual task once.",
            kind="EXECUTE",
            lease_hours=6,
        )

        with patch.object(
            bm.git_store,
            "publish_cas",
            side_effect=common.WorkerError("simulated remote outage"),
        ):
            with self.assertRaises(common.WorkerError):
                bm.finish_manual(
                    source_root=self.worker,
                    project_id="p",
                    run_id=claim["run_id"],
                    command_id=claim["command_id"],
                    claimed_generation=claim["claimed_generation"],
                    outcome="SUCCESS",
                    final_message="The manual task finished locally.",
                )

        pending = (
            self.worker
            / "worker"
            / "runtime"
            / "manual"
            / "p"
            / f"pending-report-{claim['command_id']:03d}.md"
        )
        reason = pending.with_name(pending.name.replace(".md", ".reason.txt"))
        self.assertTrue(pending.is_file())
        self.assertIn("finished locally", pending.read_text(encoding="utf-8"))
        self.assertIn("Do not rerun", reason.read_text(encoding="utf-8"))




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
