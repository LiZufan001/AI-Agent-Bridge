import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import remote_project_registry as rpr


class RemoteProjectRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "worker").mkdir()
        self.config_path = self.root / "worker" / "config.local.json"
        self.registry_path = self.root / "worker" / "remote-projects.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_config(self, extra=None):
        data = {
            "projects": {
                "local-only": {
                    "enabled": True,
                    "workdir": str(self.root),
                }
            }
        }
        if extra:
            data.update(extra)
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

    def write_registry(self, project_workdir: Path, self_maintenance=None):
        project = {
            "enabled": True,
            "repository": "Owner/Repo",
            "workdir": str(project_workdir),
        }
        if self_maintenance is not None:
            project["self_maintenance"] = self_maintenance
        data = {
            "schema_version": 1,
            "hosts": {
                "TEST-HOST": {
                    "allowed_workdir_roots": [str(self.root)],
                    "projects": {
                        "remote-project": project
                    },
                }
            },
        }
        self.registry_path.write_text(json.dumps(data), encoding="utf-8")

    def test_remote_mapping_overlays_local_projects(self):
        project = self.root / "repo"
        project.mkdir()
        self.write_config()
        self.write_registry(project)

        config = rpr.load_runtime_config(
            self.config_path,
            self.root,
            host_name="test-host",
        )
        self.assertIn("local-only", config["projects"])
        self.assertEqual(config["projects"]["remote-project"]["repository"], "Owner/Repo")
        self.assertTrue(config["projects"]["remote-project"]["_remote_registry"])
        self.assertEqual(config["_remote_allowed_workdir_roots"], [str(self.root)])

    def test_missing_host_preserves_local_projects(self):
        project = self.root / "repo"
        project.mkdir()
        self.write_config()
        self.write_registry(project)
        config = rpr.load_runtime_config(
            self.config_path,
            self.root,
            host_name="OTHER-HOST",
        )
        self.assertEqual(set(config["projects"]), {"local-only"})

    def test_remote_mapping_preserves_optional_self_maintenance_config(self):
        project = self.root / "repo"
        project.mkdir()
        self.write_config()
        self.write_registry(
            project,
            {
                "enabled": True,
                "repository": "Owner/Repo",
                "candidate_branch": "feature/synthetic-candidate",
                "bootstrap_base": "4444444444444444444444444444444444444444",
            },
        )
        config = rpr.load_runtime_config(
            self.config_path,
            self.root,
            host_name="TEST-HOST",
        )
        self.assertEqual(
            config["projects"]["remote-project"]["self_maintenance"]["candidate_branch"],
            "feature/synthetic-candidate",
        )

    def test_canonical_repository_accepts_https_and_ssh(self):
        expected = "example-owner/exampledeviceapp"
        self.assertEqual(
            rpr.canonical_repository("https://github.com/example-owner/ExampleDeviceApp.git"),
            expected,
        )
        self.assertEqual(
            rpr.canonical_repository("git@github.com:example-owner/ExampleDeviceApp.git"),
            expected,
        )
        self.assertEqual(
            rpr.canonical_repository("example-owner/ExampleDeviceApp"),
            expected,
        )

    def test_validate_remote_project_checks_origin_and_roots(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Owner/Repo.git"],
            check=True,
            capture_output=True,
        )
        config = {
            "_remote_allowed_workdir_roots": [str(self.root)],
            "allowed_workdir_roots": [str(self.root)],
        }
        project = {
            "_remote_registry": True,
            "repository": "Owner/Repo",
            "workdir": str(repo),
        }
        self.assertEqual(
            rpr.validate_runtime_project("p", project, config, self.root),
            repo.resolve(),
        )

    def test_validate_remote_project_rejects_origin_mismatch(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Owner/Other.git"],
            check=True,
            capture_output=True,
        )
        project = {
            "_remote_registry": True,
            "repository": "Owner/Repo",
            "workdir": str(repo),
        }
        with self.assertRaises(rpr.RemoteProjectError):
            rpr.validate_runtime_project(
                "p",
                project,
                {"_remote_allowed_workdir_roots": [str(self.root)]},
                self.root,
            )

    def test_local_allowed_roots_can_narrow_remote_registry(self):
        repo = self.root / "repo"
        repo.mkdir()
        outside = self.root / "allowed" / "different"
        outside.mkdir(parents=True)
        project = {
            "_remote_registry": True,
            "repository": "Owner/Repo",
            "workdir": str(repo),
        }
        with self.assertRaises(rpr.RemoteProjectError):
            rpr.validate_runtime_project(
                "p",
                project,
                {
                    "_remote_allowed_workdir_roots": [str(self.root)],
                    "allowed_workdir_roots": [str(outside)],
                },
                self.root,
            )


if __name__ == "__main__":
    unittest.main()
