#!/usr/bin/env python3
"""Read-only project/Goal/Owner inbox projection; no independently saved status."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import state_roots
from typing import Any

META = re.compile(r"<!--\s*(bridge-goal|bridge-current-goal):\s*(\{.*?\})\s*-->", re.DOTALL)
ACTION = re.compile(r"^owner-action-(\d+)$")
FIELDS = re.compile(r"^- ([a-z_]+):\s*(.*?)\s*$", re.MULTILINE)
MAX_EVIDENCE_BYTES = 256 * 1024


class ProjectViewError(RuntimeError):
    """Project-view evidence is ambiguous, malformed, or unsafe to trust."""


def _strict_json_load(raw: bytes | str, *, limit: int = MAX_EVIDENCE_BYTES) -> Any:
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
        text = raw
    elif isinstance(raw, bytes):
        encoded = raw
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectViewError("JSON_UTF8_INVALID") from exc
    else:
        raise ProjectViewError("JSON_INPUT_INVALID")
    if len(encoded) > limit:
        raise ProjectViewError("JSON_TOO_LARGE")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ProjectViewError("JSON_DUPLICATE_KEY")
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ProjectViewError("JSON_NONFINITE_NUMBER")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except ProjectViewError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProjectViewError("JSON_INVALID") from exc


def metadata(path: Path, kind: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ProjectViewError("PROJECT_METADATA_UNREADABLE")
    matches = [m for m in META.finditer(path.read_text(encoding="utf-8")) if m.group(1) == kind]
    if len(matches) != 1:
        raise ProjectViewError("PROJECT_METADATA_AMBIGUOUS")
    value = _strict_json_load(matches[0].group(2))
    if not isinstance(value, dict):
        raise ProjectViewError("PROJECT_METADATA_INVALID")
    return value


def owner_threads(
    project: Path, *, enforce_blocker_consistency: bool = True
) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for path in sorted((project / "owner-actions").glob("owner-action-*.md")):
        if not ACTION.fullmatch(path.stem) or path.is_symlink() or path.stat().st_size > MAX_EVIDENCE_BYTES:
            raise ProjectViewError("OWNER_EVENT_PATH_INVALID")
        pairs = FIELDS.findall(path.read_text(encoding="utf-8"))
        fields: dict[str, str] = {}
        for key, value in pairs:
            # Legacy bodies may repeat prose fields; identity/progress ambiguity
            # must never be silently reconciled by choosing the last occurrence.
            if key in fields and key in {
                "action_id",
                "root_action_id",
                "relates_to",
                "blocker_key",
                "owner_status",
                "verification_status",
            }:
                raise ProjectViewError("OWNER_EVENT_AMBIGUOUS")
            fields[key] = value.strip("`\"")
        action_id = fields.get("action_id", path.stem)
        if action_id != path.stem:
            raise ProjectViewError("OWNER_EVENT_ID_MISMATCH")
        fields["action_id"] = action_id
        events[action_id] = fields
    roots: dict[str, list[dict[str, Any]]] = {}
    for event in events.values():
        root_id = event.get("root_action_id", event["action_id"])
        if root_id not in events:
            raise ProjectViewError("OWNER_ROOT_MISSING")
        root = events[root_id]
        if root.get("root_action_id", root_id) != root_id:
            raise ProjectViewError("OWNER_ROOT_NOT_ROOT")
        if (
            enforce_blocker_consistency
            and event.get("blocker_key")
            and root.get("blocker_key")
            and event["blocker_key"] != root["blocker_key"]
        ):
            raise ProjectViewError("OWNER_BLOCKER_MISMATCH")
        related = event.get("relates_to")
        if related and related not in {"null", "None", "N/A", event["action_id"]}:
            if related not in events or (
                event["action_id"] != root_id
                and events[related].get("root_action_id", related) != root_id
            ):
                raise ProjectViewError("OWNER_THREAD_LINK_INVALID")
            if int(related.rsplit("-", 1)[1]) >= int(event["action_id"].rsplit("-", 1)[1]):
                raise ProjectViewError("OWNER_EVENT_ORDER_INVALID")
        roots.setdefault(root_id, []).append(event)
    result = []
    for root_id, thread in roots.items():
        newest = max(thread, key=lambda e: int(e["action_id"].rsplit("-", 1)[1]))
        owner = newest.get("owner_status")
        verified = newest.get("verification_status", "PENDING")
        if owner == "OWNER_REPORTED_DONE":
            disposition = {
                "PENDING": "VERIFICATION_PENDING",
                "VERIFIED": "RESUME_PENDING",
                "FAILED": "VERIFICATION_FAILED",
            }.get(verified, "OWNER_EVIDENCE_UNKNOWN")
        else:
            disposition = {
                "AWAITING_OWNER": "AWAITING_OWNER",
                "OWNER_IN_PROGRESS": "OWNER_IN_PROGRESS",
            }.get(owner, "OWNER_EVIDENCE_UNKNOWN")
        result.append(
            {
                "root_action_id": root_id,
                "latest_action_id": newest["action_id"],
                "blocker_key": newest.get("blocker_key", events[root_id].get("blocker_key")),
                "disposition": disposition,
                "relates_to_report": newest.get(
                    "relates_to_report",
                    newest.get("related_report", events[root_id].get("relates_to_report")),
                ),
            }
        )
    return sorted(result, key=lambda x: x["root_action_id"])


def project_view(
    root: Path, project_id: str, *, archival: bool = False
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id):
        raise ProjectViewError("INVALID_PROJECT_ID")
    project = root / "projects" / project_id
    if project.is_symlink():
        raise ProjectViewError("SYMLINK_PROJECT")
    if any((project / name).is_symlink() for name in ("state.json", "goals", "owner-actions")):
        raise ProjectViewError("SYMLINK_PROJECT_EVIDENCE")
    with (project / "state.json").open("rb") as source:
        state = _strict_json_load(source.read(MAX_EVIDENCE_BYTES + 1))
    if not isinstance(state, dict) or state.get("project_id") != project_id:
        raise ProjectViewError("PROJECT_STATE_ID_MISMATCH")
    goals: list[dict[str, Any]] = []
    for path in sorted((project / "goals").glob("goal-*.md")):
        goal = metadata(path, "bridge-goal")
        goal_id = goal.get("goal_id")
        if type(goal_id) is not int or goal_id < 1 or path.name != f"goal-{goal_id:03d}.md":
            raise ProjectViewError("GOAL_ID_MISMATCH")
        if goal.get("status") == "ACTIVE":
            goals.append({"goal_id": goal_id, "goal_file": "goals/" + path.name})
    if len(goals) > 1:
        raise ProjectViewError("MULTIPLE_ACTIVE_GOALS")
    pointer_path = project / "CURRENT_GOAL.md"
    if pointer_path.exists():
        pointer = metadata(pointer_path, "bridge-current-goal")
        if not goals or any(pointer.get(k) != goals[0][k] for k in ("goal_id", "goal_file")):
            raise ProjectViewError("STALE_CURRENT_GOAL_POINTER")
    elif goals:
        raise ProjectViewError("CURRENT_GOAL_POINTER_MISSING")
    threads = owner_threads(project, enforce_blocker_consistency=not archival)
    for thread in threads:
        thread["matches_latest_report"] = str(thread["relates_to_report"]) == str(state.get("latest_report"))
        thread["requires_attention"] = thread["matches_latest_report"] and thread["disposition"] != "RESUME_PENDING"
    return {
        "project_id": project_id,
        "canonical": {
            k: state.get(k)
            for k in (
                "status",
                "generation",
                "latest_command",
                "latest_report",
                "last_reviewed_report",
                "active_run",
            )
        },
        "active_goal": goals[0] if goals else None,
        "owner_threads": threads,
        "view_authority": "derived_read_only",
    }


def overview(root: Path) -> dict[str, Any]:
    portfolio = _strict_json_load((root / "supervisor/portfolio.json").read_bytes())
    if not isinstance(portfolio, dict) or not isinstance(portfolio.get("projects"), list):
        raise ProjectViewError("PORTFOLIO_INVALID")
    rows = []
    seen: set[str] = set()
    for entry in sorted(portfolio["projects"], key=lambda item: item["priority_rank"]):
        if entry.get("owner_selected") is not True:
            continue
        project_id = entry["project_id"]
        if project_id in seen:
            raise ProjectViewError("DUPLICATE_PORTFOLIO_PROJECT")
        seen.add(project_id)
        try:
            row = project_view(root, project_id)
        except (ProjectViewError, OSError, UnicodeDecodeError) as exc:
            row = {
                "project_id": project_id,
                "error": str(exc) if isinstance(exc, ProjectViewError) else "PROJECT_IO_FAILED",
            }
        rows.append(
            {
                **row,
                "owner_paused": entry["owner_paused"],
                "priority_rank": entry["priority_rank"],
            }
        )
    return {
        "schema_version": 1,
        "projects": rows,
        "healthy": not any("error" in row for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", "--root", dest="root", type=Path)
    args = parser.parse_args()
    args.root = state_roots.resolve_state_root(args.root)
    try:
        result = overview(args.root)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result["healthy"] else 1
    except (OSError, ProjectViewError, KeyError, TypeError):
        print(json.dumps({"healthy": False, "error": "OVERVIEW_UNAVAILABLE"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
