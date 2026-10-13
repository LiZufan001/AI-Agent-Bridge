#!/usr/bin/env python3
"""Freshness-enforcing façade for the staged Supervisor publication gateway.

The stable parsing/CAS implementation lives in ``supervisor_publication_gateway_core``.
This façade adds transport-only invariants: a staged request is eligible for
consumption for at most ten minutes from the Git commit that introduced that
immutable request path, and a ``HUMAN_REQUIRED`` project may resume only from
current verified owner-action evidence already defined by Protocol v2.

Freshness and owner-action inspection are non-canonical.  They never add a
state, consume a command id, or authorize execution by themselves.  The same
existing command publication CAS remains the only canonical write boundary.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import bridge_common
import git_store
import phased_task
import protocol_core
import worker_execution_control
import supervisor_publication_gateway_core as _core
from supervisor_publication_gateway_core import *  # noqa: F401,F403


# Preserve compatibility for callers/tests that historically imported this
# bounded scan constant even though it was not part of the old __all__ list.
MAX_SCAN_ENTRIES = _core.MAX_SCAN_ENTRIES
MAX_STAGED_REQUEST_AGE_SECONDS = 600
MAX_STAGED_REQUEST_FUTURE_SKEW_SECONDS = 60
MAX_OWNER_ACTION_ENTRIES = 512
MAX_OWNER_ACTION_BYTES = 256 * 1024
_OWNER_ACTION_NAME_RE = re.compile(r"^owner-action-(\d+)\.md$", re.IGNORECASE)
_EXTRA_SAFE_REASONS = frozenset(
    {
        "request_expired",
        "request_commit_time_future",
        "request_commit_time_unavailable",
        "owner_execution_blocked",
    }
)


def _parse_offset_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _tracked_request_commit_time(
    bridge_root: Path,
    request_path: Path,
) -> datetime | None:
    """Return the committer timestamp of the immutable tracked request path."""

    try:
        relative = git_store.relative_path(request_path, bridge_root)
        result = git_store.git(
            bridge_root,
            "log",
            "-1",
            "--format=%cI",
            "--",
            relative,
        )
    except (OSError, ValueError, git_store.WorkerError):
        return None
    text = str(getattr(result, "stdout", "")).strip()
    return _parse_offset_timestamp(text)


def _request_freshness_reason(
    bridge_root: Path,
    request_path: Path,
    *,
    now: datetime | None = None,
) -> str | None:
    committed_at = _tracked_request_commit_time(bridge_root, request_path)
    if committed_at is None:
        return "request_commit_time_unavailable"
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if committed_at > current + timedelta(seconds=MAX_STAGED_REQUEST_FUTURE_SKEW_SECONDS):
        return "request_commit_time_future"
    if current - committed_at > timedelta(seconds=MAX_STAGED_REQUEST_AGE_SECONDS):
        return "request_expired"
    return None


def _owner_action_fields(raw_bytes: bytes) -> dict[str, str] | None:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        key, separator, value = line[2:].partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key in fields:
            return None
        fields[key] = value
    return fields


def _verified_owner_resume_evidence(
    bridge_root: Path,
    project_root: Path,
    latest_report: int,
) -> str | None:
    """Return a stable fingerprint for the newest verified action on this report.

    This is intentionally a narrow transport guard, not a second owner-action
    state machine.  The Supervisor still owns semantic review.  The local
    gateway merely re-proves that the current Git evidence it is about to bind
    is still ``OWNER_REPORTED_DONE / VERIFIED`` for the canonical report
    boundary being resumed.
    """

    owner_actions = project_root / "owner-actions"
    if owner_actions.is_symlink() or not owner_actions.is_dir():
        return None

    candidates: list[tuple[int, str, bytes, dict[str, str]]] = []
    try:
        with os.scandir(owner_actions) as entries:
            examined = 0
            for entry in entries:
                examined += 1
                if examined > MAX_OWNER_ACTION_ENTRIES:
                    return None
                match = _OWNER_ACTION_NAME_RE.fullmatch(entry.name)
                if match is None:
                    continue
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    return None
                path = Path(entry.path)
                try:
                    raw_bytes = git_store.read_blob(
                        bridge_root,
                        git_store.relative_path(path, bridge_root),
                        max_bytes=MAX_OWNER_ACTION_BYTES,
                    )
                except (OSError, ValueError, git_store.WorkerError):
                    return None
                fields = _owner_action_fields(raw_bytes)
                if fields is None:
                    return None
                relates_to_report = fields.get("relates_to_report")
                if relates_to_report is None:
                    # Legacy owner-action records without a report link are
                    # historical evidence only; they cannot authorize this
                    # exact canonical resume boundary.
                    continue
                try:
                    linked_report = int(relates_to_report)
                except ValueError:
                    return None
                if linked_report != latest_report:
                    continue
                action_id = fields.get("action_id")
                expected_action_id = entry.name[:-3]
                if action_id != expected_action_id:
                    return None
                # Historical root actions predate root_action_id.  They may
                # participate in ordering, but only the newest matching event
                # is allowed to authorize resume and must satisfy the current
                # complete verified-event contract below.
                if not fields.get("blocker_key"):
                    return None
                candidates.append((int(match.group(1)), entry.name, raw_bytes, fields))
    except OSError:
        return None

    if not candidates:
        return None
    _, name, raw_bytes, fields = max(candidates, key=lambda item: item[0])
    if not fields.get("root_action_id") or not fields.get("blocker_key"):
        return None
    if fields.get("owner_status") != "OWNER_REPORTED_DONE":
        return None
    if fields.get("verification_status") != "VERIFIED":
        return None
    if not fields.get("verification_source"):
        return None
    return _core.sha256(name.encode("utf-8") + b"\x00" + raw_bytes).hexdigest()


class SupervisorPublicationGateway(_core.SupervisorPublicationGateway):
    """Core gateway plus freshness, execution-control and owner-resume guards."""

    def _freshness_reason(self, request_path: Path) -> str | None:
        return _request_freshness_reason(self.bridge_root, request_path)

    def _execution_allowed(self) -> bool:
        return worker_execution_control.new_execution_allowed(self.bridge_root).execution_allowed

    def _owner_resume_evidence(self, project_root: Path, latest_report: int) -> str | None:
        return _verified_owner_resume_evidence(self.bridge_root, project_root, latest_report)

    @staticmethod
    def _result(
        request: _core.StagedPublicationRequest,
        outcome: str,
        reason: str,
    ) -> _core.GatewayResult:
        safe_reason = (
            reason
            if reason in _core._SAFE_REASONS or reason in _EXTRA_SAFE_REASONS
            else "error_safe"
        )
        return _core.GatewayResult(
            request_id=request.request_id,
            project_id=request.project_id,
            command_id=request.command_id,
            outcome=outcome,
            reason=safe_reason,
        )


__all__ = list(_core.__all__) + [
    "MAX_OWNER_ACTION_BYTES",
    "MAX_OWNER_ACTION_ENTRIES",
    "MAX_SCAN_ENTRIES",
    "MAX_STAGED_REQUEST_AGE_SECONDS",
    "MAX_STAGED_REQUEST_FUTURE_SKEW_SECONDS",
    "SupervisorPublicationGateway",
]
