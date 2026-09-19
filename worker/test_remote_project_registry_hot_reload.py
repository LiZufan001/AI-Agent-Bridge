import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import remote_project_registry as rpr


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class HotReloadChangeDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def repo(self) -> tuple[Path, str]:
        root = self.root / f"repo-{len(list(self.root.glob('repo-*')))}"
        root.mkdir()
        git("init", "--initial-branch=main", cwd=root)
        git("config", "user.name", "hot-reload-test", cwd=root)
        git("config", "user.email", "hot-reload-test@localhost", cwd=root)
        (root / "README.md").write_text("boot\n", encoding="utf-8")
        git("add", "README.md", cwd=root)
        git("commit", "-m", "boot", cwd=root)
        return root, git("rev-parse", "HEAD", cwd=root)

    def commit_file(self, root: Path, relative: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n", encoding="utf-8")
        git("add", relative, cwd=root)
        git("commit", "-m", f"change {relative}", cwd=root)

    def test_head_unchanged_and_missing_boot_head_are_false(self):
        root, boot = self.repo()
        self.assertFalse(rpr.worker_code_changed(root, boot))
        self.assertFalse(rpr.worker_code_changed(root, None))

    def test_project_state_command_report_and_docs_only_commits_are_false(self):
        paths = (
            "projects/p/state.json",
            "projects/p/commands/command-008.md",
            "projects/p/reports/report-008.md",
            "docs/ROADMAP.md",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                root, boot = self.repo()
                self.commit_file(root, relative)
                self.assertFalse(rpr.worker_code_changed(root, boot))

    def test_runtime_dependency_changes_are_true(self):
        paths = (
            "worker/bridge_worker.py",
            "worker/bridge_worker_hardened.py",
            "worker/executor.py",
            "worker/bridge_common.py",
            "worker/git_store.py",
            "worker/protocol_core.py",
            "worker/pending_report.py",
            "worker/report_builder.py",
            "worker/remote_project_registry.py",
            "worker/recovery_journal.py",
            "worker/phased_task.py",
            "worker/codex_lifecycle.py",
            "worker/worker_health.py",
            "worker/bridge_alerts.py",
            "worker/supervisor_publication_gateway.py",
            "worker/supervisor_publication_gateway_core.py",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                root, boot = self.repo()
                self.commit_file(root, relative)
                self.assertTrue(rpr.worker_code_changed(root, boot))

    def test_successful_empty_diff_is_not_an_inspection_failure(self):
        root, boot = self.repo()
        with patch.object(rpr, "git_head", return_value="current-head"), patch.object(
            rpr, "_git_output_status", return_value=(True, "")
        ):
            self.assertFalse(rpr.worker_code_changed(root, boot))

    def test_real_git_inspection_failure_remains_fail_safe(self):
        root, boot = self.repo()
        with patch.object(rpr, "git_head", return_value="current-head"), patch.object(
            rpr, "_git_output_status", return_value=(False, "")
        ):
            self.assertTrue(rpr.worker_code_changed(root, boot))


if __name__ == "__main__":
    unittest.main()
