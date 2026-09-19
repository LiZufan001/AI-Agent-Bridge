#!/usr/bin/env python3
"""Explicit operator-only repair for one unclaimed invalid Protocol-v2 command.

This module is deliberately narrower than the manual fast lane and separate
from recovery.  It can replace only the canonical, still-unclaimed command
when the command itself has a deterministic Protocol-v2 contract error.  It
never claims a command, creates a run, invokes an executor, writes a report,
or writes recovery evidence.
The staged-repair entry point additionally adopts one already-written,
immutable replacement command.  It publishes only the canonical state pointer
and never writes either command file.
"""

from __future__ import annotations

import state_roots

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bridge_common
import git_store
import protocol_core


COMMAND_REPAIR_EVENT = "operator.repair_unclaimed_command"
STAGED_REPAIR_EVENT = "operator.adopt_staged_repair"
COMMAND_FILE_RE = re.compile(r"^command-(\d+)\.md$", re.IGNORECASE)
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CommandRepairError(bridge_common.WorkerError):
    """The operator repair request is malformed or not eligible."""


class CommandRepairConflict(bridge_common.CASConflict, CommandRepairError):
    """Canonical state or immutable command evidence changed during repair."""


@dataclass(frozen=True)
class CommandFileEvidence:
    """Read-only evidence for one command file."""

    path: Path
    text: str | None
    sha256: str | None
    meta: dict[str, Any] | None
    parse_error: str | None
    filename_command_id: int | None


@dataclass(frozen=True)
class CommandRepairInspection:
    """Read-only repair eligibility evidence."""

    bridge_root: Path
    project_id: str
    target_command_id: int
    state: dict[str, Any]
    target: CommandFileEvidence
    target_report_exists: bool
    assessment: protocol_core.CommandRepairAssessment


def _safe_project_id(value: Any) -> str:
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value):
        raise CommandRepairError("project must be a safe project id")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CommandRepairError(f"{label} must be a positive integer")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _project_dir(bridge_root: Path, project_id: str) -> Path:
    return bridge_root / "projects" / _safe_project_id(project_id)


def _state_path(bridge_root: Path, project_id: str) -> Path:
    return _project_dir(bridge_root, project_id) / "state.json"


def _command_path(
    bridge_root: Path,
    project_id: str,
    command_id: int,
) -> Path:
    return _project_dir(bridge_root, project_id) / "commands" / f"command-{command_id:03d}.md"


def _report_path(
    bridge_root: Path,
    project_id: str,
    command_id: int,
) -> Path:
    return _project_dir(bridge_root, project_id) / "reports" / f"report-{command_id:03d}.md"


def _read_command(path: Path, filename_command_id: int | None) -> CommandFileEvidence:
    if not path.is_file():
        return CommandFileEvidence(
            path=path,
            text=None,
            sha256=None,
            meta=None,
            parse_error="canonical command file is missing",
            filename_command_id=filename_command_id,
        )
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return CommandFileEvidence(
            path=path,
            text=None,
            sha256=None,
            meta=None,
            parse_error=f"canonical command is missing or not valid UTF-8 ({type(exc).__name__})",
            filename_command_id=filename_command_id,
        )

    try:
        meta = protocol_core.parse_command_metadata(text)
    except protocol_core.ProtocolViolation as exc:
        return CommandFileEvidence(
            path=path,
            text=text,
            sha256=_sha256(raw),
            meta=None,
            parse_error=str(exc),
            filename_command_id=filename_command_id,
        )
    return CommandFileEvidence(
        path=path,
        text=text,
        sha256=_sha256(raw),
        meta=meta,
        parse_error=None,
        filename_command_id=filename_command_id,
    )


def _read_replacement(path_value: str | Path) -> tuple[Path, bytes, str, dict[str, Any], int]:
    path = Path(path_value).expanduser().resolve()
    match = COMMAND_FILE_RE.fullmatch(path.name)
    if match is None:
        raise CommandRepairError(
            "replacement command filename must be command-<id>.md"
        )
    filename_command_id = int(match.group(1))
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CommandRepairError(
            "replacement command is missing or not valid UTF-8"
        ) from exc
    try:
        meta = protocol_core.parse_command_metadata(text)
    except protocol_core.ProtocolViolation as exc:
        raise CommandRepairError(
            f"replacement command metadata is invalid: {exc}"
        ) from exc
    errors = protocol_core.command_contract_errors(
        meta,
        filename_command_id=filename_command_id,
    )
    if errors:
        raise CommandRepairError(
            "replacement command is not a complete Protocol-v2 command: "
            + "; ".join(errors)
        )
    return path, raw, text, meta, filename_command_id


def _load_state(bridge_root: Path, project_id: str) -> dict[str, Any]:
    path = _state_path(bridge_root, project_id)
    try:
        return bridge_common.load_json(path)
    except (OSError, bridge_common.WorkerError, json.JSONDecodeError) as exc:
        raise CommandRepairError("canonical state.json cannot be read") from exc


def _existing_command_ids(project_dir: Path) -> set[int]:
    commands_dir = project_dir / "commands"
    if not commands_dir.is_dir():
        return set()
    values: set[int] = set()
    for path in commands_dir.iterdir():
        if not path.is_file():
            continue
        match = COMMAND_FILE_RE.fullmatch(path.name)
        if match:
            values.add(int(match.group(1)))
    return values


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _command_bytes_snapshot(project_dir: Path) -> dict[Path, bytes]:
    """Read every existing command's exact bytes for an immutable CAS guard."""

    commands_dir = project_dir / "commands"
    if not commands_dir.is_dir():
        return {}
    snapshot: dict[Path, bytes] = {}
    for path in sorted(commands_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or COMMAND_FILE_RE.fullmatch(path.name) is None:
            continue
        try:
            snapshot[path] = path.read_bytes()
        except (OSError, IsADirectoryError) as exc:
            raise CommandRepairError(
                f"command history cannot be read safely: {path.name}"
            ) from exc
    return snapshot


def _existing_supersede_relations(project_dir: Path) -> dict[int, int]:
    """Read valid integer supersede edges without treating history as mutable."""

    commands_dir = project_dir / "commands"
    if not commands_dir.is_dir():
        return {}
    relations: dict[int, int] = {}
    for path in sorted(commands_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        match = COMMAND_FILE_RE.fullmatch(path.name)
        if match is None:
            continue
        file_id = int(match.group(1))
        evidence = _read_command(path, file_id)
        if evidence.parse_error is not None or evidence.meta is None:
            continue
        relation = evidence.meta.get("supersedes_command_id")
        if (
            isinstance(relation, int)
            and not isinstance(relation, bool)
            and relation >= 1
        ):
            relations[file_id] = relation
    return relations


def inspect_command_repair(
    *,
    bridge_root: Path,
    project_id: str,
    command_id: int,
) -> CommandRepairInspection:
    """Inspect one target without Git publication or any other write."""

    root = Path(bridge_root).resolve()
    safe_project = _safe_project_id(project_id)
    target_id = _positive_int(command_id, "command-id")
    state = _load_state(root, safe_project)
    target_path = _command_path(root, safe_project, target_id)
    target = _read_command(target_path, target_id)
    report_exists = _report_path(root, safe_project, target_id).exists()
    assessment = protocol_core.assess_unclaimed_command_repair(
        state=state,
        target_command_id=target_id,
        target_meta=target.meta,
        target_parse_error=target.parse_error,
        target_filename_command_id=target.filename_command_id,
        target_file_exists=target.path.is_file(),
        target_report_exists=report_exists,
    )
    return CommandRepairInspection(
        bridge_root=root,
        project_id=safe_project,
        target_command_id=target_id,
        state=state,
        target=target,
        target_report_exists=report_exists,
        assessment=assessment,
    )


def _format_errors(assessment: protocol_core.CommandRepairAssessment) -> str:
    if not assessment.errors:
        return "no repair assessment errors were recorded"
    return "; ".join(assessment.errors)


def _same_state_identity(
    current: dict[str, Any],
    *,
    generation: int,
    latest_command: int,
    latest_report: int,
    status: str,
) -> bool:
    return (
        current.get("status") == status
        and current.get("generation") == generation
        and current.get("latest_command") == latest_command
        and current.get("latest_report") == latest_report
        and current.get("active_run") is None
    )


def repair_unclaimed_command(
    *,
    bridge_root: Path,
    project_id: str,
    command_id: int,
    replacement_command: str | Path,
) -> dict[str, Any]:
    """Publish one explicit append-only replacement through the shared CAS store."""

    root = Path(bridge_root).resolve()
    safe_project = _safe_project_id(project_id)
    target_id = _positive_int(command_id, "command-id")
    replacement_path, replacement_raw, replacement_text, replacement_meta, replacement_id = (
        _read_replacement(replacement_command)
    )

    # The Store owns synchronization and bounded Git CAS publication.  The
    # initial sync makes the read-only evidence below refer to the newest
    # remote branch before any repair decision is made.
    try:
        git_store.sync_to_remote(root)
    except (bridge_common.WorkerError, OSError) as exc:
        raise CommandRepairError("unable to synchronize the Bridge checkout safely") from exc

    initial = inspect_command_repair(
        bridge_root=root,
        project_id=safe_project,
        command_id=target_id,
    )
    existing_ids = _existing_command_ids(_project_dir(root, safe_project))
    assessment = protocol_core.assess_unclaimed_command_repair(
        state=initial.state,
        target_command_id=target_id,
        target_meta=initial.target.meta,
            target_parse_error=initial.target.parse_error,
            target_filename_command_id=initial.target.filename_command_id,
            target_file_exists=initial.target.path.is_file(),
            target_report_exists=initial.target_report_exists,
        replacement_meta=replacement_meta,
        replacement_filename_command_id=replacement_id,
        existing_command_ids=existing_ids,
    )
    if not assessment.eligible:
        raise CommandRepairError(
            "unclaimed command repair is not eligible: " + _format_errors(assessment)
        )
    if initial.target.sha256 is None or initial.target.text is None:
        raise CommandRepairError("canonical target bytes cannot be bound safely")

    state_generation = initial.state.get("generation")
    latest_report = initial.state.get("latest_report")
    if (
        isinstance(state_generation, bool)
        or not isinstance(state_generation, int)
        or state_generation < 0
        or isinstance(latest_report, bool)
        or not isinstance(latest_report, int)
        or latest_report < 0
    ):
        raise CommandRepairError("canonical generation/report identity is not valid")

    state_path = _state_path(root, safe_project)
    target_path = _command_path(root, safe_project, target_id)
    canonical_replacement_path = _command_path(root, safe_project, replacement_id)
    replacement_sha256 = _sha256(replacement_raw)
    target_sha256 = initial.target.sha256
    replacement_text_normalized = _normalize_newlines(replacement_text)
    generation_after = protocol_core.publication_generation(state_generation)
    source_path = replacement_path
    already_applied_seen = False

    def source_is_unchanged() -> bool:
        try:
            return _sha256(source_path.read_bytes()) == replacement_sha256
        except (OSError, IsADirectoryError):
            return False

    def current_target() -> CommandFileEvidence:
        return _read_command(target_path, target_id)

    def current_assessment(current: dict[str, Any]) -> protocol_core.CommandRepairAssessment:
        target = current_target()
        return protocol_core.assess_unclaimed_command_repair(
            state=current,
            target_command_id=target_id,
            target_meta=target.meta,
            target_parse_error=target.parse_error,
            target_filename_command_id=target.filename_command_id,
            target_file_exists=target.path.is_file(),
            target_report_exists=_report_path(root, safe_project, target_id).exists(),
            replacement_meta=replacement_meta,
            replacement_filename_command_id=replacement_id,
            existing_command_ids=_existing_command_ids(_project_dir(root, safe_project)),
        )

    def already_applied(current: dict[str, Any]) -> bool:
        nonlocal already_applied_seen
        if not _same_state_identity(
            current,
            generation=generation_after,
            latest_command=replacement_id,
            latest_report=latest_report,
            status="COMMAND_READY",
        ):
            return False
        old = current_target()
        if old.sha256 != target_sha256:
            return False
        new = _read_command(canonical_replacement_path, replacement_id)
        if new.meta is None or new.parse_error is not None:
            return False
        if new.text is None:
            return False
        if _normalize_newlines(new.text) != replacement_text_normalized:
            return False
        if new.meta != replacement_meta:
            return False
        if _report_path(root, safe_project, replacement_id).exists():
            return False
        already_applied_seen = True
        return True

    def expected(current: dict[str, Any]) -> bool:
        if not source_is_unchanged():
            return False
        old = current_target()
        if old.sha256 != target_sha256:
            return False
        decision = current_assessment(current)
        return decision.eligible

    def payload_builder(current: dict[str, Any]) -> dict[Path, str]:
        if not source_is_unchanged():
            raise CommandRepairConflict(
                "replacement input changed before canonical publication"
            )
        old = current_target()
        if old.sha256 != target_sha256:
            raise CommandRepairConflict(
                "canonical target command changed before publication"
            )
        decision = current_assessment(current)
        if not decision.eligible:
            raise CommandRepairConflict(
                "canonical repair evidence changed before publication: "
                + _format_errors(decision)
            )
        if canonical_replacement_path.exists():
            raise CommandRepairConflict(
                f"refusing to overwrite existing command file: {canonical_replacement_path.name}"
            )

        updated = dict(current)
        updated["status"] = "COMMAND_READY"
        updated["generation"] = protocol_core.publication_generation(
            int(current["generation"])
        )
        updated["latest_command"] = replacement_id
        updated["active_run"] = None
        updated["updated_at"] = bridge_common.now_iso()
        return {
            canonical_replacement_path: replacement_text,
            state_path: bridge_common.json_text(updated),
        }

    try:
        final_state = git_store.publish_cas(
            bridge_root=root,
            state_path=state_path,
            expected=expected,
            already_applied=already_applied,
            payload_builder=payload_builder,
            message=(
                f"bridge: repair unclaimed {safe_project} command "
                f"{target_id:03d} with {replacement_id:03d}"
            ),
        )
    except CommandRepairConflict:
        raise
    except bridge_common.CASConflict as exc:
        raise CommandRepairConflict(
            "canonical state changed during repair; no stale publication was applied"
        ) from exc
    except bridge_common.WorkerError as exc:
        raise CommandRepairError("repair CAS publication failed safely") from exc

    final_old = _read_command(target_path, target_id)
    final_new = _read_command(canonical_replacement_path, replacement_id)
    if final_old.sha256 != target_sha256:
        raise CommandRepairError("immutable target command bytes changed unexpectedly")
    if final_new.meta != replacement_meta or final_new.parse_error is not None:
        raise CommandRepairError("published replacement command failed post-publication validation")
    if not _same_state_identity(
        final_state,
        generation=generation_after,
        latest_command=replacement_id,
        latest_report=latest_report,
        status="COMMAND_READY",
    ):
        raise CommandRepairError("repair returned an unexpected canonical state")
    if _report_path(root, safe_project, target_id).exists() or _report_path(
        root, safe_project, replacement_id
    ).exists():
        raise CommandRepairError("repair unexpectedly has a report artifact")

    return {
        "event": COMMAND_REPAIR_EVENT,
        "result": "ALREADY_APPLIED" if already_applied_seen else "REPAIRED",
        "project": safe_project,
        "target_command_id": target_id,
        "replacement_command_id": replacement_id,
        "target_command_sha256": target_sha256,
        "replacement_command_sha256": final_new.sha256,
        "status": final_state.get("status"),
        "generation_before": state_generation,
        "generation_after": final_state.get("generation"),
        "latest_command": final_state.get("latest_command"),
        "latest_report": final_state.get("latest_report"),
        "active_run_present": final_state.get("active_run") is not None,
        "report_created": False,
        "recovery_created": False,
        "executor_invoked": False,
        "supersedes_command_id": replacement_meta.get("supersedes_command_id"),
    }


def adopt_staged_repair(
    *,
    bridge_root: Path,
    project_id: str,
    command_id: int,
    replacement_command_id: int,
) -> dict[str, Any]:
    """Adopt one pre-written immutable replacement through the shared CAS.

    Unlike :func:`repair_unclaimed_command`, this operation requires the
    replacement command to already exist at its canonical command path.  The
    exact state snapshot, all command bytes, and especially both 009/010
    command identities are bound before publication and checked again by the
    CAS predicates.  The only payload written is the advanced ``state.json``.
    """

    root = Path(bridge_root).resolve()
    safe_project = _safe_project_id(project_id)
    target_id = _positive_int(command_id, "command-id")
    replacement_id = _positive_int(
        replacement_command_id,
        "replacement-command-id",
    )
    if target_id == replacement_id:
        raise CommandRepairError(
            "staged replacement command id must differ from the target command id"
        )

    try:
        git_store.sync_to_remote(root)
    except (bridge_common.WorkerError, OSError) as exc:
        raise CommandRepairError(
            "unable to synchronize the Bridge checkout safely"
        ) from exc

    state_path = _state_path(root, safe_project)
    target_path = _command_path(root, safe_project, target_id)
    replacement_path = _command_path(root, safe_project, replacement_id)

    try:
        initial_state_raw = state_path.read_bytes()
    except (OSError, IsADirectoryError) as exc:
        raise CommandRepairError("canonical state.json cannot be read safely") from exc

    initial = inspect_command_repair(
        bridge_root=root,
        project_id=safe_project,
        command_id=target_id,
    )

    try:
        state_raw_after_inspection = state_path.read_bytes()
    except (OSError, IsADirectoryError) as exc:
        raise CommandRepairConflict(
            "canonical state disappeared during staged repair inspection"
        ) from exc
    if state_raw_after_inspection != initial_state_raw:
        raise CommandRepairConflict(
            "canonical state bytes changed during staged repair inspection"
        )
    initial_state_raw = state_raw_after_inspection

    target_raw: bytes
    try:
        target_raw = target_path.read_bytes()
    except (OSError, IsADirectoryError) as exc:
        raise CommandRepairError("canonical target command bytes cannot be bound safely") from exc
    if initial.target.sha256 is None or _sha256(target_raw) != initial.target.sha256:
        raise CommandRepairConflict("canonical target command bytes changed during inspection")

    replacement = _read_command(replacement_path, replacement_id)
    if replacement.parse_error is not None or replacement.meta is None:
        detail = replacement.parse_error or "metadata is unavailable"
        raise CommandRepairError(
            "staged replacement command is not a complete Protocol-v2 command: "
            + detail
        )
    try:
        replacement_raw = replacement_path.read_bytes()
    except (OSError, IsADirectoryError) as exc:
        raise CommandRepairError(
            "staged replacement command bytes cannot be bound safely"
        ) from exc
    if replacement.sha256 is None or _sha256(replacement_raw) != replacement.sha256:
        raise CommandRepairConflict(
            "staged replacement command bytes changed during inspection"
        )

    project_dir = _project_dir(root, safe_project)
    existing_ids = _existing_command_ids(project_dir)
    if replacement_id not in existing_ids:
        raise CommandRepairError(
            "staged replacement command must already exist in immutable command history"
        )
    command_snapshot = _command_bytes_snapshot(project_dir)
    state_snapshot = json.loads(json.dumps(initial.state, ensure_ascii=False))
    state_snapshot_sha256 = _sha256(initial_state_raw)
    target_sha256 = initial.target.sha256
    replacement_sha256 = replacement.sha256

    initial_assessment = protocol_core.assess_staged_unclaimed_command_repair(
        state=initial.state,
        target_command_id=target_id,
        target_meta=initial.target.meta,
        target_parse_error=initial.target.parse_error,
        target_filename_command_id=initial.target.filename_command_id,
        target_file_exists=initial.target.path.is_file(),
        target_report_exists=initial.target_report_exists,
        replacement_report_exists=_report_path(
            root,
            safe_project,
            replacement_id,
        ).exists(),
        replacement_meta=replacement.meta,
        replacement_filename_command_id=replacement_id,
        existing_command_ids=existing_ids,
        existing_supersedes=_existing_supersede_relations(project_dir),
    )
    if not initial_assessment.eligible:
        raise CommandRepairError(
            "staged unclaimed command repair is not eligible: "
            + _format_errors(initial_assessment)
        )

    state_generation = initial.state.get("generation")
    latest_report = initial.state.get("latest_report")
    if (
        isinstance(state_generation, bool)
        or not isinstance(state_generation, int)
        or state_generation < 0
        or isinstance(latest_report, bool)
        or not isinstance(latest_report, int)
        or latest_report < 0
    ):
        raise CommandRepairError("canonical generation/report identity is not valid")
    generation_after = protocol_core.publication_generation(state_generation)

    def state_is_unchanged() -> bool:
        try:
            raw = state_path.read_bytes()
        except (OSError, IsADirectoryError):
            return False
        return raw == initial_state_raw and _sha256(raw) == state_snapshot_sha256

    def command_history_is_unchanged() -> bool:
        try:
            current = _command_bytes_snapshot(project_dir)
        except CommandRepairError:
            return False
        return current == command_snapshot

    def exact_bytes_unchanged(path: Path, expected_raw: bytes, expected_sha256: str) -> bool:
        try:
            raw = path.read_bytes()
        except (OSError, IsADirectoryError):
            return False
        return raw == expected_raw and _sha256(raw) == expected_sha256

    def current_assessment(
        current: dict[str, Any],
    ) -> protocol_core.CommandRepairAssessment:
        target = _read_command(target_path, target_id)
        staged = _read_command(replacement_path, replacement_id)
        current_ids = _existing_command_ids(project_dir)
        return protocol_core.assess_staged_unclaimed_command_repair(
            state=current,
            target_command_id=target_id,
            target_meta=target.meta,
            target_parse_error=target.parse_error,
            target_filename_command_id=target.filename_command_id,
            target_file_exists=target.path.is_file(),
            target_report_exists=_report_path(root, safe_project, target_id).exists(),
            replacement_report_exists=_report_path(
                root,
                safe_project,
                replacement_id,
            ).exists(),
            replacement_meta=staged.meta,
            replacement_filename_command_id=replacement_id,
            existing_command_ids=current_ids,
            existing_supersedes=_existing_supersede_relations(project_dir),
        )

    def expected(current: dict[str, Any]) -> bool:
        if not state_is_unchanged() or current != state_snapshot:
            return False
        if not command_history_is_unchanged():
            return False
        if not exact_bytes_unchanged(target_path, target_raw, target_sha256):
            return False
        if not exact_bytes_unchanged(
            replacement_path,
            replacement_raw,
            replacement_sha256,
        ):
            return False
        return current_assessment(current).eligible

    def payload_builder(current: dict[str, Any]) -> dict[Path, str]:
        if not state_is_unchanged() or current != state_snapshot:
            raise CommandRepairConflict(
                "canonical state snapshot or SHA changed before staged publication"
            )
        if not command_history_is_unchanged():
            raise CommandRepairConflict(
                "an existing command changed before staged publication"
            )
        if not exact_bytes_unchanged(target_path, target_raw, target_sha256):
            raise CommandRepairConflict(
                "canonical target command changed before staged publication"
            )
        if not exact_bytes_unchanged(
            replacement_path,
            replacement_raw,
            replacement_sha256,
        ):
            raise CommandRepairConflict(
                "staged replacement command changed before canonical publication"
            )
        decision = current_assessment(current)
        if not decision.eligible:
            raise CommandRepairConflict(
                "staged repair evidence changed before publication: "
                + _format_errors(decision)
            )

        updated = dict(current)
        updated["status"] = "COMMAND_READY"
        updated["generation"] = protocol_core.publication_generation(
            int(current["generation"])
        )
        updated["latest_command"] = replacement_id
        updated["latest_report"] = int(current["latest_report"])
        updated["active_run"] = None
        updated["updated_at"] = bridge_common.now_iso()
        # Deliberately publish only state.json.  The staged replacement is
        # immutable input, never a payload to write or overwrite.
        return {state_path: bridge_common.json_text(updated)}

    try:
        final_state = git_store.publish_cas(
            bridge_root=root,
            state_path=state_path,
            expected=expected,
            already_applied=lambda _current: False,
            payload_builder=payload_builder,
            message=(
                f"bridge: adopt staged repair {safe_project} command "
                f"{target_id:03d} with {replacement_id:03d}"
            ),
        )
    except CommandRepairConflict:
        raise
    except bridge_common.CASConflict as exc:
        raise CommandRepairConflict(
            "canonical state or staged command evidence changed during repair; "
            "no stale publication was applied"
        ) from exc
    except bridge_common.WorkerError as exc:
        raise CommandRepairError("staged repair CAS publication failed safely") from exc

    final_target_raw = target_path.read_bytes()
    final_replacement_raw = replacement_path.read_bytes()
    if final_target_raw != target_raw or _sha256(final_target_raw) != target_sha256:
        raise CommandRepairError("immutable target command bytes changed unexpectedly")
    if (
        final_replacement_raw != replacement_raw
        or _sha256(final_replacement_raw) != replacement_sha256
    ):
        raise CommandRepairError(
            "immutable staged replacement command bytes changed unexpectedly"
        )
    if not command_history_is_unchanged():
        raise CommandRepairError("an existing command changed unexpectedly")
    if not _same_state_identity(
        final_state,
        generation=generation_after,
        latest_command=replacement_id,
        latest_report=latest_report,
        status="COMMAND_READY",
    ):
        raise CommandRepairError("staged repair returned an unexpected canonical state")
    if _report_path(root, safe_project, target_id).exists() or _report_path(
        root, safe_project, replacement_id
    ).exists():
        raise CommandRepairError("staged repair unexpectedly has a report artifact")

    return {
        "event": STAGED_REPAIR_EVENT,
        "result": "ADOPTED",
        "project": safe_project,
        "target_command_id": target_id,
        "replacement_command_id": replacement_id,
        "target_command_sha256": target_sha256,
        "replacement_command_sha256": replacement_sha256,
        "state_sha256_before": state_snapshot_sha256,
        "status": final_state.get("status"),
        "generation_before": state_generation,
        "generation_after": final_state.get("generation"),
        "latest_command": final_state.get("latest_command"),
        "latest_report": final_state.get("latest_report"),
        "active_run_present": final_state.get("active_run") is not None,
        "run_created": False,
        "lease_created": False,
        "report_created": False,
        "recovery_created": False,
        "executor_invoked": False,
        "command_bytes_unchanged": True,
        "supersedes_command_id": replacement.meta.get("supersedes_command_id"),
    }


def _inspection_json(evidence: CommandRepairInspection) -> dict[str, Any]:
    state = evidence.state
    assessment = evidence.assessment
    generation = state.get("generation")
    expected_generation = (
        generation + 1
        if isinstance(generation, int) and not isinstance(generation, bool)
        else None
    )
    return {
        "event": COMMAND_REPAIR_EVENT,
        "operation": "inspect",
        "project": evidence.project_id,
        "canonical": {
            "status": state.get("status"),
            "generation": state.get("generation"),
            "latest_command": state.get("latest_command"),
            "latest_report": state.get("latest_report"),
            "active_run_present": state.get("active_run") is not None,
        },
        "target": {
            "command_id": evidence.target_command_id,
            "filename_command_id": evidence.target.filename_command_id,
            "sha256": evidence.target.sha256,
            "report_present": evidence.target_report_exists,
        },
        "deterministic_validation_errors": list(assessment.target_errors),
        "state_guard_errors": list(assessment.state_errors),
        "all_validation_errors": list(assessment.errors),
        "repair_eligible": assessment.eligible,
        "replacement_requirements": {
            "based_on_report": state.get("latest_report"),
            "expected_generation": expected_generation,
            "supersedes_command_id": evidence.target_command_id,
            "command_id_strictly_greater_than_existing_history": True,
        },
    }


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bridge-root",
        default=None,
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--command-id", type=int, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicit Protocol-v2 repair of one deterministic invalid, "
            "unclaimed command; never executes or reruns Codex."
        )
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="read and print command repair eligibility",
    )
    _add_common_arguments(inspect_parser)
    repair_parser = subparsers.add_parser(
        "repair",
        help="publish an explicit replacement command through Protocol-v2 CAS",
    )
    _add_common_arguments(repair_parser)
    repair_parser.add_argument("--replacement-command", required=True)
    staged_parser = subparsers.add_parser(
        "adopt-staged",
        aliases=["adopt-staged-repair", "adopt_staged_repair"],
        help=(
            "adopt one already-existing immutable replacement command through "
            "Protocol-v2 CAS"
        ),
    )
    _add_common_arguments(staged_parser)
    staged_parser.add_argument("--replacement-command-id", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.bridge_root = state_roots.resolve_state_root(args.bridge_root, for_write=args.operation != "inspect")
    try:
        if args.operation == "inspect":
            evidence = inspect_command_repair(
                bridge_root=Path(args.bridge_root),
                project_id=args.project,
                command_id=args.command_id,
            )
            print("COMMAND_REPAIR_INSPECT_JSON:")
            print(json.dumps(_inspection_json(evidence), ensure_ascii=False, indent=2))
            return 0

        if args.operation in {
            "adopt-staged",
            "adopt-staged-repair",
            "adopt_staged_repair",
        }:
            result = adopt_staged_repair(
                bridge_root=Path(args.bridge_root),
                project_id=args.project,
                command_id=args.command_id,
                replacement_command_id=args.replacement_command_id,
            )
        else:
            result = repair_unclaimed_command(
                bridge_root=Path(args.bridge_root),
                project_id=args.project,
                command_id=args.command_id,
                replacement_command=args.replacement_command,
            )
        print("COMMAND_REPAIR_JSON:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CommandRepairError, protocol_core.ProtocolViolation, protocol_core.ProtocolConflict) as exc:
        print(f"COMMAND_REPAIR_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
