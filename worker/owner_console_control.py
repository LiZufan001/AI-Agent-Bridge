"""Owner operations over existing portfolio authority and exact Worker run scopes.

The console never writes Protocol state. Pause uses the existing Git CAS store;
stop is a run-bound request consumed only by that run's lifecycle owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import git_store
from bridge_common import WorkerError

VERSION = 1
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ControlConflict(WorkerError):
    pass


def read_object(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ControlConflict("evidence_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ControlConflict("invalid_evidence")
    return value


def revision(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def portfolio_entry(root: Path, project: str) -> dict[str, Any] | None:
    path = root / "supervisor/portfolio.json"
    if not path.exists():
        return None  # Existing standalone Worker deployments have no portfolio.
    value = read_object(path)
    entries = value.get("projects")
    if not isinstance(entries, list):
        raise ControlConflict("portfolio_invalid")
    matches = [v for v in entries if isinstance(v, dict) and v.get("project_id") == project]
    if len(matches) > 1:
        raise ControlConflict("portfolio_duplicate")
    return matches[0] if matches else None


def require_project_admission(root: Path, project: str) -> None:
    entry = portfolio_entry(root, project)
    if entry is not None and (entry.get("owner_paused") is not False or entry.get("owner_selected") is not True):
        raise ControlConflict("owner_project_paused")


def set_project_paused(root: Path, project: str, paused: bool, expected_revision: str) -> dict[str, Any]:
    if not ID.fullmatch(project) or type(paused) is not bool:
        raise ControlConflict("invalid_request")
    path = root / "supervisor/portfolio.json"

    def build(current: dict[str, Any]) -> dict[Path, str]:
        entries = current.get("projects", [])
        matches = [v for v in entries if isinstance(v, dict) and v.get("project_id") == project]
        if len(matches) != 1 or matches[0].get("owner_selected") is not True:
            raise ControlConflict("project_not_selected")
        matches[0]["owner_paused"] = paused
        return {path: json.dumps(current, ensure_ascii=False, indent=2) + "\n"}

    # All reads/commit/push/retries share the existing Worker Git mutation gate.
    result = git_store.publish_cas(
        bridge_root=root, state_path=path,
        expected=lambda current: revision(current) == expected_revision,
        already_applied=lambda current: False,
        payload_builder=build,
        message=f"owner: {'pause' if paused else 'resume'} project {project}",
    )
    return {"status": "applied", "revision": revision(result)}


def run_directory(root: Path, project: str, run_id: str) -> Path:
    if not ID.fullmatch(project) or not ID.fullmatch(run_id):
        raise ControlConflict("invalid_run_identity")
    base = root.resolve() / "worker/runtime"
    path = base / project / "runs" / run_id
    # Reject links/junctions at every level, including runtime itself.
    if path.resolve() != path.absolute() or base.resolve() != base.absolute():
        raise ControlConflict("runtime_path_redirected")
    return path


def exact_active(root: Path, project: str, identity: dict[str, Any]) -> dict[str, Any]:
    if not ID.fullmatch(project):
        raise ControlConflict("invalid_project")
    if any(type(identity.get(key)) is not int for key in ('command_id', 'generation')):
        raise ControlConflict('invalid_run_identity')
    state = read_object(root / "projects" / project / "state.json")
    active = state.get("active_run")
    if (state.get("status") != "CODEX_RUNNING" or not isinstance(active, dict)
            or state.get("generation") != identity.get("generation")
            or active.get("claimed_generation") != identity.get("generation")
            or state.get("latest_command") != identity.get("command_id")
            or active.get("command_id") != identity.get("command_id")
            or active.get("run_id") != identity.get("run_id")):
        raise ControlConflict("run_changed")
    return active


def request_stop(root: Path, project: str, identity: dict[str, Any]) -> dict[str, Any]:
    directory = run_directory(root, project, str(identity.get("run_id", "")))
    with git_store.git_mutation_gate(root):
        exact_active(root, project, identity)
        if not directory.is_dir():
            raise ControlConflict("run_not_observable")
        path = directory / "owner-stop.json"
        if path.resolve() != path.absolute():
            raise ControlConflict('runtime_path_redirected')
        payload = {"schema_version": VERSION, "project_id": project,
                   "run_id": identity["run_id"], "command_id": identity["command_id"],
                   "generation": identity["generation"], "reason": "owner_stop",
                   "requested_at": datetime.now(timezone.utc).isoformat()}
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            previous = read_object(path)
            if any(previous.get(k) != payload[k] for k in ("project_id", "run_id", "command_id", "generation")):
                raise ControlConflict("stop_identity_conflict")
        return {"status": "requested", "run_id": identity["run_id"]}


def stop_requested(root: Path, project: str, identity: dict[str, Any]) -> bool:
    """Called by the owning lifecycle; stale requests never reach another run."""
    path = run_directory(root, project, identity["run_id"]) / "owner-stop.json"
    if not path.exists():
        return False
    try:
        value = read_object(path)
        if value.get("schema_version") != VERSION or value.get("reason") != "owner_stop":
            return False
        if value.get("project_id") != project or any(value.get(k) != identity[k] for k in ("run_id", "command_id", "generation")):
            return False
        exact_active(root, project, identity)
        return True
    except (OSError, ValueError, WorkerError):
        return False
