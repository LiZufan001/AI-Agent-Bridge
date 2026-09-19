from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic-state"
PROJECTS = ROOT / "projects"
PORTFOLIO = ROOT / "supervisor" / "portfolio.json"
GOAL_META_RE = re.compile(r"<!--\s*bridge-goal:\s*(\{.*?\})\s*-->")
CURRENT_GOAL_RE = re.compile(r"<!--\s*bridge-current-goal:\s*(\{.*?\})\s*-->")
OWNER_FIELD_RE = re.compile(r"^\s*-\s+([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _goal_meta(path: Path) -> dict | None:
    match = GOAL_META_RE.search(path.read_text(encoding="utf-8"))
    return json.loads(match.group(1)) if match else None


def _current_goal_meta(path: Path) -> dict | None:
    match = CURRENT_GOAL_RE.search(path.read_text(encoding="utf-8"))
    return json.loads(match.group(1)) if match else None


def _owner_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            break
        match = OWNER_FIELD_RE.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip("`")
    return fields


def _optional_owner_ref(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() in {"null", "none"}:
        return None
    return normalized


class PortfolioReconciliationConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        portfolio = _load_json(PORTFOLIO)
        self.entries = {
            item["project_id"]: item
            for item in portfolio.get("projects", [])
            if item.get("owner_selected") is True
        }

    def test_nonpaused_projects_do_not_have_stale_pause_assertions_in_operational_supervisor_docs(self) -> None:
        for project_id, entry in self.entries.items():
            if entry.get("owner_paused") is not False:
                continue
            for context_dir in PROJECTS.iterdir():
                supervisor = context_dir / "SUPERVISOR.md"
                if not supervisor.exists():
                    continue
                for line_number, line in enumerate(
                    supervisor.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    lowered = line.lower()
                    if project_id.lower() not in lowered:
                        continue
                    stale_pause = (
                        "remains owner-paused" in lowered
                        or "stays owner-paused" in lowered
                        or "is owner-paused" in lowered
                        or "remains owner paused" in lowered
                        or "stays owner paused" in lowered
                        or "is owner paused" in lowered
                    )
                    self.assertFalse(
                        stale_pause,
                        f"{supervisor.relative_to(ROOT)}:{line_number} hard-codes "
                        f"{project_id} as paused while supervisor/portfolio.json says owner_paused=false",
                    )

    def test_current_goal_pointer_matches_exactly_one_active_goal(self) -> None:
        for project_id, entry in self.entries.items():
            project_dir = PROJECTS / project_id
            goals_dir = project_dir / "goals"
            if not goals_dir.exists():
                continue

            active: list[tuple[Path, dict]] = []
            for goal_path in sorted(goals_dir.glob("goal-*.md")):
                meta = _goal_meta(goal_path)
                if meta and meta.get("status") == "ACTIVE":
                    active.append((goal_path, meta))

            self.assertLessEqual(
                len(active),
                1,
                f"{project_id} has more than one ACTIVE planning goal",
            )

            current_path = project_dir / "CURRENT_GOAL.md"
            if not active:
                # A paused, terminal, acceptance-only, or not-yet-activated project
                # may legitimately have no current pointer.
                continue

            self.assertTrue(
                current_path.exists(),
                f"{project_id} has an ACTIVE goal but no CURRENT_GOAL.md pointer",
            )
            pointer = _current_goal_meta(current_path)
            self.assertIsNotNone(
                pointer,
                f"{current_path.relative_to(ROOT)} lacks bridge-current-goal metadata",
            )
            active_path, active_meta = active[0]
            self.assertEqual(pointer.get("goal_id"), active_meta.get("goal_id"))
            self.assertEqual(pointer.get("goal_file"), f"goals/{active_path.name}")

            if entry.get("owner_paused") is False:
                self.assertNotIn(
                    "remains owner-paused",
                    current_path.read_text(encoding="utf-8").lower(),
                    f"{current_path.relative_to(ROOT)} embeds stale portfolio pause state",
                )

    def test_human_required_projects_have_owner_action_evidence(self) -> None:
        for project_id in self.entries:
            project_dir = PROJECTS / project_id
            state_path = project_dir / "state.json"
            if not state_path.exists():
                continue
            state = _load_json(state_path)
            if state.get("status") != "HUMAN_REQUIRED":
                continue

            action_paths = sorted((project_dir / "owner-actions").glob("owner-action-*.md"))
            self.assertTrue(
                action_paths,
                f"{project_id} is HUMAN_REQUIRED but has no owner-action evidence",
            )

            # If a thread has newer linked events, callers must use the newest
            # event rather than treating an immutable root AWAITING_OWNER record
            # as current forever. Validate the thread linkage is at least
            # mechanically inspectable and non-ambiguous by action id.
            seen_ids: set[str] = set()
            for path in action_paths:
                fields = _owner_fields(path)
                action_id = fields.get("action_id")
                if action_id:
                    self.assertNotIn(action_id, seen_ids, f"duplicate owner action id {action_id}")
                    seen_ids.add(action_id)
                root = _optional_owner_ref(fields.get("root_action_id"))
                relates_to = _optional_owner_ref(fields.get("relates_to"))
                if root and root != action_id:
                    self.assertIn(root, seen_ids, f"{path.name} references unknown/later root {root}")
                if relates_to:
                    self.assertIn(relates_to, seen_ids, f"{path.name} references unknown/later event {relates_to}")


if __name__ == "__main__":
    unittest.main()
