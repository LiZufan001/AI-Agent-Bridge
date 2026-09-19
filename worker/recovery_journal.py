#!/usr/bin/env python3
"""Durable, allowlisted local evidence for interrupted Bridge runs.

Recovery journals live below the ignored Worker runtime directory.  They are
deliberately independent from the GitHub-backed protocol state: a journal is
written before the Worker attempts a remote recovery publication, so a short
network outage cannot erase the fact that Codex was terminated.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
JOURNAL_STATUSES = frozenset({"pending", "reconciled", "superseded", "conflict"})

# This is the complete on-disk schema.  Keep this list intentionally small:
# command text, Codex stdin, raw logs, and arbitrary exception objects must
# never enter a recovery journal.
JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "command_id",
        "run_id",
        "claim_generation",
        "interrupted_at",
        "interruption_kind",
        "interruption_reason_safe",
        "claimed_at",
        "lease_expires_at",
        "head_before",
        "head_after",
        "worktree_dirty",
        "local_commit_created",
        "unpushed_commits_present",
        "report_path",
        "pending_report_path",
        "marker_status",
        "process_exit_code",
        "termination_reason",
        "external_side_effects_unknown",
        "remote_publish_pending",
        "journal_status",
        "reconciled_at",
        "reconciliation_reason",
    }
)

REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "command_id",
        "run_id",
        "claim_generation",
        "interrupted_at",
        "interruption_kind",
        "interruption_reason_safe",
        "remote_publish_pending",
        "journal_status",
    }
)

_TEXT_FIELDS = {
    "project_id",
    "run_id",
    "interrupted_at",
    "interruption_kind",
    "interruption_reason_safe",
    "claimed_at",
    "lease_expires_at",
    "head_before",
    "head_after",
    "report_path",
    "pending_report_path",
    "marker_status",
    "termination_reason",
    "reconciled_at",
    "reconciliation_reason",
}
_BOOL_FIELDS = {
    "worktree_dirty",
    "local_commit_created",
    "unpushed_commits_present",
    "external_side_effects_unknown",
    "remote_publish_pending",
}
_INT_FIELDS = {"command_id", "claim_generation", "process_exit_code"}
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s]+"),
    re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s]+"),
    re.compile(r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[-A-Za-z0-9_]{8,}\b"),
)


class RecoveryJournalError(ValueError):
    """The local recovery journal is malformed or cannot be persisted."""


def _safe_text(value: Any, *, limit: int = 512) -> str:
    text = str(value).replace("\x00", " ")
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex
                else "[REDACTED_TOKEN]"
            ),
            text,
        )
    # Reasons are diagnostics, not a second log channel.  Collapse newlines
    # and bound their size so a caller cannot smuggle raw stderr into a journal.
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]", "_", str(value).strip())
    if component in {"", ".", ".."}:
        raise RecoveryJournalError("Recovery journal path component is invalid.")
    return component[:160]


def _normalize_value(key: str, value: Any) -> Any:
    if key == "schema_version":
        if isinstance(value, bool) or value is None:
            raise RecoveryJournalError("Journal schema_version must be an integer.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RecoveryJournalError(
                "Journal schema_version must be an integer."
            ) from exc
    if key in _TEXT_FIELDS:
        if value is None:
            return None
        return _safe_text(value)
    if key in _BOOL_FIELDS:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise RecoveryJournalError(f"Journal field {key} must be boolean or null.")
        return value
    if key in _INT_FIELDS:
        if value is None:
            return None
        if isinstance(value, bool):
            raise RecoveryJournalError(f"Journal field {key} must be integer or null.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RecoveryJournalError(
                f"Journal field {key} must be integer or null."
            ) from exc
    if key == "journal_status":
        return _safe_text(value, limit=32)
    return None


def normalize_journal(data: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown fields and normalize the allowlisted journal schema."""
    if not isinstance(data, dict):
        raise RecoveryJournalError("Recovery journal must be a JSON object.")
    normalized: dict[str, Any] = {}
    for key in JOURNAL_FIELDS:
        if key in data:
            normalized[key] = _normalize_value(key, data[key])
    return normalized


def validate_journal(data: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_journal(data)
    missing = sorted(REQUIRED_FIELDS - set(normalized))
    if missing:
        raise RecoveryJournalError(
            "Recovery journal is missing required fields: " + ", ".join(missing)
        )
    if normalized["schema_version"] != SCHEMA_VERSION:
        raise RecoveryJournalError("Unsupported recovery journal schema_version.")
    if not normalized["project_id"] or not normalized["run_id"]:
        raise RecoveryJournalError("Recovery journal identity fields must be non-empty.")
    if normalized["command_id"] is None or int(normalized["command_id"]) <= 0:
        raise RecoveryJournalError("Recovery journal command_id must be positive.")
    if (
        normalized["claim_generation"] is None
        or int(normalized["claim_generation"]) < 0
    ):
        raise RecoveryJournalError("Recovery journal claim_generation must be non-negative.")
    if normalized["journal_status"] not in JOURNAL_STATUSES:
        raise RecoveryJournalError("Recovery journal has an unsupported journal_status.")
    if not isinstance(normalized["remote_publish_pending"], bool):
        raise RecoveryJournalError("Recovery journal remote_publish_pending must be boolean.")
    return normalized


def recovery_root(bridge_root: Path, project_id: str) -> Path:
    return (
        bridge_root
        / "worker"
        / "runtime"
        / _safe_component(project_id)
        / "recovery"
    )


def journal_path(bridge_root: Path, project_id: str, run_id: str) -> Path:
    return recovery_root(bridge_root, project_id) / f"{_safe_component(run_id)}.json"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        flags = getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(path), os.O_RDONLY | flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_journal(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_journal(data)
    _write_atomic(path, normalized)
    return normalized


def read_journal(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryJournalError(f"Unable to read recovery journal: {path}") from exc
    return validate_journal(raw)


def update_journal(path: Path, **updates: Any) -> dict[str, Any]:
    current = read_journal(path)
    current.update({key: value for key, value in updates.items() if key in JOURNAL_FIELDS})
    return write_journal(path, current)


def pending_journal_paths(
    bridge_root: Path,
    *,
    project_id: str | None = None,
) -> Iterable[Path]:
    runtime = bridge_root / "worker" / "runtime"
    if project_id is not None:
        roots = [recovery_root(bridge_root, project_id)]
    else:
        roots = [path for path in runtime.glob("*/recovery") if path.is_dir()]
    for root in sorted(roots, key=lambda path: str(path).casefold()):
        for path in sorted(root.glob("*.json"), key=lambda item: item.name.casefold()):
            yield path
