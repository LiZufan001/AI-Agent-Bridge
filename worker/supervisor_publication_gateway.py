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

    def _consume_one(
        self,
        request_path: Path,
        request: _core.StagedPublicationRequest,
    ) -> _core.GatewayResult:
        freshness_reason = _request_freshness_reason(self.bridge_root, request_path)
        if freshness_reason == "request_expired":
            return self._result(request, "stale", freshness_reason)
        if freshness_reason is not None:
            return self._result(request, "invalid", freshness_reason)

        try:
            project_root = self._project_root(request.project_id)
            state_path = project_root / "state.json"
            commands_dir = project_root / "commands"
            reports_dir = project_root / "reports"
            if project_root.is_symlink() or commands_dir.is_symlink() or reports_dir.is_symlink():
                return self._result(request, "invalid", "project_path_invalid")
            if not commands_dir.is_dir() or not reports_dir.is_dir():
                return self._result(request, "invalid", "project_path_invalid")
            if state_path.is_symlink() or not state_path.is_file():
                return self._result(request, "invalid", "state_invalid")
            state = _core._read_state(state_path)
            if state.get("protocol_version") != protocol_core.PROTOCOL_VERSION:
                return self._result(request, "invalid", "state_invalid")
            if state.get("project_id") != request.project_id:
                return self._result(request, "invalid", "project_mismatch")
            if self._already_applied(project_root, state, request):
                return self._result(request, "already_applied", "already_applied")
            if not worker_execution_control.new_execution_allowed(self.bridge_root).execution_allowed:
                return self._result(request, "stale", "owner_execution_blocked")

            status = state.get("status")
            if (
                status in protocol_core.PENDING_STATES
                or status == "CODEX_RUNNING"
                or state.get("active_run") is not None
            ):
                return self._result(request, "active_project", "active_project")
            if status not in {"REPORT_READY", "HUMAN_REQUIRED"}:
                return self._result(request, "stale", "not_report_ready")

            generation = _core._bounded_int(state.get("generation"), "generation", minimum=0)
            latest_command = _core._bounded_int(
                state.get("latest_command"), "latest_command", minimum=0
            )
            latest_report = _core._bounded_int(
                state.get("latest_report"), "latest_report", minimum=0
            )
            if request.expected_generation != generation + 1:
                return self._result(request, "stale", "wrong_generation")
            if request.based_on_report != latest_report:
                return self._result(request, "stale", "wrong_based_on_report")
            if request.command_id != latest_command + 1:
                return self._result(request, "stale", "command_id_not_next")

            resume_after_owner = status == "HUMAN_REQUIRED"
            resume_evidence: str | None = None
            if resume_after_owner:
                if (
                    state.get("human_required") is not True
                    or request.source != "scheduled_chatgpt"
                    or request.kind != "EXECUTE"
                ):
                    return self._result(request, "stale", "not_report_ready")
                resume_evidence = _verified_owner_resume_evidence(
                    self.bridge_root,
                    project_root,
                    latest_report,
                )
                if resume_evidence is None:
                    return self._result(request, "stale", "not_report_ready")

            command_path = project_root / "commands" / f"command-{request.command_id:03d}.md"
            report_path = project_root / "reports" / f"report-{request.command_id:03d}.md"
            if command_path.is_symlink() or report_path.is_symlink():
                return self._result(request, "invalid", "project_path_invalid")
            if command_path.exists() or report_path.exists():
                return self._result(request, "invalid", "command_conflict")
            if not self._command_history_is_bounded_and_monotonic(
                project_root, request.command_id
            ):
                return self._result(request, "invalid", "command_history_invalid")

            target_status = _core._target_status(request.kind)
            projected = dict(state)
            projected["status"] = target_status
            projected["generation"] = request.expected_generation
            projected["latest_command"] = request.command_id
            projected["last_reviewed_report"] = request.based_on_report
            projected["active_run"] = None
            if resume_after_owner:
                projected["human_required"] = False
            try:
                _core.validate_exact_command_for_publication(
                    request.command_bytes,
                    state=projected,
                    target_status=target_status,
                    command_id=request.command_id,
                )
            except (protocol_core.ProtocolViolation, phased_task.PhasedTaskError):
                return self._result(request, "invalid", "preflight_failed")

            freshness_reason = _request_freshness_reason(self.bridge_root, request_path)
            if freshness_reason == "request_expired":
                return self._result(request, "stale", freshness_reason)
            if freshness_reason is not None:
                return self._result(request, "invalid", freshness_reason)

            initial_state_fingerprint = _core._state_fingerprint(state)
            initial_request_bytes = self._read_staged_blob(request_path)
            if initial_request_bytes != request.raw_bytes:
                return self._result(request, "cas_race", "request_changed")
            already_applied_seen = False

            def request_is_unchanged_and_fresh() -> bool:
                if _request_freshness_reason(self.bridge_root, request_path) is not None:
                    return False
                try:
                    return self._read_staged_blob(request_path) == initial_request_bytes
                except _core.StagedPublicationError:
                    return False

            def resume_evidence_is_unchanged() -> bool:
                if not resume_after_owner:
                    return True
                return (
                    _verified_owner_resume_evidence(
                        self.bridge_root,
                        project_root,
                        latest_report,
                    )
                    == resume_evidence
                )

            def state_is_eligible(current: dict[str, Any]) -> bool:
                return (
                    _core._state_fingerprint(current) == initial_state_fingerprint
                    and current.get("project_id") == request.project_id
                    and current.get("status") == status
                    and current.get("active_run") is None
                    and current.get("generation") == generation
                    and current.get("latest_command") == latest_command
                    and current.get("latest_report") == latest_report
                    and (
                        not resume_after_owner
                        or current.get("human_required") is True
                    )
                )

            def already_applied(current: dict[str, Any]) -> bool:
                nonlocal already_applied_seen
                if not request_is_unchanged_and_fresh():
                    raise bridge_common.CASConflict("staged request changed or expired")
                already_applied_seen = self._already_applied(project_root, current, request)
                return already_applied_seen

            def expected(current: dict[str, Any]) -> bool:
                return (
                    request_is_unchanged_and_fresh()
                    and resume_evidence_is_unchanged()
                    and state_is_eligible(current)
                )

            def payload_builder(current: dict[str, Any]) -> dict[Path, str | bytes]:
                if not worker_execution_control.new_execution_allowed(self.bridge_root).execution_allowed:
                    raise bridge_common.CASConflict("Owner execution control blocked publication")
                if (
                    not request_is_unchanged_and_fresh()
                    or not resume_evidence_is_unchanged()
                    or not state_is_eligible(current)
                ):
                    raise bridge_common.CASConflict("staged publication evidence changed or expired")
                fresh = _core._read_state(state_path)
                if not state_is_eligible(fresh):
                    raise bridge_common.CASConflict("canonical state changed before publish")
                if not resume_evidence_is_unchanged():
                    raise bridge_common.CASConflict("owner resume evidence changed before publish")
                if _request_freshness_reason(self.bridge_root, request_path) is not None:
                    raise bridge_common.CASConflict("staged request expired before publish")
                projected_fresh = dict(fresh)
                projected_fresh["status"] = target_status
                projected_fresh["generation"] = request.expected_generation
                projected_fresh["latest_command"] = request.command_id
                projected_fresh["last_reviewed_report"] = request.based_on_report
                projected_fresh["active_run"] = None
                if resume_after_owner:
                    projected_fresh["human_required"] = False
                try:
                    _core.validate_exact_command_for_publication(
                        request.command_bytes,
                        state=projected_fresh,
                        target_status=target_status,
                        command_id=request.command_id,
                    )
                except (protocol_core.ProtocolViolation, phased_task.PhasedTaskError) as exc:
                    raise bridge_common.CASConflict("exact command preflight changed") from exc
                if (
                    command_path.is_symlink()
                    or report_path.is_symlink()
                    or command_path.exists()
                    or report_path.exists()
                ):
                    raise bridge_common.CASConflict("command file appeared before publish")
                if not self._command_history_is_bounded_and_monotonic(
                    project_root, request.command_id
                ):
                    raise bridge_common.CASConflict("command history changed before publish")
                updated = dict(fresh)
                updated["status"] = target_status
                updated["generation"] = request.expected_generation
                updated["latest_command"] = request.command_id
                updated["last_reviewed_report"] = request.based_on_report
                updated["active_run"] = None
                if resume_after_owner:
                    updated["human_required"] = False
                updated["updated_at"] = _core._now_iso()
                return {
                    command_path: request.command_bytes,
                    state_path: _core._json_text(updated),
                }

            try:
                final_state = git_store.publish_cas(
                    bridge_root=self.bridge_root,
                    state_path=state_path,
                    expected=expected,
                    already_applied=already_applied,
                    payload_builder=payload_builder,
                    message=(
                        f"bridge: staged supervisor publication {request.project_id} "
                        f"command {request.command_id:03d}"
                    ),
                )
            except bridge_common.CASConflict:
                freshness_reason = _request_freshness_reason(self.bridge_root, request_path)
                if freshness_reason == "request_expired":
                    return self._result(request, "stale", freshness_reason)
                if freshness_reason is not None:
                    return self._result(request, "invalid", freshness_reason)
                return self._result(request, "cas_race", "cas_race")
            except (OSError, ValueError, git_store.WorkerError):
                return self._result(request, "error_safe", "error_safe")
            except Exception:
                return self._result(request, "error_safe", "error_safe")

            if not isinstance(final_state, Mapping):
                return self._result(request, "error_safe", "error_safe")
            try:
                canonical_bytes = git_store.read_blob(
                    self.bridge_root,
                    git_store.relative_path(command_path, self.bridge_root),
                    max_bytes=_core.MAX_COMMAND_BYTES,
                )
                verified_state = _core._read_state(state_path)
            except (OSError, _core.StagedPublicationError, git_store.WorkerError, ValueError):
                return self._result(request, "error_safe", "error_safe")
            if (
                canonical_bytes != request.command_bytes
                or _core.sha256(canonical_bytes).hexdigest() != request.command_sha256
                or verified_state.get("project_id") != request.project_id
                or verified_state.get("latest_command") != request.command_id
                or verified_state.get("generation") != request.expected_generation
                or verified_state.get("last_reviewed_report") != request.based_on_report
                or verified_state.get("status") != target_status
                or verified_state.get("active_run") is not None
                or (
                    resume_after_owner
                    and verified_state.get("human_required") is not False
                )
            ):
                return self._result(request, "error_safe", "error_safe")
            return self._result(
                request,
                "already_applied" if already_applied_seen else "published",
                "already_applied" if already_applied_seen else "published",
            )
        except _core.StagedPublicationError as exc:
            return self._result(request, "invalid", exc.reason)
        except Exception:
            return self._result(request, "error_safe", "error_safe")

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
