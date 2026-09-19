#!/usr/bin/env python3
"""Repository-level Protocol v2 conformance checks.

Standard-library only by design so CI and a local Worker checkout can run it
without adding package-management requirements. JSON Schema files are the
published contracts; this script implements the current high-value checks and
cross-file invariants that matter for the repository itself.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ENGINE_ROOT = Path(__file__).resolve().parents[2]
PROFILE = Path(__file__).resolve().parent
ROOT = ENGINE_ROOT / "tests/fixtures/synthetic-state"
PROJECTS = ROOT / "projects"
WORKER = ENGINE_ROOT / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

import protocol_core
import state_roots

COMMAND_META_RE = re.compile(
    r"^\s*<!--\s*bridge-command:\s*(\{.*?\})\s*-->\s*$",
    re.IGNORECASE,
)
COMMAND_FILE_RE = re.compile(r"^command-(\d+)\.md$", re.IGNORECASE)
REPORT_FILE_RE = re.compile(r"^report-(\d+)\.md$", re.IGNORECASE)
OWNER_FILE_RE = re.compile(r"^owner-action-(\d+)\.md$", re.IGNORECASE)
BULLET_META_RE = re.compile(r"^\s*-\s+([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")


def _label(path: Path) -> str:
    for root in (ROOT, ENGINE_ROOT):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.name


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def schema_enum(filename: str, property_name: str) -> set[str]:
    schema = load_json(PROFILE / filename)
    values = schema["properties"][property_name]["enum"]
    return {str(value) for value in values}


def parse_command_meta(path: Path) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = COMMAND_META_RE.match(line)
        if not match:
            continue
        value = json.loads(match.group(1))
        if not isinstance(value, dict):
            raise ValueError("bridge-command metadata is not a JSON object")
        matches.append(value)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"expected exactly one bridge-command metadata line, found {len(matches)}")
    return matches[0]


def parse_bullet_metadata(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        # A recovery wrapper may preserve a complete legacy pending report in
        # its body.  Only the first normalized header is canonical; metadata
        # inside the preserved evidence must not override it.
        if line.strip().startswith("## "):
            break
        match = BULLET_META_RE.match(line)
        if match:
            result[match.group(1)] = match.group(2).strip().strip("`")
    return result


def as_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def validate_command_meta(
    path: Path,
    meta: dict[str, Any],
    *,
    sources: set[str],
    kinds: set[str],
) -> list[str]:
    filename_match = COMMAND_FILE_RE.match(path.name)
    filename_id = int(filename_match.group(1)) if filename_match else None
    # Keep the repository-level checker on the same strict contract validator
    # as Worker/operator code.  The schema/core drift check below proves that
    # the supplied source/kind sets are the same v2 sets used here.
    del sources, kinds
    return list(
        protocol_core.command_contract_errors(
            meta,
            filename_command_id=filename_id,
        )
    )


class CommandHistoryRecord:
    """One v2 command record, including errors that relation cannot hide."""

    def __init__(
        self,
        path: Path,
        file_id: int | None,
        meta: dict[str, Any] | None,
        contract_errors: list[str],
        relation_errors: list[str],
    ) -> None:
        self.path = path
        self.file_id = file_id
        self.meta = meta
        self.contract_errors = contract_errors
        self.relation_errors = relation_errors


def _command_marker_present(text: str) -> bool:
    # Treat any bridge-command marker as Protocol-v2 evidence, even when the
    # marker itself is malformed.  Otherwise a bad historical v2 command
    # could be relabeled as legacy merely by breaking its JSON envelope.
    return any(
        re.match(r"^\s*<!--\s*bridge-command:", line, re.IGNORECASE)
        for line in text.splitlines()
    )


def _load_command_history_records(commands_dir: Path) -> list[CommandHistoryRecord]:
    records: list[CommandHistoryRecord] = []
    for path in sorted(commands_dir.glob("command-*.md")):
        filename_match = COMMAND_FILE_RE.fullmatch(path.name)
        file_id = int(filename_match.group(1)) if filename_match else None
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            records.append(
                CommandHistoryRecord(
                    path=path,
                    file_id=file_id,
                    meta=None,
                    contract_errors=[f"command file cannot be read: {exc}"],
                    relation_errors=[],
                )
            )
            continue

        # Protocol-v1 command bodies remain valid legacy history when they do
        # not carry a v2 marker.  A marker that is malformed is v2 evidence and
        # must fail unless a later valid repair successor covers its contract
        # error.
        if not _command_marker_present(text):
            continue
        try:
            meta = parse_command_meta(path)
        except Exception as exc:
            records.append(
                CommandHistoryRecord(
                    path=path,
                    file_id=file_id,
                    meta=None,
                    contract_errors=[f"invalid metadata: {exc}"],
                    relation_errors=[],
                )
            )
            continue
        if meta is None:
            records.append(
                CommandHistoryRecord(
                    path=path,
                    file_id=file_id,
                    meta=None,
                    contract_errors=[
                        "canonical Protocol-v2 command lacks bridge-command metadata"
                    ],
                    relation_errors=[],
                )
            )
            continue

        contract_errors = validate_command_meta(
            path,
            meta,
            sources=set(protocol_core.COMMAND_SOURCES),
            kinds=set(protocol_core.COMMAND_KINDS),
        )
        try:
            protocol_core.parse_command_metadata(text)
        except protocol_core.ProtocolViolation as exc:
            message = f"strict command header validation failed: {exc}"
            if message not in contract_errors:
                contract_errors.append(message)
        records.append(
            CommandHistoryRecord(
                path=path,
                file_id=file_id,
                meta=meta,
                contract_errors=contract_errors,
                relation_errors=[],
            )
        )
    return records


def _cycle_nodes(edges: dict[int, int]) -> set[int]:
    nodes_in_cycles: set[int] = set()
    for start in edges:
        order: list[int] = []
        seen: dict[int, int] = {}
        current: int | None = start
        while current in edges and current not in seen:
            seen[current] = len(order)
            order.append(current)
            current = edges[current]
        if current in seen:
            nodes_in_cycles.update(order[seen[current] :])
    return nodes_in_cycles


def _append_relation_error(
    records_by_file_id: dict[int, CommandHistoryRecord],
    command_id: int,
    message: str,
) -> None:
    record = records_by_file_id.get(command_id)
    if record is not None and message not in record.relation_errors:
        record.relation_errors.append(message)


def validate_state(
    project_dir: Path,
    *,
    allowed_states: set[str],
    command_sources: set[str],
    command_kinds: set[str],
) -> list[str]:
    errors: list[str] = []
    state_path = project_dir / "state.json"
    if not state_path.exists():
        return errors
    try:
        state = load_json(state_path)
    except Exception as exc:
        return [f"{_label(state_path)}: invalid JSON: {exc}"]

    prefix = str(_label(state_path))
    if state.get("protocol_version") != 2:
        errors.append(f"{prefix}: protocol_version must be 2")
    if state.get("project_id") != project_dir.name:
        errors.append(f"{prefix}: project_id must match directory name {project_dir.name!r}")
    status = str(state.get("status", ""))
    if status not in allowed_states:
        errors.append(f"{prefix}: unsupported top-level status {status!r}")
    if status == "OWNER_IN_PROGRESS":
        errors.append(f"{prefix}: OWNER_IN_PROGRESS is owner-action evidence, never a top-level state")

    numbers: dict[str, int] = {}
    for field in ("generation", "latest_command", "latest_report", "last_reviewed_report"):
        try:
            numbers[field] = as_nonnegative_int(state.get(field), field)
        except ValueError as exc:
            errors.append(f"{prefix}: {exc}")
    if len(numbers) == 4:
        if numbers["last_reviewed_report"] > numbers["latest_report"]:
            errors.append(f"{prefix}: last_reviewed_report exceeds latest_report")
        if numbers["latest_report"] > numbers["latest_command"]:
            errors.append(f"{prefix}: latest_report exceeds latest_command")

    active = state.get("active_run")
    if status == "CODEX_RUNNING":
        if not isinstance(active, dict):
            errors.append(f"{prefix}: CODEX_RUNNING requires active_run")
        else:
            if active.get("command_id") != state.get("latest_command"):
                errors.append(f"{prefix}: active_run.command_id must equal latest_command")
            if active.get("claimed_generation") != state.get("generation"):
                errors.append(f"{prefix}: active_run.claimed_generation must equal generation")
            if not str(active.get("run_id", "")):
                errors.append(f"{prefix}: active_run.run_id is required")
    elif active is not None:
        errors.append(f"{prefix}: active_run must be null outside CODEX_RUNNING")

    if "human_required" in state:
        value = state["human_required"]
        if not isinstance(value, bool):
            errors.append(f"{prefix}: human_required compatibility field must be boolean")
        elif value != (status == "HUMAN_REQUIRED"):
            errors.append(f"{prefix}: human_required contradicts canonical status")
    if status == "DONE" and state.get("finalized") is not True:
        errors.append(f"{prefix}: DONE requires finalized=true when compatibility field is present")

    latest_command = numbers.get("latest_command", 0)
    latest_report = numbers.get("latest_report", 0)
    command_path = project_dir / "commands" / f"command-{latest_command:03d}.md"
    report_path = project_dir / "reports" / f"report-{latest_report:03d}.md"
    if latest_command and not command_path.exists():
        errors.append(f"{prefix}: canonical latest_command file is missing: {command_path.name}")
    if latest_report and not report_path.exists():
        errors.append(f"{prefix}: canonical latest_report file is missing: {report_path.name}")

    raw_withdrawals = state.get("withdrawn_commands", [])
    if raw_withdrawals is not None:
        if not isinstance(raw_withdrawals, list):
            errors.append(f"{prefix}: withdrawn_commands must be an array")
        else:
            seen_withdrawal_targets: set[int] = set()
            for index, record in enumerate(raw_withdrawals):
                try:
                    protocol_core.validate_withdrawal_record(record)
                except protocol_core.ProtocolViolation as exc:
                    errors.append(f"{prefix}: withdrawn_commands[{index}]: {exc}")
                    continue
                target_id = int(record["command_id"])
                replacement_id = int(record["replacement_command_id"])
                if target_id in seen_withdrawal_targets:
                    errors.append(
                        f"{prefix}: withdrawn_commands duplicates target command-{target_id:03d}"
                    )
                seen_withdrawal_targets.add(target_id)
                target_file = project_dir / "commands" / f"command-{target_id:03d}.md"
                replacement_file = project_dir / "commands" / f"command-{replacement_id:03d}.md"
                if not target_file.exists():
                    errors.append(
                        f"{prefix}: withdrawal target file is missing: {target_file.name}"
                    )
                if not replacement_file.exists():
                    errors.append(
                        f"{prefix}: withdrawal replacement file is missing: {replacement_file.name}"
                    )
                if replacement_id > latest_command:
                    errors.append(
                        f"{prefix}: withdrawal replacement exceeds latest_command"
                    )
                if replacement_id == latest_command and replacement_file.exists():
                    try:
                        replacement_meta = parse_command_meta(replacement_file)
                    except Exception as exc:
                        errors.append(
                            f"{_label(replacement_file)}: invalid withdrawal replacement metadata: {exc}"
                        )
                    else:
                        if replacement_meta is None:
                            errors.append(
                                f"{_label(replacement_file)}: withdrawal replacement lacks command metadata"
                            )
                        elif replacement_meta.get("withdraws_command_id") != target_id:
                            errors.append(
                                f"{_label(replacement_file)}: withdraws_command_id does not match withdrawal record"
                            )

    if command_path.exists():
        try:
            meta = parse_command_meta(command_path)
        except Exception as exc:
            errors.append(f"{_label(command_path)}: invalid metadata: {exc}")
            meta = None
        if meta is None:
            errors.append(f"{_label(command_path)}: canonical Protocol-v2 command lacks bridge-command metadata")
        else:
            for error in validate_command_meta(command_path, meta, sources=command_sources, kinds=command_kinds):
                errors.append(f"{_label(command_path)}: {error}")
            try:
                protocol_core.parse_command_metadata(
                    command_path.read_text(encoding="utf-8")
                )
            except protocol_core.ProtocolViolation as exc:
                errors.append(
                    f"{_label(command_path)}: "
                    f"strict command header validation failed: {exc}"
                )
            if status in {"COMMAND_READY", "FINALIZING"}:
                if meta.get("expected_generation") != state.get("generation"):
                    errors.append(f"{_label(command_path)}: pending command expected_generation must equal canonical generation")
                if meta.get("based_on_report") != state.get("latest_report"):
                    errors.append(f"{_label(command_path)}: pending command based_on_report must equal latest_report")

    if report_path.exists():
        report_meta = parse_bullet_metadata(report_path)
        command_value = report_meta.get("command_id")
        if command_value is not None:
            try:
                if int(command_value) != latest_report:
                    errors.append(f"{_label(report_path)}: command_id does not match report id")
            except ValueError:
                errors.append(f"{_label(report_path)}: command_id is not an integer")

    return errors


def validate_history(
    project_dir: Path,
    *,
    command_sources: set[str],
    command_kinds: set[str],
    owner_statuses: set[str],
    verification_statuses: set[str],
    canonical_latest_command: int | None = None,
    warnings: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    commands_dir = project_dir / "commands"
    if commands_dir.exists():
        records = _load_command_history_records(commands_dir)
        records_by_file_id = {
            record.file_id: record
            for record in records
            if record.file_id is not None
        }
        # A supersede target's existence is an immutable-history/filesystem fact,
        # not a parse-success fact. A deterministic-invalid historical command may
        # lack the canonical v2 marker (the exact repair case exercised by the
        # tests) while its command-NNN.md file still exists.
        existing_file_ids: set[int] = set()
        for path in commands_dir.glob("command-*.md"):
            filename_match = COMMAND_FILE_RE.fullmatch(path.name)
            if filename_match is not None:
                existing_file_ids.add(int(filename_match.group(1)))

        relation_targets: dict[int, list[int]] = {}
        relation_edges: dict[int, int] = {}
        withdrawal_targets: dict[int, list[int]] = {}

        for record in records:
            if record.meta is None or record.file_id is None:
                continue
            raw_superseded = record.meta.get("supersedes_command_id")
            if (
                isinstance(raw_superseded, int)
                and not isinstance(raw_superseded, bool)
                and raw_superseded >= 1
            ):
                superseded_id = raw_superseded
                relation_targets.setdefault(superseded_id, []).append(record.file_id)
                relation_edges[record.file_id] = superseded_id
                if superseded_id == record.file_id:
                    record.relation_errors.append(
                        "supersedes_command_id must not self-reference"
                    )
                elif superseded_id > record.file_id:
                    record.relation_errors.append(
                        "supersedes_command_id must refer to an earlier command id"
                    )
                if superseded_id not in existing_file_ids:
                    record.relation_errors.append(
                        f"supersedes_command_id references missing command-{superseded_id:03d}"
                    )
                if (
                    canonical_latest_command is not None
                    and record.file_id > canonical_latest_command
                ):
                    record.relation_errors.append(
                        "superseder command is beyond canonical latest_command"
                    )

            raw_withdrawn = record.meta.get("withdraws_command_id")
            if (
                isinstance(raw_withdrawn, int)
                and not isinstance(raw_withdrawn, bool)
                and raw_withdrawn >= 1
            ):
                withdrawn_id = raw_withdrawn
                withdrawal_targets.setdefault(withdrawn_id, []).append(record.file_id)
                if withdrawn_id == record.file_id:
                    record.relation_errors.append(
                        "withdraws_command_id must not self-reference"
                    )
                elif withdrawn_id > record.file_id:
                    record.relation_errors.append(
                        "withdraws_command_id must refer to an earlier command id"
                    )
                if withdrawn_id not in existing_file_ids:
                    record.relation_errors.append(
                        f"withdraws_command_id references missing command-{withdrawn_id:03d}"
                    )
                if (
                    canonical_latest_command is not None
                    and record.file_id > canonical_latest_command
                ):
                    record.relation_errors.append(
                        "withdrawal command is beyond canonical latest_command"
                    )

        for superseded_id, superseders in relation_targets.items():
            unique_superseders = sorted(set(superseders))
            if len(unique_superseders) > 1:
                for superseder_id in unique_superseders:
                    _append_relation_error(
                        records_by_file_id,
                        superseder_id,
                        (
                            f"command-{superseded_id:03d} has multiple conflicting "
                            "superseders"
                        ),
                    )

        for withdrawn_id, withdrawers in withdrawal_targets.items():
            unique_withdrawers = sorted(set(withdrawers))
            if len(unique_withdrawers) > 1:
                for withdrawer_id in unique_withdrawers:
                    _append_relation_error(
                        records_by_file_id,
                        withdrawer_id,
                        (
                            f"command-{withdrawn_id:03d} has multiple withdrawal "
                            "records"
                        ),
                    )

        for command_id in _cycle_nodes(relation_edges):
            _append_relation_error(
                records_by_file_id,
                command_id,
                "supersede relation contains a cycle",
            )

        # A superseder may waive only the old command's deterministic contract
        # errors.  Graph/identity/canonical-history errors always remain fatal.
        for record in records:
            record_errors = record.contract_errors + record.relation_errors
            if not record_errors:
                continue
            if not record.contract_errors or record.relation_errors:
                for error in record_errors:
                    errors.append(f"{_label(record.path)}: {error}")
                continue
            if (
                record.file_id is None
                or canonical_latest_command is None
                or record.file_id == canonical_latest_command
            ):
                for error in record_errors:
                    errors.append(f"{_label(record.path)}: {error}")
                continue

            superseders = sorted(
                superseder_id
                for superseder_id in relation_targets.get(record.file_id, [])
                if superseder_id > record.file_id
            )
            valid_superseders = [
                superseder_id
                for superseder_id in superseders
                if (
                    canonical_latest_command is not None
                    and superseder_id <= canonical_latest_command
                    and not records_by_file_id[superseder_id].contract_errors
                    and not records_by_file_id[superseder_id].relation_errors
                )
            ]
            if len(valid_superseders) == 1 and len(superseders) == 1:
                superseder_id = valid_superseders[0]
                if warnings is not None:
                    warnings.append(
                        f"{project_dir.name}: command-{record.file_id:03d} "
                        f"repaired/superseded by command-{superseder_id:03d}; "
                        "invalid command retained as immutable history"
                    )
                continue
            for error in record_errors:
                errors.append(f"{_label(record.path)}: {error}")

    owner_dir = project_dir / "owner-actions"
    if owner_dir.exists():
        existing_ids = {path.stem for path in owner_dir.glob("owner-action-*.md")}
        for path in sorted(owner_dir.glob("owner-action-*.md")):
            match = OWNER_FILE_RE.match(path.name)
            if not match:
                continue
            meta = parse_bullet_metadata(path)
            owner_status = meta.get("owner_status")
            verification = meta.get("verification_status")
            if owner_status is not None and owner_status not in owner_statuses:
                errors.append(f"{_label(path)}: unsupported owner_status {owner_status!r}")
            if verification is not None and verification not in verification_statuses:
                errors.append(f"{_label(path)}: unsupported verification_status {verification!r}")
            if owner_status == "OWNER_IN_PROGRESS" and verification not in {None, "PENDING"}:
                errors.append(f"{_label(path)}: OWNER_IN_PROGRESS must remain verification_status=PENDING")
            for field in ("root_action_id", "relates_to"):
                value = meta.get(field)
                if value in {None, "", "null", "None"}:
                    continue
                if not re.fullmatch(r"owner-action-\d+", value):
                    errors.append(f"{_label(path)}: {field} has invalid id {value!r}")
                elif value not in existing_ids:
                    errors.append(f"{_label(path)}: {field} references missing {value}")
    return errors


def validate_transition_spec(allowed_states: set[str]) -> list[str]:
    errors: list[str] = []
    path = PROFILE / "transitions.json"
    try:
        spec = load_json(path)
    except Exception as exc:
        return [f"{_label(path)}: invalid JSON: {exc}"]
    spec_states = {str(value) for value in spec.get("allowed_states", [])}
    if spec_states != allowed_states:
        errors.append(f"{_label(path)}: allowed_states differs from state.schema.json")
    if "OWNER_IN_PROGRESS" in spec_states:
        errors.append(f"{_label(path)}: OWNER_IN_PROGRESS must not be a canonical state")
    for item in spec.get("canonical_transitions", []):
        if not isinstance(item, dict):
            errors.append(f"{_label(path)}: transition entry must be an object")
            continue
        sources = item.get("from", [])
        destination = item.get("to")
        for source in sources if isinstance(sources, list) else []:
            if source not in allowed_states:
                errors.append(f"{_label(path)}: transition {item.get('event')} has invalid source {source!r}")
        if destination not in allowed_states:
            errors.append(f"{_label(path)}: transition {item.get('event')} has invalid destination {destination!r}")
    return errors


def validate_runtime_core(
    *,
    allowed_states: set[str],
    command_sources: set[str],
    command_kinds: set[str],
) -> list[str]:
    """Ensure the shared Python runtime semantics do not drift from v2 files."""

    errors: list[str] = []
    transitions_path = PROFILE / "transitions.json"
    try:
        transitions = load_json(transitions_path)
    except Exception as exc:
        return [f"{_label(transitions_path)}: invalid JSON: {exc}"]

    if protocol_core.PROTOCOL_VERSION != transitions.get("wire_protocol_version"):
        errors.append(
            f"{_label(transitions_path)}: protocol_core.PROTOCOL_VERSION "
            "differs from wire_protocol_version"
        )
    if protocol_core.ENGINEERING_PROFILE != transitions.get("engineering_profile"):
        errors.append(
            f"{_label(transitions_path)}: protocol_core.ENGINEERING_PROFILE "
            "differs from engineering_profile"
        )
    if set(protocol_core.ALLOWED_STATES) != allowed_states:
        errors.append(
            f"{_label(transitions_path)}: protocol_core allowed states "
            "differ from state.schema.json"
        )
    if set(protocol_core.ALLOWED_STATES) != {
        str(value) for value in transitions.get("allowed_states", [])
    }:
        errors.append(
            f"{_label(transitions_path)}: protocol_core allowed states "
            "differ from transitions.json"
        )
    if set(protocol_core.TERMINAL_STATES) != {
        str(value) for value in transitions.get("terminal_states", [])
    }:
        errors.append(
            f"{_label(transitions_path)}: protocol_core terminal states "
            "differ from transitions.json"
        )
    if set(protocol_core.COMMAND_SOURCES) != command_sources:
        errors.append(
            f"{_label(transitions_path)}: protocol_core command sources "
            "differ from command.schema.json"
        )
    if set(protocol_core.COMMAND_KINDS) != command_kinds:
        errors.append(
            f"{_label(transitions_path)}: protocol_core command kinds "
            "differ from command.schema.json"
        )

    command_schema = load_json(PROFILE / "command.schema.json")
    schema_required = {
        str(value) for value in command_schema.get("required", [])
    }
    if set(protocol_core.COMMAND_REQUIRED_FIELDS) != schema_required:
        errors.append(
            f"{_label(transitions_path)}: protocol_core required command "
            "fields differ from command.schema.json"
        )
    schema_optional = set(command_schema.get("properties", {})) - schema_required
    if set(protocol_core.COMMAND_OPTIONAL_FIELDS) != schema_optional:
        errors.append(
            f"{_label(transitions_path)}: protocol_core optional command "
            "fields differ from command.schema.json"
        )
    schema_efforts = {
        str(value)
        for value in command_schema["properties"]["executor"]["properties"][
            "reasoning_effort"
        ]["enum"]
    }
    if set(protocol_core.SUPPORTED_REASONING_EFFORTS) != schema_efforts:
        errors.append(
            f"{_label(transitions_path)}: protocol_core reasoning effort "
            "allowlist differs from command.schema.json"
        )
    executor_properties = set(
        command_schema["properties"]["executor"]["properties"]
    )
    if executor_properties != set(protocol_core.EXECUTOR_FIELDS):
        errors.append(
            f"{_label(transitions_path)}: protocol_core executor fields "
            "differ from command.schema.json"
        )
    executor_required = set(command_schema["properties"]["executor"].get("required", []))
    if executor_required != set(protocol_core.EXECUTOR_REQUIRED_FIELDS):
        errors.append(
            f"{_label(transitions_path)}: protocol_core required executor "
            "fields differ from command.schema.json"
        )
    schema_service_tiers = {
        str(value)
        for value in command_schema["properties"]["executor"]["properties"][
            "service_tier"
        ]["enum"]
    }
    if set(protocol_core.SUPPORTED_SERVICE_TIERS) != schema_service_tiers:
        errors.append(
            f"{_label(transitions_path)}: protocol_core service tier "
            "allowlist differs from command.schema.json"
        )
    if "OWNER_IN_PROGRESS" in protocol_core.ALLOWED_STATES:
        errors.append(
            "protocol_core: OWNER_IN_PROGRESS must not be a canonical top-level state"
        )
    if protocol_core.PENDING_STATES != frozenset({"COMMAND_READY", "FINALIZING"}):
        errors.append(
            "protocol_core: pending states must remain COMMAND_READY and FINALIZING"
        )
    if protocol_core.EXECUTABLE_STATES != protocol_core.PENDING_STATES:
        errors.append("protocol_core: executable states must match pending states")
    return errors


def main(argv: list[str] | None = None) -> int:
    global ROOT, PROJECTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args(argv)
    ROOT = state_roots.resolve_state_root(args.state_root)
    PROJECTS = ROOT / "projects"
    allowed_states = schema_enum("state.schema.json", "status")
    command_sources = schema_enum("command.schema.json", "source")
    command_kinds = schema_enum("command.schema.json", "kind")
    owner_statuses = schema_enum("owner-action.schema.json", "owner_status")
    verification_statuses = schema_enum("owner-action.schema.json", "verification_status")

    errors = validate_transition_spec(allowed_states)
    errors.extend(
        validate_runtime_core(
            allowed_states=allowed_states,
            command_sources=command_sources,
            command_kinds=command_kinds,
        )
    )
    projects_checked = 0
    warnings: list[str] = []
    for project_dir in sorted(path for path in PROJECTS.iterdir() if path.is_dir()):
        if not (project_dir / "state.json").exists():
            continue
        projects_checked += 1
        errors.extend(
            validate_state(
                project_dir,
                allowed_states=allowed_states,
                command_sources=command_sources,
                command_kinds=command_kinds,
            )
        )
        canonical_latest_command: int | None = None
        try:
            candidate_state = load_json(project_dir / "state.json")
            raw_latest_command = candidate_state.get("latest_command")
            if (
                isinstance(raw_latest_command, int)
                and not isinstance(raw_latest_command, bool)
                and raw_latest_command >= 0
            ):
                canonical_latest_command = raw_latest_command
        except Exception:
            pass
        errors.extend(
            validate_history(
                project_dir,
                command_sources=command_sources,
                command_kinds=command_kinds,
                owner_statuses=owner_statuses,
                verification_statuses=verification_statuses,
                canonical_latest_command=canonical_latest_command,
                warnings=warnings,
            )
        )

    if errors:
        print(f"Protocol v2 conformance FAILED with {len(errors)} issue(s):")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Protocol v2 conformance PASS: {projects_checked} project state(s) checked")
    for warning in warnings:
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
