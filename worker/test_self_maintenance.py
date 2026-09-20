import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge_worker as bw
import executor
import self_maintenance as sm
from codex_lifecycle import CodexRunResult


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def make_repo(root: Path, name: str, origin: str = "example-owner/AI-Agent-Bridge") -> Path:
    repo = root / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Self Maintenance Tests")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "baseline")
    run_git(repo, "remote", "add", "origin", origin)
    return repo


def fake_success_result() -> CodexRunResult:
    return CodexRunResult(
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
        process_scope="test",
        marker_result="VALID",
    )


class SelfMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.live = make_repo(self.root, "live")
        self.candidate = make_repo(self.root, "candidate")
        run_git(self.candidate, "switch", "-c", sm.DEFAULT_CANDIDATE_BRANCH)
        self.base_patch = patch.object(
            sm,
            "BOOTSTRAP_BASE_COMMIT",
            run_git(self.candidate, "rev-parse", "HEAD"),
        )
        self.base_patch.start()
        self.config = {
            "workdir": str(self.candidate),
            "self_maintenance": {
                "enabled": True,
                "repository": "https://github.com/example-owner/AI-Agent-Bridge.git",
                "candidate_branch": sm.DEFAULT_CANDIDATE_BRANCH,
                "bootstrap_base": sm.BOOTSTRAP_BASE_COMMIT,
            },
        }

    def tearDown(self) -> None:
        self.base_patch.stop()
        self.temp_dir.cleanup()

    def preflight(self, *, candidate: Path | None = None, live: Path | None = None):
        return sm.preflight(
            self.config,
            candidate_workdir=candidate or self.candidate,
            live_root=live or self.live,
        )

    def assert_rejected(self, code: str, **kwargs) -> None:
        with self.assertRaises(sm.SelfMaintenancePreflightError) as context:
            self.preflight(**kwargs)
        self.assertEqual(context.exception.code, code)

    def test_disabled_or_absent_configuration_is_ordinary(self):
        self.assertIsNone(sm.parse_config({"workdir": str(self.candidate)}))
        self.assertIsNone(
            sm.parse_config(
                {
                    "self_maintenance": {
                        "enabled": False,
                        "repository": "Owner/Repo",
                        "candidate_branch": "main",
                    }
                }
            )
        )

    def test_malformed_configuration_fails_closed(self):
        with self.assertRaises(sm.SelfMaintenancePreflightError):
            sm.parse_config({"self_maintenance": {"enabled": "yes"}})

        invalid_branch = dict(self.config)
        invalid_branch["self_maintenance"] = dict(self.config["self_maintenance"])
        invalid_branch["self_maintenance"]["candidate_branch"] = "main"
        with self.assertRaises(sm.SelfMaintenancePreflightError) as context:
            sm.preflight(invalid_branch, candidate_workdir=self.candidate, live_root=self.live)
        self.assertEqual(context.exception.code, "self_maintenance_candidate_branch_forbidden")

    def test_equal_parent_child_and_case_overlap_rejected(self):
        self.assert_rejected("checkout_paths_overlap", candidate=self.live)

        child = self.live / "nested"
        child.mkdir()
        self.assert_rejected("checkout_paths_overlap", candidate=child)

        parent = self.root / "parent-root"
        parent.mkdir()
        nested_candidate = parent / "candidate"
        nested_candidate.mkdir()
        self.assert_rejected("checkout_paths_overlap", candidate=nested_candidate, live=parent)

        if os.name == "nt":
            self.assert_rejected(
                "checkout_paths_overlap",
                candidate=Path(str(self.live).upper()),
            )

    def test_symlink_alias_to_live_is_rejected_when_supported(self):
        alias = self.root / "live-alias"
        try:
            alias.symlink_to(self.live, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("directory symlinks are unavailable on this host")
            junction = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(alias), str(self.live)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if junction.returncode != 0:
                self.skipTest("directory symlinks and junctions are unavailable")
        self.assert_rejected("checkout_paths_overlap", candidate=alias)

    def test_wrong_repository_main_and_wrong_branch_are_rejected(self):
        wrong_repo = make_repo(self.root, "wrong-repo", origin="Owner/Other")
        run_git(wrong_repo, "switch", "-c", sm.DEFAULT_CANDIDATE_BRANCH)
        self.assert_rejected("candidate_repository_mismatch", candidate=wrong_repo)

        main_candidate = make_repo(self.root, "main-candidate")
        self.assert_rejected("candidate_branch_mismatch", candidate=main_candidate)

        wrong_branch = make_repo(self.root, "wrong-branch")
        run_git(wrong_branch, "switch", "-c", "feature/other")
        self.assert_rejected("candidate_branch_mismatch", candidate=wrong_branch)

    def test_independent_clone_on_exact_feature_branch_is_accepted(self):
        result = self.preflight()
        self.assertIsNotNone(result)
        self.assertEqual(result.candidate.branch, sm.DEFAULT_CANDIDATE_BRANCH)
        self.assertEqual(result.candidate.repository, "example-owner/ai-agent-bridge")
        self.assertNotEqual(result.live.common_dir, result.candidate.common_dir)

    def test_coordinator_runtime_fixture_stays_outside_protected_candidate_tree(self):
        result = self.preflight()
        before = sm._protected_snapshot(self.candidate)
        isolated_runtime = self.root / "isolated-worker-runtime"

        coordinator = bw.WorkerCoordinator(
            self.candidate,
            {"runtime": {"max_parallel_runs": 1}},
            runtime_root=isolated_runtime,
        )
        coordinator.close()

        after = sm._protected_snapshot(self.candidate)
        self.assertEqual(before, after)
        self.assertTrue((isolated_runtime / "worker-health.json").is_file())
        self.assertTrue(
            (isolated_runtime / "resource-registry.json").is_file()
        )
        guard = sm.verify_after_run(result)
        self.assertTrue(guard.allowed)
        self.assertEqual(guard.reason, "accepted")
        self.assertEqual(guard.violations, 0)
        self.assertFalse((self.candidate / "worker" / "runtime").exists())

    def test_shared_git_worktree_is_rejected(self):
        shared = self.root / "shared-worktree"
        run_git(
            self.live,
            "worktree",
            "add",
            "-b",
            sm.DEFAULT_CANDIDATE_BRANCH,
            str(shared),
            "HEAD",
        )
        self.assert_rejected("git_common_dir_shared", candidate=shared)

    def test_candidate_dirty_worktree_is_rejected(self):
        (self.candidate / "README.md").write_text("dirty\n", encoding="utf-8")
        self.assert_rejected("candidate_worktree_dirty")

    def test_existing_protected_commit_is_checked_against_fixed_base(self):
        protected = self.candidate / "projects" / "demo" / "state.json"
        protected.parent.mkdir(parents=True)
        protected.write_text("{}\n", encoding="utf-8")
        run_git(self.candidate, "add", str(protected.relative_to(self.candidate)))
        run_git(self.candidate, "commit", "-m", "unsafe protected change")
        self.assert_rejected("candidate_bootstrap_protected_changes")

    def test_protected_diff_is_not_accepted_but_source_diff_is(self):
        result = self.preflight()
        protected = self.candidate / "projects" / "demo" / "state.json"
        protected.parent.mkdir(parents=True)
        protected.write_text("{}\n", encoding="utf-8")
        guard = sm.verify_after_run(result)
        self.assertFalse(guard.allowed)
        self.assertEqual(guard.reason, "protected_path_mutation")
        self.assertGreaterEqual(guard.violations, 1)

        self.tearDown()
        self.setUp()
        result = self.preflight()
        (self.candidate / "README.md").write_text("candidate change\n", encoding="utf-8")
        guard = sm.verify_after_run(result)
        self.assertTrue(guard.allowed)
        self.assertEqual(guard.reason, "accepted")

    def test_worker_downgrades_success_when_protected_path_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge_root = Path(tmp) / "bridge-control"
            project = bridge_root / "projects" / "engine-maintenance"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("candidate maintenance\n", encoding="utf-8")
            state = {
                "protocol_version": 2,
                "status": "COMMAND_READY",
                "generation": 7,
                "latest_command": 1,
                "latest_report": 0,
                "active_run": None,
            }
            state_path = project / "state.json"
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            (project / "commands" / "command-001.md").write_text(
                '<!-- bridge-command: {"command_id":1,"source":"scheduled_chatgpt",'
                '"based_on_report":0,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )
            remote_state = dict(state)
            published_report = ""

            def fake_publish(**kwargs):
                nonlocal remote_state, published_report
                payloads = kwargs["payload_builder"](remote_state)
                remote_state = json.loads(payloads[kwargs["state_path"]])
                for path, value in payloads.items():
                    if path.name == "report-001.md":
                        published_report = value
                return remote_state

            def fake_codex(**kwargs):
                self.assertEqual(
                    kwargs["codex_execution_mode"],
                    executor.SELF_MAINTENANCE_PERMISSIONS_MODE,
                )
                self.assertIn(
                    "SELF-MAINTENANCE CANDIDATE BOUNDARY",
                    kwargs["prompt"],
                )
                self.assertNotIn(executor.FULL_ACCESS_FLAG, kwargs["codex_args"])
                self.assertNotIn("--sandbox", kwargs["codex_args"])
                self.assertNotIn("--ask-for-approval", kwargs["codex_args"])
                self.assertIn('approval_policy="never"', kwargs["codex_args"])
                self.assertIn("--ignore-user-config", kwargs["codex_args"])
                self.assertIn("--ignore-rules", kwargs["codex_args"])
                self.assertIn('windows.sandbox="elevated"', kwargs["codex_args"])
                self.assertIn(
                    f'permissions.{executor.SELF_MAINTENANCE_PERMISSION_PROFILE}.filesystem={{":root"="read"}}',
                    kwargs["codex_args"],
                )
                protected = self.candidate / "projects" / "owned" / "state.json"
                protected.parent.mkdir(parents=True)
                protected.write_text("mutated\n", encoding="utf-8")
                return fake_success_result()

            config = {
                "codex_command": "python",
                "codex_execution_mode": "full_access",
                "codex_args": [
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                ],
                "network_guard": {"enabled": False},
            }
            project_cfg = {
                "workdir": str(self.candidate),
                "self_maintenance": self.config["self_maintenance"],
            }
            with patch.object(sm, "live_worker_checkout", return_value=self.live), patch.object(
                bw, "publish_cas", side_effect=fake_publish
            ), patch.object(bw, "codex_run", side_effect=fake_codex), patch.object(
                bw, "get_head", side_effect=lambda workdir: run_git(workdir, "rev-parse", "HEAD")
            ):
                processed = bw.process_project(
                    bridge_root,
                    "engine-maintenance",
                    project_cfg,
                    config,
                )
            self.assertTrue(processed)
            self.assertEqual(remote_state["status"], "REPORT_READY")
            self.assertIn("SELF_MAINTENANCE_GUARD_FAILED", remote_state["last_execution_error"])
            self.assertIn("outcome: FAILED", published_report)


    def test_ignored_runtime_mutation_and_branch_change_are_rejected(self):
        result = self.preflight()
        runtime_file = self.candidate / "worker" / "runtime" / "run.log"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("bounded evidence\n", encoding="utf-8")
        guard = sm.verify_after_run(result)
        self.assertFalse(guard.allowed)
        self.assertEqual(guard.reason, "protected_path_mutation")

        self.tearDown()
        self.setUp()
        result = self.preflight()
        run_git(self.candidate, "switch", "-c", "feature/other")
        guard = sm.verify_after_run(result)
        self.assertFalse(guard.allowed)
        self.assertEqual(guard.reason, "candidate_branch_changed")

    def test_preflight_rejection_happens_before_claim_or_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "p"
            (project / "commands").mkdir(parents=True)
            (project / "reports").mkdir()
            (project / "MISSION.md").write_text("maintenance\n", encoding="utf-8")
            state = {
                "protocol_version": 2,
                "status": "COMMAND_READY",
                "generation": 7,
                "latest_command": 1,
                "latest_report": 0,
                "active_run": None,
            }
            state_path = project / "state.json"
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            (project / "commands" / "command-001.md").write_text(
                '<!-- bridge-command: {"command_id":1,"source":"scheduled_chatgpt",'
                '"based_on_report":0,"expected_generation":7,"kind":"EXECUTE"} -->\n',
                encoding="utf-8",
            )
            config = {
                "network_guard": {"enabled": False},
                "projects": {},
            }
            project_cfg = {
                "workdir": "__BRIDGE_ROOT__",
                "self_maintenance": {
                    "enabled": True,
                    "repository": "example-owner/AI-Agent-Bridge",
                    "candidate_branch": sm.DEFAULT_CANDIDATE_BRANCH,
                    "bootstrap_base": sm.BOOTSTRAP_BASE_COMMIT,
                },
            }
            before = state_path.read_bytes()
            with patch.object(sm, "live_worker_checkout", return_value=root), patch.object(
                bw, "publish_cas"
            ) as publish, patch.object(bw, "codex_run") as codex:
                processed = bw.process_project(root, "p", project_cfg, config)
            self.assertFalse(processed)
            publish.assert_not_called()
            codex.assert_not_called()
            self.assertEqual(state_path.read_bytes(), before)
            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(current["generation"], 7)
            self.assertIsNone(current["active_run"])
            self.assertNotIn("RECOVERY_REQUIRED", current["status"])




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
