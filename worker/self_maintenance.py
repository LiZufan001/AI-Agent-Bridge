#!/usr/bin/env python3
"""Fail-closed admission and mutation guards for Bridge self-maintenance.

This module is deliberately local and small. It does not change Protocol v2
state transitions, provide a filesystem sandbox, execute Codex, or choose a
historical Candidate/Stable topology. It proves that a configured
self-maintenance target is an independent Git clone before a claim and checks
that a successful run did not mutate Bridge control paths.

The branch and source baseline are operation-specific configuration. They are
never inferred from the retired Candidate-B bootstrap.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import remote_project_registry
from operator_maintenance import MaintenanceLimits, run_maintenance_command


# Backward-compatible fixture names only. Production parsing has no branch or
# baseline default: an enabled self-maintenance target must supply both values.
DEFAULT_CANDIDATE_BRANCH = "maintenance/bridge-current"
BOOTSTRAP_BASE_COMMIT = "0" * 40

_CONFIG_FIELDS = {"enabled", "repository", "candidate_branch", "bootstrap_base"}
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MAX_SNAPSHOT_ENTRIES = 10_000
_GIT_TIMEOUT_SECONDS = 15
_CONTROL_DIRECTORY_NAMES = {
    "commands",
    "reports",
    "owner-actions",
    "owner-feedback",
}
_LOCAL_SECRET_DIRECTORY_NAMES = {
    ".secrets",
    "secrets",
    "local-secrets",
    "local_only",
    "local-only",
}
_LOCAL_SECRET_FILE_NAMES = {
    "credentials.json",
    "credentials.ini",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "token.json",
    "token.txt",
}


class SelfMaintenancePreflightError(RuntimeError):
    """A safe, bounded reason for refusing self-maintenance admission."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SelfMaintenanceConfig:
    repository: str
    candidate_branch: str
    bootstrap_base: str


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    common_dir: Path
    repository: str
    branch: str
    head: str


FileFingerprint = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class ProtectedPathSnapshot:
    entries: tuple[tuple[str, FileFingerprint], ...]


@dataclass(frozen=True)
class SelfMaintenancePreflight:
    config: SelfMaintenanceConfig
    live: RepositoryIdentity
    candidate: RepositoryIdentity
    protected_snapshot: ProtectedPathSnapshot


@dataclass(frozen=True)
class SelfMaintenanceGuard:
    allowed: bool
    reason: str
    violations: int = 0
    branch: str | None = None
    head: str | None = None


def live_worker_checkout() -> Path:
    """Return the checkout containing this running Worker implementation.

    The live path is intentionally derived from the runtime module, never
    supplied by project configuration or by a Codex prompt.
    """

    return Path(__file__).resolve().parents[1]


def _fail(code: str) -> None:
    raise SelfMaintenancePreflightError(code)


def _validate_candidate_branch(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("self_maintenance_candidate_branch_invalid")
    branch = value.strip()
    if (
        branch.casefold() == "main"
        or not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "@{" in branch
        or branch.startswith(("/", "."))
        or branch.endswith(("/", "."))
        or "//" in branch
    ):
        _fail("self_maintenance_candidate_branch_forbidden")
    return branch


def _validate_baseline(value: object) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value.strip()) is None:
        _fail("self_maintenance_bootstrap_base_invalid")
    return value.strip().lower()


def parse_config(project_cfg: Any) -> SelfMaintenanceConfig | None:
    """Parse explicit project-level self-maintenance configuration.

    Enabled self-maintenance has no historical branch or baseline default.
    The caller must bind the operation to the exact current maintenance branch
    and an immutable source baseline. This keeps the preflight fail-closed
    without reviving the retired Candidate-B bootstrap identity.
    """

    if not isinstance(project_cfg, dict):
        _fail("project_config_not_object")
    raw = project_cfg.get("self_maintenance")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _fail("self_maintenance_config_not_object")
    unknown = set(raw) - _CONFIG_FIELDS
    if unknown:
        _fail("self_maintenance_config_unsupported_fields")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        _fail("self_maintenance_enabled_not_boolean")
    if not enabled:
        return None

    repository_value = raw.get("repository")
    if not isinstance(repository_value, str):
        _fail("self_maintenance_repository_not_string")
    repository = remote_project_registry.canonical_repository(repository_value)
    if repository is None:
        _fail("self_maintenance_repository_invalid")

    candidate_branch = _validate_candidate_branch(raw.get("candidate_branch"))
    bootstrap_base = _validate_baseline(raw.get("bootstrap_base"))

    return SelfMaintenanceConfig(
        repository=repository,
        candidate_branch=candidate_branch,
        bootstrap_base=bootstrap_base,
    )


def _path_key(path: Path) -> str:
    value = os.path.abspath(os.path.normpath(str(path)))
    return os.path.normcase(value)


def _paths_overlap(first: Path, second: Path) -> bool:
    first_key = _path_key(first)
    second_key = _path_key(second)
    try:
        common = os.path.commonpath([first_key, second_key])
    except (OSError, ValueError):
        return True
    return common in {first_key, second_key}


def _canonical_existing_dir(
    value: str | os.PathLike[str] | Path,
    code: str,
) -> Path:
    try:
        expanded = os.path.expandvars(os.path.expanduser(os.fspath(value)))
        path = Path(expanded)
        if not path.is_absolute():
            path = live_worker_checkout() / path
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            _fail(code)
        return resolved
    except SelfMaintenancePreflightError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail(code)
    raise AssertionError("unreachable")


def _run_git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        result = run_maintenance_command(
            command,
            command_class="self-maintenance-git",
            cwd=repo,
            limits=MaintenanceLimits(
                timeout_seconds=_GIT_TIMEOUT_SECONDS,
                stdout_bytes=128 * 1024,
                stderr_bytes=64 * 1024,
            ),
        )
    except (OSError, RuntimeError, ValueError):
        _fail("git_inspection_failed")
    if not result["success"]:
        _fail("git_inspection_failed")
    return str(result["stdout"]["tail"]).strip()


def _repo_path_from_git(value: str, repo: Path, code: str) -> Path:
    if not value:
        _fail(code)
    raw = Path(value)
    if not raw.is_absolute():
        raw = repo / raw
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        _fail(code)
    return resolved


def _repository_identity(repo: Path) -> RepositoryIdentity:
    git_root = _repo_path_from_git(
        _run_git(repo, "rev-parse", "--show-toplevel"),
        repo,
        "git_root_invalid",
    )
    if _path_key(git_root) != _path_key(repo):
        _fail("git_root_mismatch")
    origin = remote_project_registry.canonical_repository(
        _run_git(repo, "config", "--get", "remote.origin.url")
    )
    if origin is None:
        _fail("git_origin_invalid")
    branch = _run_git(repo, "branch", "--show-current")
    if not branch:
        _fail("git_detached_head")
    head = _run_git(repo, "rev-parse", "HEAD")
    if not head or _SHA_RE.fullmatch(head) is None:
        _fail("git_head_invalid")
    common_dir = _repo_path_from_git(
        _run_git(repo, "rev-parse", "--git-common-dir"),
        repo,
        "git_common_dir_invalid",
    )
    if not common_dir.is_dir():
        _fail("git_common_dir_invalid")
    return RepositoryIdentity(
        repo,
        common_dir,
        origin,
        branch,
        head.lower(),
    )


def _is_protected(relative: str, *, is_directory: bool) -> bool:
    normalized = relative.replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = normalized.casefold().split("/")
    if len(parts) >= 3 and parts[0] == "projects":
        if parts[-1] == "state.json":
            return True
        if any(part in _CONTROL_DIRECTORY_NAMES for part in parts[2:]):
            return True
    if len(parts) >= 2 and parts[0] == "worker" and parts[1] == "runtime":
        return True
    if parts == ["worker", "config.local.json"]:
        return True

    if any(part in _LOCAL_SECRET_DIRECTORY_NAMES for part in parts):
        return True
    basename = parts[-1]
    if basename in _LOCAL_SECRET_FILE_NAMES:
        return True
    if basename.startswith(".env") and basename not in {".env.example", ".env.sample"}:
        return True
    if basename.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    if not is_directory and (
        basename.endswith((".secret", ".secrets"))
        or basename.endswith((".local.json", ".local.yaml", ".local.yml"))
    ):
        return True
    return False


def _fingerprint(path: Path) -> FileFingerprint:
    try:
        info = path.lstat()
    except OSError:
        _fail("protected_snapshot_failed")
    return (
        stat.S_IFMT(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ino),
        int(info.st_dev),
    )


def _protected_snapshot(repo: Path) -> ProtectedPathSnapshot:
    entries: dict[str, FileFingerprint] = {}
    try:
        walker = os.walk(repo, topdown=True, followlinks=False)
        for current, directories, files in walker:
            current_path = Path(current)
            directories[:] = [name for name in directories if name != ".git"]
            for name in [*directories, *files]:
                path = current_path / name
                try:
                    relative = path.relative_to(repo).as_posix()
                except ValueError:
                    _fail("protected_snapshot_failed")
                if not _is_protected(relative, is_directory=path.is_dir()):
                    continue
                entries[relative] = _fingerprint(path)
                if len(entries) > _MAX_SNAPSHOT_ENTRIES:
                    _fail("protected_snapshot_too_large")
    except SelfMaintenancePreflightError:
        raise
    except (OSError, RuntimeError):
        _fail("protected_snapshot_failed")
    return ProtectedPathSnapshot(tuple(sorted(entries.items())))


def _candidate_is_clean(repo: Path) -> None:
    if _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        _fail("candidate_worktree_dirty")


def _assert_baseline_ancestor(repo: Path, baseline: str) -> None:
    try:
        _run_git(repo, "merge-base", "--is-ancestor", baseline, "HEAD")
    except SelfMaintenancePreflightError:
        _fail("candidate_bootstrap_base_missing")


def _preflight(
    project_cfg: Any,
    candidate_workdir: str | os.PathLike[str] | Path,
    live_root: str | os.PathLike[str] | Path | None,
) -> SelfMaintenancePreflight | None:
    config = parse_config(project_cfg)
    if config is None:
        return None

    live = _canonical_existing_dir(
        live_root if live_root is not None else live_worker_checkout(),
        "live_checkout_invalid",
    )
    candidate = _canonical_existing_dir(
        candidate_workdir,
        "candidate_checkout_invalid",
    )
    if _paths_overlap(live, candidate):
        _fail("checkout_paths_overlap")

    live_identity = _repository_identity(live)
    candidate_identity = _repository_identity(candidate)
    if live_identity.repository != config.repository:
        _fail("live_repository_mismatch")
    if candidate_identity.repository != config.repository:
        _fail("candidate_repository_mismatch")
    if candidate_identity.branch != config.candidate_branch:
        _fail("candidate_branch_mismatch")
    if candidate_identity.branch.casefold() == "main":
        _fail("candidate_branch_forbidden")
    if candidate_identity.branch == live_identity.branch:
        _fail("candidate_branch_is_live_branch")
    if _path_key(live_identity.common_dir) == _path_key(candidate_identity.common_dir):
        _fail("git_common_dir_shared")

    _candidate_is_clean(candidate)
    _assert_baseline_ancestor(candidate, config.bootstrap_base)

    protected_changes = {
        path
        for path in _changed_paths(candidate, config.bootstrap_base)
        if _is_protected(path, is_directory=False)
    }
    if protected_changes:
        _fail("candidate_bootstrap_protected_changes")

    return SelfMaintenancePreflight(
        config=config,
        live=live_identity,
        candidate=candidate_identity,
        protected_snapshot=_protected_snapshot(candidate),
    )


def preflight(
    project_cfg: Any,
    *,
    candidate_workdir: str | os.PathLike[str] | Path,
    live_root: str | os.PathLike[str] | Path | None = None,
) -> SelfMaintenancePreflight | None:
    """Validate a self-maintenance target before any Bridge claim is made."""

    try:
        return _preflight(project_cfg, candidate_workdir, live_root)
    except SelfMaintenancePreflightError:
        raise
    except Exception:
        _fail("self_maintenance_inspection_failed")
    raise AssertionError("unreachable")


def _changed_paths(repo: Path, baseline_head: str) -> set[str]:
    changed = {
        line.replace("\\", "/").strip("/")
        for line in _run_git(
            repo,
            "diff",
            "--name-only",
            "--no-renames",
            baseline_head,
            "--",
        ).splitlines()
        if line.strip()
    }
    changed.update(
        line.replace("\\", "/").strip("/")
        for line in _run_git(
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
        ).splitlines()
        if line.strip()
    )
    return changed


def verify_after_run(result: SelfMaintenancePreflight) -> SelfMaintenanceGuard:
    """Verify target identity and reject protected-path mutations.

    This is a repository mutation guard, not a sandbox: it preserves source
    changes for inspection and only prevents the Worker from publishing SUCCESS
    when the guard detects a protected mutation or identity drift.
    """

    try:
        current = _repository_identity(result.candidate.root)
        if _path_key(current.root) != _path_key(result.candidate.root):
            return SelfMaintenanceGuard(
                False,
                "candidate_root_changed",
                branch=current.branch,
                head=current.head,
            )
        if current.repository != result.config.repository:
            return SelfMaintenanceGuard(
                False,
                "candidate_repository_changed",
                branch=current.branch,
                head=current.head,
            )
        if current.branch != result.config.candidate_branch:
            return SelfMaintenanceGuard(
                False,
                "candidate_branch_changed",
                branch=current.branch,
                head=current.head,
            )
        if current.branch.casefold() == "main":
            return SelfMaintenanceGuard(
                False,
                "candidate_branch_forbidden",
                branch=current.branch,
                head=current.head,
            )
        if _path_key(current.common_dir) != _path_key(result.candidate.common_dir):
            return SelfMaintenanceGuard(
                False,
                "git_common_dir_changed",
                branch=current.branch,
                head=current.head,
            )
        _assert_baseline_ancestor(
            result.candidate.root,
            result.config.bootstrap_base,
        )
        changed_paths = _changed_paths(
            result.candidate.root,
            result.config.bootstrap_base,
        )
        after = _protected_snapshot(result.candidate.root)
    except SelfMaintenancePreflightError as exc:
        return SelfMaintenanceGuard(False, exc.code)
    except Exception:
        return SelfMaintenanceGuard(False, "self_maintenance_guard_failed")

    before_entries = dict(result.protected_snapshot.entries)
    after_entries = dict(after.entries)
    snapshot_changes = {
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    }
    diff_changes = {
        path
        for path in changed_paths
        if _is_protected(path, is_directory=False)
    }
    violations = snapshot_changes | diff_changes
    if violations:
        return SelfMaintenanceGuard(
            False,
            "protected_path_mutation",
            violations=len(violations),
            branch=current.branch,
            head=current.head,
        )
    return SelfMaintenanceGuard(
        True,
        "accepted",
        branch=current.branch,
        head=current.head,
    )


__all__ = [
    "DEFAULT_CANDIDATE_BRANCH",
    "BOOTSTRAP_BASE_COMMIT",
    "RepositoryIdentity",
    "SelfMaintenanceConfig",
    "SelfMaintenanceGuard",
    "SelfMaintenancePreflight",
    "SelfMaintenancePreflightError",
    "live_worker_checkout",
    "parse_config",
    "preflight",
    "verify_after_run",
]
