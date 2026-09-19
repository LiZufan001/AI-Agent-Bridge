#!/usr/bin/env python3
"""Remote project registry and runtime validation for the local Bridge Worker.

The tracked registry may add/override project mappings for a named Worker host.
Local config remains authoritative for secrets/runtime knobs and may optionally
narrow workdirs further with ``allowed_workdir_roots``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_PATH = "worker/remote-projects.json"
REMOTE_PROJECT_FIELDS = {"enabled", "repository", "workdir", "self_maintenance"}
REMOTE_HOST_FIELDS = {"projects", "allowed_workdir_roots"}
WORKER_CODE_PATHS = (
    "worker/state_roots.py",
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
    "worker/worker_execution_control.py",
    "worker/bridge_alerts.py",
    "worker/supervisor_publication_gateway.py",
    "worker/supervisor_publication_gateway_core.py",
    "worker/self_maintenance.py",
)


class RemoteProjectError(RuntimeError):
    pass


def _json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteProjectError(f"Unable to read remote project registry: {path}") from exc
    if not isinstance(data, dict):
        raise RemoteProjectError("Remote project registry must be a JSON object.")
    return data


def _resolve_path(value: str, bridge_root: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = bridge_root / path
    return path.resolve()


def _host_record(registry: dict[str, Any], host_name: str) -> dict[str, Any] | None:
    if int(registry.get("schema_version", 0)) != 1:
        raise RemoteProjectError("Unsupported remote project registry schema_version.")
    hosts = registry.get("hosts")
    if not isinstance(hosts, dict):
        raise RemoteProjectError("Remote project registry requires a 'hosts' object.")

    direct = hosts.get(host_name)
    if direct is None:
        folded = host_name.casefold()
        matches = [value for key, value in hosts.items() if str(key).casefold() == folded]
        if len(matches) == 1:
            direct = matches[0]
    if direct is None:
        return None
    if not isinstance(direct, dict):
        raise RemoteProjectError(f"Remote host record is not an object: {host_name}")
    unknown = sorted(set(direct) - REMOTE_HOST_FIELDS)
    if unknown:
        raise RemoteProjectError(
            "Remote host record contains unsupported fields: " + ", ".join(unknown)
        )
    return direct


def _validate_remote_project(project_id: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RemoteProjectError(f"Remote project mapping is not an object: {project_id}")
    unknown = sorted(set(raw) - REMOTE_PROJECT_FIELDS)
    if unknown:
        raise RemoteProjectError(
            f"Remote project {project_id} contains unsupported fields: " + ", ".join(unknown)
        )
    workdir = raw.get("workdir")
    repository = raw.get("repository")
    if not isinstance(workdir, str) or not workdir.strip():
        raise RemoteProjectError(f"Remote project {project_id} requires a workdir.")
    if not isinstance(repository, str) or not canonical_repository(repository):
        raise RemoteProjectError(f"Remote project {project_id} requires a GitHub repository.")
    validated = {
        "enabled": bool(raw.get("enabled", True)),
        "repository": repository.strip(),
        "workdir": workdir.strip(),
        "_remote_registry": True,
    }
    if "self_maintenance" in raw:
        self_maintenance = raw["self_maintenance"]
        if not isinstance(self_maintenance, dict):
            raise RemoteProjectError(
                f"Remote project {project_id} self_maintenance must be an object."
            )
        # The Worker owns the strict typed validation.  Keep the optional
        # mapping intact so host-specific registry entries can opt in without
        # changing ordinary project behavior.
        validated["self_maintenance"] = dict(self_maintenance)
    return validated


def load_runtime_config(
    config_path: Path,
    bridge_root: Path,
    *,
    host_name: str | None = None,
) -> dict[str, Any]:
    """Reload local config and overlay this host's tracked remote project mappings."""
    local = _json_object(config_path)
    projects = local.get("projects")
    if not isinstance(projects, dict):
        raise RemoteProjectError("Local config must contain a 'projects' object.")

    registry_value = str(local.get("remote_projects_file", DEFAULT_REGISTRY_PATH)).strip()
    if not registry_value:
        return local
    registry_path = _resolve_path(registry_value, bridge_root)
    if not registry_path.is_file():
        return local

    registry = _json_object(registry_path)
    host = _host_record(registry, host_name or platform.node() or "unknown")
    if host is None:
        return local

    remote_projects = host.get("projects", {})
    if not isinstance(remote_projects, dict):
        raise RemoteProjectError("Remote host 'projects' must be an object.")

    merged_projects = dict(projects)
    for project_id, raw in remote_projects.items():
        project_key = str(project_id).strip()
        if not project_key:
            raise RemoteProjectError("Remote project id must not be empty.")
        merged_projects[project_key] = _validate_remote_project(project_key, raw)

    roots = host.get("allowed_workdir_roots", [])
    if not isinstance(roots, list) or not all(isinstance(item, str) and item.strip() for item in roots):
        raise RemoteProjectError("Remote host allowed_workdir_roots must be a list of paths.")

    merged = dict(local)
    merged["projects"] = merged_projects
    merged["_remote_allowed_workdir_roots"] = list(roots)
    merged["_remote_registry_path"] = str(registry_path)
    return merged


def canonical_repository(value: str) -> str | None:
    """Normalize common GitHub repository/origin spellings to owner/repo."""
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\\", "/")
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", text, re.IGNORECASE)
    if match:
        return match.group(1).removesuffix(".git").casefold()
    match = re.fullmatch(
        r"(?:https?|ssh)://(?:git@)?github\.com/([^/]+/[^/]+?)(?:\.git)?/?",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).removesuffix(".git").casefold()
    match = re.fullmatch(r"([^/\s]+/[^/\s]+?)(?:\.git)?", text)
    if match:
        return match.group(1).removesuffix(".git").casefold()
    return None


def _git_output(workdir: Path, *args: str) -> str | None:
    success, output = _git_output_status(workdir, *args)
    if not success:
        return None
    # Preserve the historical helper contract for existing callers: a
    # successful command with empty stdout still appears as None here.  The
    # hot-reload decision below uses the explicit status-aware helper.
    return output or None


def _git_output_status(workdir: Path, *args: str) -> tuple[bool, str]:
    """Return (command_succeeded, stripped_stdout) without conflating empty output."""

    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
            **options,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout.strip()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _enforce_roots(path: Path, roots: Any, bridge_root: Path, label: str) -> None:
    if roots in (None, []):
        return
    if not isinstance(roots, list) or not all(isinstance(item, str) and item.strip() for item in roots):
        raise RemoteProjectError(f"{label} must be a list of non-empty paths.")
    resolved = [_resolve_path(item, bridge_root) for item in roots]
    if not any(_is_within(path, root) or path == root for root in resolved):
        raise RemoteProjectError(f"Workdir is outside {label}.")


def validate_runtime_project(
    project_id: str,
    project_cfg: dict[str, Any],
    config: dict[str, Any],
    bridge_root: Path,
) -> Path:
    """Validate a project mapping before any command is claimed."""
    if not isinstance(project_cfg, dict):
        raise RemoteProjectError(f"Project mapping is not an object: {project_id}")
    raw_workdir = str(project_cfg.get("workdir", "__BRIDGE_ROOT__"))
    if project_cfg.get("_remote_registry") and raw_workdir == "__BRIDGE_ROOT__":
        raise RemoteProjectError("Remote-managed projects may not target __BRIDGE_ROOT__.")
    workdir = _resolve_path(raw_workdir, bridge_root) if raw_workdir != "__BRIDGE_ROOT__" else bridge_root.resolve()

    _enforce_roots(workdir, config.get("_remote_allowed_workdir_roots", []), bridge_root, "remote allowed_workdir_roots")
    _enforce_roots(workdir, config.get("allowed_workdir_roots", []), bridge_root, "local allowed_workdir_roots")

    if not workdir.is_dir():
        raise RemoteProjectError(f"Configured workdir does not exist for {project_id}: {workdir}")

    if project_cfg.get("_remote_registry"):
        inside = _git_output(workdir, "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            raise RemoteProjectError(f"Remote workdir is not a Git repository: {project_id}")
        expected = canonical_repository(str(project_cfg.get("repository", "")))
        origin = _git_output(workdir, "config", "--get", "remote.origin.url")
        actual = canonical_repository(origin or "")
        if not expected or actual != expected:
            raise RemoteProjectError(
                f"Git origin does not match registered repository for {project_id}."
            )
    return workdir


def git_head(bridge_root: Path) -> str | None:
    output = _git_output(bridge_root, "rev-parse", "HEAD")
    return output


def worker_code_changed(bridge_root: Path, boot_head: str | None) -> bool:
    """Return True if tracked Worker implementation changed since this process booted."""
    if not boot_head:
        return False
    current = git_head(bridge_root)
    if not current or current == boot_head:
        return False
    inspection_succeeded, output = _git_output_status(
        bridge_root,
        "diff",
        "--name-only",
        f"{boot_head}..{current}",
        "--",
        *WORKER_CODE_PATHS,
    )
    if not inspection_succeeded:
        return True
    return bool(output)
