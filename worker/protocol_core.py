#!/usr/bin/env python3
"""Shared, side-effect-free Protocol v2 runtime semantics.

The files under ``protocol/v2`` are the machine-readable normative profile.
This module is the small Python implementation of the stable v2 semantics
that must be shared by the Worker and the manual fast lane.  It deliberately
does not know about Git, GitHub, Codex, local configuration, or email.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


PROTOCOL_VERSION = 2
ENGINEERING_PROFILE = "2.6"

ALLOWED_STATES = frozenset(
    {
        "COMMAND_READY",
        "CODEX_RUNNING",
        "REPORT_READY",
        "FINALIZING",
        "FINAL_REPORT_READY",
        "HUMAN_REQUIRED",
        "RECOVERY_REQUIRED",
        "DONE",
        "FAILED",
    }
)
TERMINAL_STATES = frozenset({"DONE", "FAILED"})
PENDING_STATES = frozenset({"COMMAND_READY", "FINALIZING"})
EXECUTABLE_STATES = PENDING_STATES

COMMAND_KINDS = frozenset({"EXECUTE", "FINALIZE"})
COMMAND_SOURCES = frozenset(
    {"scheduled_chatgpt", "manual_chatgpt", "user_direct", "finalizer"}
)
SUPPORTED_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
# The installed Codex CLI accepts the ``service_tier`` configuration key.  The
# Bridge command contract deliberately exposes only the Fast request tier; a
# command must not turn this field into a general ``-c`` injection surface.
SUPPORTED_SERVICE_TIERS = frozenset({"fast"})
RECOVERY_REPORT_OUTCOMES = frozenset({"BLOCKED", "PARTIAL", "FAILED"})
RECOVERY_RESOLUTION = "interrupted_report_published"
WITHDRAWAL_RESOLUTION = "owner_withdrew_unclaimed_command_before_execution"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

COMMAND_REQUIRED_FIELDS = frozenset(
    {"command_id", "source", "based_on_report", "expected_generation", "kind"}
)
COMMAND_OPTIONAL_FIELDS = frozenset(
    {
        "executor",
        "manual_request_id",
        "supersedes_command_id",
        "withdraws_command_id",
    }
)
COMMAND_ALLOWED_FIELDS = COMMAND_REQUIRED_FIELDS | COMMAND_OPTIONAL_FIELDS
WITHDRAWAL_RECORD_FIELDS = frozenset(
    {
        "command_id",
        "source",
        "kind",
        "command_sha256",
        "reason",
        "withdrawn_at",
        "replacement_command_id",
        "resolution",
    }
)
EXECUTOR_REQUIRED_FIELDS = frozenset({"model", "reasoning_effort"})
EXECUTOR_FIELDS = EXECUTOR_REQUIRED_FIELDS | {"service_tier"}

COMMAND_META_RE = re.compile(
    r"^\s*<!--\s*bridge-command:\s*(\{.*?\})\s*-->\s*$",
    re.IGNORECASE,
)
FINAL_RESULT_PREFIX = "BRIDGE_FINAL_JSON:"
MODEL_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
COMMAND_METADATA_HEADER_LINES = 12


class ProtocolViolation(ValueError):
    """A malformed or unsupported Protocol-v2 value."""


class ProtocolConflict(ProtocolViolation):
    """A valid value that is stale or cannot win the current CAS decision."""


@dataclass(frozen=True)
class ManualGenerationPlan:
    """The two logical generations consumed by one manual fast-lane commit."""

    base_generation: int
    publication_generation: int
    claimed_generation: int


@dataclass(frozen=True)
class ManualStartDecision:
    """Pure result of checking whether manual start may supersede a command."""

    supersedes_command_id: int | None


@dataclass(frozen=True)
class ManualSupersedeDecision:
    """Pure decision for replacing one unclaimed scheduled command.

    This is a normal pending-command publication, not the manual fast lane:
    only one logical generation is consumed and no execution lease is made.
    """

    supersedes_command_id: int
    publication_generation: int


@dataclass(frozen=True)
class CommandRepairAssessment:
    """Pure evidence and decision for an unclaimed-command repair."""

    eligible: bool
    state_errors: tuple[str, ...]
    target_errors: tuple[str, ...]
    replacement_errors: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class WithdrawAndManualStartDecision:
    """Pure decision for atomic owner withdrawal plus manual claim."""

    target_command_id: int
    replacement_command_id: int
    publication_generation: int
    claimed_generation: int


def _require_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolViolation(f"{label} must be an integer")
    if value < minimum:
        raise ProtocolViolation(f"{label} must be >= {minimum}")
    return value


def _optional_int_equal(left: Any, right: Any) -> bool:
    """Compare persisted integer fields without treating missing fields as equal."""

    if left is None or right is None or isinstance(left, bool) or isinstance(right, bool):
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


def _next_generation(generation: int) -> int:
    return _require_int(generation, "generation", minimum=0) + 1


def publication_generation(generation: int) -> int:
    """Return the generation for a normal command publication: G -> G+1."""

    return _next_generation(generation)


def claim_generation(generation: int) -> int:
    """Return the generation for a normal execution claim: G -> G+1."""

    return _next_generation(generation)


def report_generation(generation: int) -> int:
    """Return the generation for a normal report publication: G -> G+1."""

    return _next_generation(generation)


def recovery_generation(generation: int) -> int:
    """Return the generation for a recovery transition: G -> G+1."""

    return _next_generation(generation)


def manual_claim_generations(base_generation: int) -> ManualGenerationPlan:
    """Return the manual atomic publication+claim logical generations."""

    base = _require_int(base_generation, "generation", minimum=0)
    return ManualGenerationPlan(
        base_generation=base,
        publication_generation=base + 1,
        claimed_generation=base + 2,
    )


def parse_command_metadata(command_text: str) -> dict[str, Any]:
    """Parse exactly one near-header ``bridge-command`` metadata line.

    The old Worker only considered the first twelve lines, so the header
    location remains bounded.  All matching lines are counted so a duplicate
    metadata line cannot be silently ignored.
    """

    if not isinstance(command_text, str):
        raise ProtocolViolation("Command text must be a string.")

    matches: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(command_text.splitlines()):
        match = COMMAND_META_RE.match(line)
        if match:
            matches.append((index, match))
    if not matches:
        raise ProtocolViolation(
            "Protocol v2 command is missing the bridge-command metadata comment."
        )
    if len(matches) != 1:
        raise ProtocolViolation(
            "Expected exactly one bridge-command metadata line, "
            f"found {len(matches)}."
        )
    if matches[0][0] >= COMMAND_METADATA_HEADER_LINES:
        raise ProtocolViolation(
            "Protocol v2 command is missing the bridge-command metadata comment "
            "near the command header."
        )

    try:
        value = json.loads(matches[0][1].group(1))
    except json.JSONDecodeError as exc:
        raise ProtocolViolation(f"Invalid bridge-command JSON metadata: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolViolation("bridge-command metadata must be a JSON object.")
    return value


def command_executor_override(meta: Mapping[str, Any]) -> dict[str, str] | None:
    """Validate and return the command-scoped executor override."""

    if "executor" not in meta:
        return None

    raw = meta["executor"]
    if not isinstance(raw, Mapping):
        raise ProtocolViolation("Command executor metadata must be a JSON object.")

    unknown = sorted(set(raw) - EXECUTOR_FIELDS)
    if unknown:
        raise ProtocolViolation(
            "Command executor metadata contains unsupported fields: "
            + ", ".join(unknown)
        )
    missing = sorted(EXECUTOR_REQUIRED_FIELDS - set(raw))
    if missing:
        raise ProtocolViolation(
            "Command executor metadata is missing required fields: "
            + ", ".join(missing)
        )

    model = raw["model"]
    if not isinstance(model, str) or not model:
        raise ProtocolViolation("Command executor model must be a non-empty string.")
    if not MODEL_VALUE_RE.fullmatch(model):
        raise ProtocolViolation(
            "Command executor model contains unsupported characters or CLI-token syntax."
        )

    reasoning_effort = raw["reasoning_effort"]
    if (
        not isinstance(reasoning_effort, str)
        or reasoning_effort not in SUPPORTED_REASONING_EFFORTS
    ):
        allowed = ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
        raise ProtocolViolation(
            "Unsupported command executor reasoning_effort; expected one of: "
            f"{allowed}."
        )

    result = {"model": model, "reasoning_effort": reasoning_effort}
    if "service_tier" in raw:
        service_tier = raw["service_tier"]
        if (
            not isinstance(service_tier, str)
            or service_tier not in SUPPORTED_SERVICE_TIERS
        ):
            allowed = ", ".join(sorted(SUPPORTED_SERVICE_TIERS))
            raise ProtocolViolation(
                "Unsupported command executor service_tier; expected one of: "
                f"{allowed}."
            )
        result["service_tier"] = service_tier
    return result


def validate_command_metadata(meta: Mapping[str, Any]) -> None:
    """Validate the normalized v2 command metadata contract."""

    if not isinstance(meta, Mapping):
        raise ProtocolViolation("bridge-command metadata must be a JSON object.")

    missing = sorted(COMMAND_REQUIRED_FIELDS - set(meta))
    if missing:
        raise ProtocolViolation(
            "Missing command metadata fields: " + ", ".join(missing)
        )
    unknown = sorted(set(meta) - COMMAND_ALLOWED_FIELDS)
    if unknown:
        raise ProtocolViolation(
            "Unknown command metadata fields: " + ", ".join(unknown)
        )

    _require_int(meta["command_id"], "command_id", minimum=1)
    _require_int(meta["based_on_report"], "based_on_report", minimum=0)
    _require_int(meta["expected_generation"], "expected_generation", minimum=1)

    if not isinstance(meta["source"], str) or meta["source"] not in COMMAND_SOURCES:
        raise ProtocolViolation(f"Unsupported command source: {meta['source']!r}")
    if not isinstance(meta["kind"], str) or meta["kind"] not in COMMAND_KINDS:
        raise ProtocolViolation(f"Unsupported command kind: {meta['kind']!r}")

    command_executor_override(meta)

    if "manual_request_id" in meta:
        request_id = meta["manual_request_id"]
        if not isinstance(request_id, str) or not request_id:
            raise ProtocolViolation("manual_request_id must be a non-empty string.")
    if "supersedes_command_id" in meta:
        _require_int(meta["supersedes_command_id"], "supersedes_command_id", minimum=1)
    if "withdraws_command_id" in meta:
        _require_int(meta["withdraws_command_id"], "withdraws_command_id", minimum=1)
        if "supersedes_command_id" in meta:
            raise ProtocolViolation(
                "A command cannot contain both supersedes_command_id and "
                "withdraws_command_id."
            )


def validate_withdrawal_record(record: Mapping[str, Any]) -> None:
    """Validate one immutable owner-withdrawal state record."""

    if not isinstance(record, Mapping):
        raise ProtocolViolation("withdrawal record must be an object.")
    missing = sorted(WITHDRAWAL_RECORD_FIELDS - set(record))
    if missing:
        raise ProtocolViolation(
            "Missing withdrawal record fields: " + ", ".join(missing)
        )
    unknown = sorted(set(record) - WITHDRAWAL_RECORD_FIELDS)
    if unknown:
        raise ProtocolViolation(
            "Unknown withdrawal record fields: " + ", ".join(unknown)
        )
    command_id = _require_int(record["command_id"], "withdrawal command_id", minimum=1)
    replacement_id = _require_int(
        record["replacement_command_id"],
        "withdrawal replacement_command_id",
        minimum=1,
    )
    if replacement_id <= command_id:
        raise ProtocolViolation(
            "withdrawal replacement_command_id must be newer than command_id."
        )
    if record["source"] not in COMMAND_SOURCES:
        raise ProtocolViolation("withdrawal record source is unsupported.")
    if record["kind"] not in COMMAND_KINDS:
        raise ProtocolViolation("withdrawal record kind is unsupported.")
    digest = record["command_sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ProtocolViolation("withdrawal record command_sha256 is invalid.")
    reason = record["reason"]
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 512
        or any(char in reason for char in "\r\n")
    ):
        raise ProtocolViolation("withdrawal record reason is invalid.")
    if not isinstance(record["withdrawn_at"], str) or not record["withdrawn_at"]:
        raise ProtocolViolation("withdrawal record withdrawn_at is invalid.")
    if record["resolution"] != WITHDRAWAL_RESOLUTION:
        raise ProtocolViolation("withdrawal record resolution is unsupported.")

def command_contract_errors(
    meta: Any,
    *,
    filename_command_id: int | None = None,
) -> tuple[str, ...]:
    """Return bounded deterministic errors for one command contract.

    This helper intentionally does not compare the command with canonical
    state.  Callers that need the pending-command boundary must add those
    state-relative checks explicitly.  Keeping the contract-only result pure
    lets the operator repair boundary and repository conformance share the
    same failure classification without importing Worker orchestration.
    """

    errors: list[str] = []
    if not isinstance(meta, Mapping):
        return ("bridge-command metadata must be a JSON object.",)
    try:
        validate_command_metadata(meta)
    except ProtocolViolation as exc:
        errors.append(str(exc))

    if (
        filename_command_id is not None
        and not isinstance(meta.get("command_id"), bool)
        and isinstance(meta.get("command_id"), int)
        and meta["command_id"] != filename_command_id
    ):
        errors.append(
            f"command_id {meta['command_id']} does not match filename id "
            f"{filename_command_id}"
        )
    return tuple(errors)


def assess_unclaimed_command_repair(
    *,
    state: Mapping[str, Any],
    target_command_id: Any,
    target_meta: Mapping[str, Any] | None,
    target_parse_error: str | None = None,
    target_filename_command_id: int | None = None,
    target_file_exists: bool = True,
    target_report_exists: bool = False,
    replacement_report_exists: bool = False,
    replacement_meta: Mapping[str, Any] | None = None,
    replacement_filename_command_id: int | None = None,
    existing_command_ids: Iterable[int] = (),
    preserve_target_source_kind: bool = True,
    allow_existing_replacement_id: int | None = None,
) -> CommandRepairAssessment:
    """Assess the narrow Protocol-v2 unclaimed-command repair transition.

    The function only consumes already-loaded values.  It does not read Git,
    invoke an executor, create a run, or perform a write.  With no
    ``replacement_meta`` it answers the read-only ``inspect`` question.  With
    a replacement it validates the complete one-step repair contract.

    A command is repairable only when the canonical snapshot is an idle
    ``COMMAND_READY`` boundary and the canonical target has at least one
    deterministic command-contract error.  State/lease/report guards are
    never treated as repairable command errors.
    """

    state_errors: list[str] = []
    target_errors: list[str] = []
    replacement_errors: list[str] = []

    if state.get("protocol_version") != PROTOCOL_VERSION:
        state_errors.append("canonical state protocol_version must be 2")
    if state.get("status") != "COMMAND_READY":
        state_errors.append("canonical status must be COMMAND_READY")
    if state.get("active_run") is not None:
        state_errors.append("canonical active_run must be null")
    if not target_file_exists:
        state_errors.append("canonical target command file must exist")
    if target_report_exists:
        state_errors.append("canonical target already has a report")

    normalized_target: int | None
    if isinstance(target_command_id, bool) or not isinstance(target_command_id, int):
        normalized_target = None
        state_errors.append("repair target command id must be an integer")
    elif target_command_id < 1:
        normalized_target = None
        state_errors.append("repair target command id must be >= 1")
    else:
        normalized_target = target_command_id

    generation: int | None = None
    raw_generation = state.get("generation")
    if isinstance(raw_generation, bool) or not isinstance(raw_generation, int) or raw_generation < 0:
        state_errors.append("canonical generation must be a non-negative integer")
    else:
        generation = raw_generation

    latest_command: int | None = None
    raw_latest_command = state.get("latest_command")
    if (
        isinstance(raw_latest_command, bool)
        or not isinstance(raw_latest_command, int)
        or raw_latest_command < 1
    ):
        state_errors.append("canonical latest_command must be a positive integer")
    else:
        latest_command = raw_latest_command

    latest_report: int | None = None
    raw_latest_report = state.get("latest_report")
    if (
        isinstance(raw_latest_report, bool)
        or not isinstance(raw_latest_report, int)
        or raw_latest_report < 0
    ):
        state_errors.append("canonical latest_report must be a non-negative integer")
    else:
        latest_report = raw_latest_report

    if normalized_target is not None and latest_command is not None:
        if normalized_target != latest_command:
            state_errors.append(
                "repair target command id must equal canonical latest_command"
            )

    if target_parse_error is not None:
        target_errors.append(
            "canonical target command metadata is not parseable: "
            + str(target_parse_error)
        )
    elif target_meta is None:
        target_errors.append("canonical target command metadata is unavailable")
    else:
        target_errors.extend(
            command_contract_errors(
                target_meta,
                filename_command_id=target_filename_command_id,
            )
        )
        raw_expected = target_meta.get("expected_generation")
        if (
            generation is not None
            and isinstance(raw_expected, int)
            and not isinstance(raw_expected, bool)
            and raw_expected >= 0
            and raw_expected != generation
        ):
            target_errors.append(
                "target expected_generation must equal canonical generation"
            )
        raw_based_on_report = target_meta.get("based_on_report")
        if (
            latest_report is not None
            and isinstance(raw_based_on_report, int)
            and not isinstance(raw_based_on_report, bool)
            and raw_based_on_report >= 0
            and raw_based_on_report != latest_report
        ):
            target_errors.append(
                "target based_on_report must equal canonical latest_report"
            )

    if replacement_meta is not None:
        if replacement_report_exists:
            replacement_errors.append("staged replacement already has a report")
        replacement_errors.extend(
            command_contract_errors(
                replacement_meta,
                filename_command_id=replacement_filename_command_id,
            )
        )

        # Preserve the readable intent of the bad command when its source and
        # kind are themselves valid.  If either value is the deterministic
        # defect, the explicit replacement is allowed to repair that value;
        # it still has to pass the complete v2 contract above.
        if preserve_target_source_kind and isinstance(target_meta, Mapping):
            target_source = target_meta.get("source")
            if (
                target_source in COMMAND_SOURCES
                and replacement_meta.get("source") != target_source
            ):
                replacement_errors.append(
                    "replacement source must preserve the target command source"
                )
            target_kind = target_meta.get("kind")
            if (
                target_kind in COMMAND_KINDS
                and replacement_meta.get("kind") != target_kind
            ):
                replacement_errors.append(
                    "replacement kind must preserve the target command kind"
                )

        replacement_id = replacement_meta.get("command_id")
        replacement_id_valid = (
            isinstance(replacement_id, int)
            and not isinstance(replacement_id, bool)
            and replacement_id >= 1
        )
        if replacement_id_valid:
            known_ids = {
                value
                for value in existing_command_ids
                if isinstance(value, int) and not isinstance(value, bool) and value >= 1
            }
            if allow_existing_replacement_id is not None:
                if (
                    isinstance(allow_existing_replacement_id, bool)
                    or not isinstance(allow_existing_replacement_id, int)
                    or allow_existing_replacement_id < 1
                ):
                    replacement_errors.append(
                        "allowed staged replacement id must be a positive integer"
                    )
                elif replacement_id != allow_existing_replacement_id:
                    replacement_errors.append(
                        "staged replacement id must match its filename id"
                    )
                else:
                    # A staged replacement is already present in immutable
                    # history.  It is the one explicit exception to the
                    # normal repair rule that the replacement id must be
                    # absent before publication.
                    known_ids.discard(allow_existing_replacement_id)
            if latest_command is not None:
                known_ids.add(latest_command)
            if replacement_id <= max(known_ids, default=0):
                replacement_errors.append(
                    "replacement command_id must be greater than every existing command id"
                )
            if normalized_target is not None and replacement_id == normalized_target:
                replacement_errors.append(
                    "replacement command_id must not self-supersede the target"
                )
        elif replacement_filename_command_id is not None:
            replacement_errors.append("replacement command_id is not a positive integer")

        if (
            replacement_filename_command_id is not None
            and replacement_id_valid
            and replacement_id != replacement_filename_command_id
        ):
            replacement_errors.append(
                "replacement command_id must match its command filename"
            )

        if "supersedes_command_id" not in replacement_meta:
            replacement_errors.append(
                "repair replacement must declare supersedes_command_id"
            )
        else:
            superseded = replacement_meta.get("supersedes_command_id")
            if (
                isinstance(superseded, int)
                and not isinstance(superseded, bool)
                and superseded >= 1
            ):
                if normalized_target is not None and superseded != normalized_target:
                    replacement_errors.append(
                        "replacement supersedes_command_id must equal the exact target command id"
                    )
                if replacement_id_valid and superseded >= replacement_id:
                    replacement_errors.append(
                        "replacement supersedes_command_id must refer to an earlier command id"
                    )

        raw_replacement_expected = replacement_meta.get("expected_generation")
        if (
            generation is not None
            and isinstance(raw_replacement_expected, int)
            and not isinstance(raw_replacement_expected, bool)
            and raw_replacement_expected != generation + 1
        ):
            replacement_errors.append(
                "replacement expected_generation must equal canonical generation + 1"
            )

        raw_replacement_report = replacement_meta.get("based_on_report")
        if (
            latest_report is not None
            and isinstance(raw_replacement_report, int)
            and not isinstance(raw_replacement_report, bool)
            and raw_replacement_report != latest_report
        ):
            replacement_errors.append(
                "replacement based_on_report must equal canonical latest_report"
            )

    errors = state_errors + target_errors + replacement_errors
    if not target_errors:
        errors.append("target command has no deterministic Protocol-v2 contract error")

    eligible = (
        not state_errors
        and bool(target_errors)
        and (replacement_meta is None or not replacement_errors)
    )
    return CommandRepairAssessment(
        eligible=eligible,
        state_errors=tuple(state_errors),
        target_errors=tuple(target_errors),
        replacement_errors=tuple(replacement_errors),
        errors=tuple(errors),
    )


def supersede_relation_errors(
    *,
    replacement_command_id: Any,
    target_command_id: Any,
    existing_command_ids: Iterable[int],
    existing_supersedes: Mapping[int, int],
    canonical_latest_command: int | None = None,
) -> tuple[str, ...]:
    """Return deterministic errors for an append-only supersede graph.

    The caller supplies already-read command ids and valid integer relation
    fields.  The function is intentionally side-effect-free so operators and
    repository conformance can share the same self/future/missing/conflict/
    cycle checks without building another persistence or lease mechanism.
    """

    errors: list[str] = []

    def positive_id(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value

    known_ids = {
        value
        for value in existing_command_ids
        if positive_id(value) is not None
    }
    replacement_id = positive_id(replacement_command_id)
    target_id = positive_id(target_command_id)

    if replacement_id is None:
        errors.append("replacement command id must be a positive integer")
    elif replacement_id not in known_ids:
        errors.append("staged replacement command id is not present in command history")
    if target_id is None:
        errors.append("supersede target command id must be a positive integer")
    elif target_id not in known_ids:
        errors.append(
            f"supersedes_command_id references missing command-{target_id:03d}"
        )
    if replacement_id is not None and target_id is not None:
        if replacement_id == target_id:
            errors.append("supersedes_command_id must not self-reference")
        elif target_id > replacement_id:
            errors.append("supersedes_command_id must refer to an earlier command id")

    edges: dict[int, int] = {}
    for raw_source, raw_target in existing_supersedes.items():
        source_id = positive_id(raw_source)
        relation_target_id = positive_id(raw_target)
        if source_id is None or relation_target_id is None:
            continue
        edges[source_id] = relation_target_id
    if replacement_id is not None and target_id is not None:
        edges[replacement_id] = target_id

    relation_targets: dict[int, list[int]] = {}
    for source_id, relation_target_id in edges.items():
        relation_targets.setdefault(relation_target_id, []).append(source_id)
        if source_id == relation_target_id:
            errors.append("supersedes_command_id must not self-reference")
        elif relation_target_id > source_id:
            errors.append("supersedes_command_id must refer to an earlier command id")
        if relation_target_id not in known_ids:
            errors.append(
                f"supersedes_command_id references missing command-{relation_target_id:03d}"
            )
        if (
            canonical_latest_command is not None
            and source_id > canonical_latest_command
        ):
            errors.append("superseder command is beyond canonical latest_command")

    for relation_target_id, superseder_ids in relation_targets.items():
        if len(set(superseder_ids)) > 1:
            errors.append(
                f"command-{relation_target_id:03d} has multiple conflicting superseders"
            )

    for start in edges:
        visited: set[int] = set()
        current = start
        while current in edges:
            if current in visited:
                errors.append("supersede relation contains a cycle")
                break
            visited.add(current)
            current = edges[current]

    return tuple(dict.fromkeys(errors))


def assess_staged_unclaimed_command_repair(
    *,
    state: Mapping[str, Any],
    target_command_id: Any,
    target_meta: Mapping[str, Any] | None,
    target_parse_error: str | None = None,
    target_filename_command_id: int | None = None,
    target_file_exists: bool = True,
    target_report_exists: bool = False,
    replacement_report_exists: bool = False,
    replacement_meta: Mapping[str, Any] | None = None,
    replacement_filename_command_id: int | None = None,
    existing_command_ids: Iterable[int] = (),
    existing_supersedes: Mapping[int, int] | None = None,
) -> CommandRepairAssessment:
    """Assess adoption of one already-existing replacement command.

    This is the staged form of unclaimed repair.  It shares the complete
    command/state contract with normal repair, but deliberately permits the
    replacement's source/kind to express a newer explicit command (for
    example scheduled ``009`` replaced by manual ``010``) and permits exactly
    the filename-bound replacement id already present in immutable history.
    """

    known_command_ids = tuple(existing_command_ids)
    base = assess_unclaimed_command_repair(
        state=state,
        target_command_id=target_command_id,
        target_meta=target_meta,
        target_parse_error=target_parse_error,
        target_filename_command_id=target_filename_command_id,
        target_file_exists=target_file_exists,
        target_report_exists=target_report_exists,
        replacement_report_exists=replacement_report_exists,
        replacement_meta=replacement_meta,
        replacement_filename_command_id=replacement_filename_command_id,
        existing_command_ids=known_command_ids,
        preserve_target_source_kind=False,
        allow_existing_replacement_id=replacement_filename_command_id,
    )

    staged_errors: list[str] = []
    if replacement_meta is None:
        staged_errors.append("staged replacement command metadata is unavailable")

    relation_errors = supersede_relation_errors(
        replacement_command_id=(
            replacement_meta.get("command_id")
            if isinstance(replacement_meta, Mapping)
            else None
        ),
        target_command_id=target_command_id,
        existing_command_ids=known_command_ids,
        existing_supersedes=existing_supersedes or {},
        canonical_latest_command=(
            replacement_meta.get("command_id")
            if isinstance(replacement_meta, Mapping)
            and isinstance(replacement_meta.get("command_id"), int)
            and not isinstance(replacement_meta.get("command_id"), bool)
            else None
        ),
    )
    staged_errors.extend(relation_errors)

    replacement_errors = tuple(
        dict.fromkeys((*base.replacement_errors, *staged_errors))
    )
    errors = tuple(dict.fromkeys((*base.errors, *staged_errors)))
    return CommandRepairAssessment(
        eligible=base.eligible and not staged_errors,
        state_errors=base.state_errors,
        target_errors=base.target_errors,
        replacement_errors=replacement_errors,
        errors=errors,
    )


def validate_pending_command(
    *,
    state: Mapping[str, Any],
    command_id: int,
    meta: Mapping[str, Any],
) -> None:
    """Validate metadata and stale generation/report guards at claim time."""

    validate_command_metadata(meta)
    requested_command_id = _require_int(command_id, "command_id", minimum=1)
    meta_command_id = int(meta["command_id"])
    if meta_command_id != requested_command_id:
        raise ProtocolViolation(
            f"Command metadata id {meta_command_id} does not match state id "
            f"{requested_command_id}."
        )

    expected_generation = int(meta["expected_generation"])
    state_generation = _require_int(
        state.get("generation"), "state generation", minimum=0
    )
    if expected_generation != state_generation:
        raise ProtocolConflict(
            f"Stale command {requested_command_id:03d}: expected generation "
            f"{expected_generation}, current {state.get('generation')}."
        )

    based_on_report = int(meta["based_on_report"])
    state_latest_report = _require_int(
        state.get("latest_report"), "state latest_report", minimum=0
    )
    if based_on_report != state_latest_report:
        raise ProtocolConflict(
            f"Stale command {requested_command_id:03d}: "
            f"based_on_report={based_on_report}, "
            f"current latest_report={state.get('latest_report')}."
        )


def manual_start_decision(
    state: Mapping[str, Any],
    existing_meta: Mapping[str, Any] | None,
) -> ManualStartDecision:
    """Decide whether the manual fast lane may start/supersede a command."""

    try:
        protocol_version = int(state.get("protocol_version", 1))
    except (TypeError, ValueError) as exc:
        raise ProtocolViolation("Manual fast lane requires protocol_version >= 2.") from exc
    if protocol_version < PROTOCOL_VERSION:
        raise ProtocolViolation("Manual fast lane requires protocol_version >= 2.")
    if state.get("active_run"):
        raise ProtocolConflict("Project already has an active execution lease.")

    status = str(state.get("status", ""))
    if status == "REPORT_READY":
        return ManualStartDecision(supersedes_command_id=None)

    if status == "COMMAND_READY":
        if not isinstance(existing_meta, Mapping):
            raise ProtocolViolation("COMMAND_READY requires readable command metadata.")
        # The canonical-command loader performs the complete metadata/schema
        # validation before reaching this decision.  Keep this narrow check
        # compatible with the historical helper's semi-public API, which was
        # also callable with only source/kind fields in unit fixtures.
        source = str(existing_meta.get("source", ""))
        kind = str(existing_meta.get("kind", "EXECUTE")).upper()
        if source != "scheduled_chatgpt" or kind != "EXECUTE":
            raise ProtocolConflict(
                "Manual fast lane may supersede only an unclaimed normal "
                "scheduled_chatgpt command."
            )
        return ManualStartDecision(
            supersedes_command_id=int(state.get("latest_command", 0))
        )

    if status == "CODEX_RUNNING":
        raise ProtocolConflict(
            "Codex is already running for this project. Manual execution must wait."
        )

    raise ProtocolConflict(
        f"Manual fast lane cannot start while project status is {status!r}."
    )


def manual_supersede_decision(
    state: Mapping[str, Any],
    existing_meta: Mapping[str, Any],
) -> ManualSupersedeDecision:
    """Validate the ordinary manual priority queue-boundary transition.

    The only admissible source command is a fully valid, unclaimed,
    canonical ``scheduled_chatgpt``/``EXECUTE`` command.  The caller must
    still bind the decision to the command file and perform the final CAS;
    this function only evaluates already-loaded values.
    """

    if state.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolViolation(
            "Manual supersede requires exactly Protocol-v2 canonical state."
        )
    if state.get("status") != "COMMAND_READY":
        raise ProtocolConflict(
            "Manual supersede is allowed only at the COMMAND_READY queue boundary."
        )
    if "active_run" not in state:
        raise ProtocolViolation("Manual supersede requires an explicit active_run=null.")
    if state.get("active_run") is not None:
        raise ProtocolConflict("Manual supersede cannot interrupt an active execution lease.")

    generation = _require_int(state.get("generation"), "generation", minimum=0)
    command_id = _require_int(state.get("latest_command"), "latest_command", minimum=1)
    latest_report = _require_int(
        state.get("latest_report"), "latest_report", minimum=0
    )
    if command_id <= latest_report:
        raise ProtocolConflict(
            "Manual supersede requires a pending command newer than latest_report."
        )

    validate_pending_command(
        state=state,
        command_id=command_id,
        meta=existing_meta,
    )
    if existing_meta.get("source") != "scheduled_chatgpt":
        raise ProtocolConflict(
            "Manual supersede targets only a normal scheduled_chatgpt command."
        )
    if existing_meta.get("kind") != "EXECUTE":
        raise ProtocolConflict(
            "Manual supersede targets only a scheduled EXECUTE command."
        )
    if "manual_request_id" in existing_meta:
        raise ProtocolConflict(
            "Manual supersede target already carries manual request identity."
        )
    if "supersedes_command_id" in existing_meta:
        raise ProtocolConflict(
            "Manual supersede target is already a historical replacement."
        )
    return ManualSupersedeDecision(
        supersedes_command_id=command_id,
        publication_generation=publication_generation(generation),
    )


def withdraw_and_manual_start_decision(
    state: Mapping[str, Any],
    *,
    target_command_id: Any,
    target_meta: Mapping[str, Any] | None,
    target_report_exists: bool,
    reason: Any,
    replacement_meta: Mapping[str, Any] | None,
    replacement_filename_command_id: int | None,
    existing_command_ids: Iterable[int],
    existing_withdrawal_command_ids: Iterable[int] = (),
) -> WithdrawAndManualStartDecision:
    """Validate atomic withdrawal, replacement publication, and claim.

    The target must be a valid canonical unclaimed manual EXECUTE command. The
    replacement is validated as a new manual command whose metadata records a
    withdrawal, and the returned generations are the two logical steps that
    the single Git CAS commit performs.
    """

    if state.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolViolation(
            "Owner withdrawal requires exactly Protocol-v2 canonical state."
        )
    if state.get("status") != "COMMAND_READY":
        raise ProtocolConflict(
            "Owner withdrawal is allowed only at the COMMAND_READY queue boundary."
        )
    if "active_run" not in state:
        raise ProtocolViolation(
            "Owner withdrawal requires an explicit active_run=null."
        )
    if state.get("active_run") is not None:
        raise ProtocolConflict(
            "Owner withdrawal cannot interrupt an active execution lease."
        )
    if target_report_exists:
        raise ProtocolConflict(
            "Owner withdrawal requires the target command to have no report."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ProtocolViolation("Owner withdrawal reason must be non-empty text.")
    if len(reason.strip()) > 512 or any(char in reason for char in "\r\n"):
        raise ProtocolViolation(
            "Owner withdrawal reason must be one line of at most 512 characters."
        )

    if (
        isinstance(target_command_id, bool)
        or not isinstance(target_command_id, int)
        or target_command_id < 1
    ):
        raise ProtocolViolation("Owner withdrawal target command id must be positive.")
    command_id = _require_int(
        state.get("latest_command"), "latest_command", minimum=1
    )
    latest_report = _require_int(
        state.get("latest_report"), "latest_report", minimum=0
    )
    if target_command_id != command_id:
        raise ProtocolConflict(
            "Owner withdrawal target command id must equal canonical latest_command."
        )
    if command_id <= latest_report:
        raise ProtocolConflict(
            "Owner withdrawal requires a command newer than latest_report."
        )
    if not isinstance(target_meta, Mapping):
        raise ProtocolViolation("Owner withdrawal target metadata is unavailable.")

    validate_pending_command(
        state=state,
        command_id=command_id,
        meta=target_meta,
    )
    if target_meta.get("source") != "manual_chatgpt":
        raise ProtocolConflict(
            "Owner withdrawal targets only a manual_chatgpt command."
        )
    if target_meta.get("kind") != "EXECUTE":
        raise ProtocolConflict(
            "Owner withdrawal targets only a manual EXECUTE command."
        )
    if "supersedes_command_id" in target_meta:
        raise ProtocolConflict(
            "Owner withdrawal does not rewrite a historical supersede relation."
        )
    if "withdraws_command_id" in target_meta:
        raise ProtocolConflict(
            "Owner withdrawal target already carries a withdrawal relation."
        )
    withdrawn_targets = {
        value
        for value in existing_withdrawal_command_ids
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1
    }
    if command_id in withdrawn_targets:
        raise ProtocolConflict(
            "Owner withdrawal target already has an immutable withdrawal record."
        )

    generation = _require_int(state.get("generation"), "generation", minimum=0)
    publication = publication_generation(generation)
    claimed = claim_generation(publication)

    if not isinstance(replacement_meta, Mapping):
        raise ProtocolViolation("Manual replacement command metadata is unavailable.")
    if (
        isinstance(replacement_filename_command_id, bool)
        or not isinstance(replacement_filename_command_id, int)
        or replacement_filename_command_id < 1
    ):
        raise ProtocolViolation(
            "Manual replacement command filename id must be positive."
        )
    replacement_errors = command_contract_errors(
        replacement_meta,
        filename_command_id=replacement_filename_command_id,
    )
    if replacement_errors:
        raise ProtocolViolation(
            "Manual replacement command is not a complete Protocol-v2 command: "
            + "; ".join(replacement_errors)
        )
    replacement_id = _require_int(
        replacement_meta.get("command_id"),
        "replacement command_id",
        minimum=1,
    )
    known_ids = {
        value
        for value in existing_command_ids
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1
    }
    if replacement_id in known_ids:
        raise ProtocolConflict(
            "Manual replacement command id already exists in command history."
        )
    if replacement_id <= max(known_ids, default=0):
        raise ProtocolConflict(
            "Manual replacement command id must be monotonic beyond command history."
        )
    if replacement_meta.get("source") != "manual_chatgpt":
        raise ProtocolViolation(
            "Manual replacement command source must be manual_chatgpt."
        )
    if replacement_meta.get("kind") != "EXECUTE":
        raise ProtocolViolation("Manual replacement command kind must be EXECUTE.")
    if replacement_meta.get("based_on_report") != latest_report:
        raise ProtocolConflict(
            "Manual replacement based_on_report must equal latest_report."
        )
    if replacement_meta.get("expected_generation") != publication:
        raise ProtocolConflict(
            "Manual replacement expected_generation must equal publication generation."
        )
    if replacement_meta.get("withdraws_command_id") != command_id:
        raise ProtocolViolation(
            "Manual replacement must withdraw the exact target command."
        )
    if "supersedes_command_id" in replacement_meta:
        raise ProtocolViolation(
            "Manual withdrawal replacement must not use supersedes_command_id."
        )
    if not isinstance(replacement_meta.get("manual_request_id"), str) or not replacement_meta[
        "manual_request_id"
    ]:
        raise ProtocolViolation(
            "Manual withdrawal replacement requires manual_request_id."
        )
    return WithdrawAndManualStartDecision(
        target_command_id=command_id,
        replacement_command_id=replacement_id,
        publication_generation=publication,
        claimed_generation=claimed,
    )


def same_ready_snapshot(
    current: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> bool:
    """Return whether a pending state still matches its CAS read snapshot."""

    return (
        current.get("status") == snapshot.get("status")
        and _optional_int_equal(current.get("generation"), snapshot.get("generation"))
        and _optional_int_equal(
            current.get("latest_command"), snapshot.get("latest_command")
        )
        and _optional_int_equal(
            current.get("latest_report"), snapshot.get("latest_report")
        )
        and not current.get("active_run")
    )


def matches_execution_lease(
    state: Mapping[str, Any],
    *,
    run_id: str,
    command_id: int,
    claimed_generation: int,
    source: str | None = None,
    kind: str | None = None,
    project_id: str | None = None,
) -> bool:
    """Match the exact active execution lease, with optional qualifiers."""

    if not isinstance(run_id, str) or not run_id:
        return False
    if (
        isinstance(command_id, bool)
        or not isinstance(command_id, int)
        or command_id < 1
        or isinstance(claimed_generation, bool)
        or not isinstance(claimed_generation, int)
        or claimed_generation < 0
    ):
        return False
    active = state.get("active_run")
    if state.get("status") != "CODEX_RUNNING":
        return False
    if project_id is not None and state.get("project_id") != project_id:
        return False
    if not _optional_int_equal(state.get("generation"), claimed_generation):
        return False
    if not isinstance(active, Mapping):
        return False
    if active.get("run_id") != run_id:
        return False
    if not _optional_int_equal(active.get("command_id"), command_id):
        return False
    if not _optional_int_equal(active.get("claimed_generation"), claimed_generation):
        return False
    if source is not None and active.get("source") != source:
        return False
    if kind is not None and active.get("kind") != kind:
        return False
    return True


def matches_recovery_running_state(
    journal: Mapping[str, Any],
    state: Mapping[str, Any],
) -> bool:
    """Match a journal to the exact still-running canonical execution lease."""

    project_id = journal.get("project_id")
    run_id = journal.get("run_id")
    command_id = journal.get("command_id")
    claimed_generation = journal.get("claim_generation")
    if (
        not isinstance(project_id, str)
        or not project_id
        or not isinstance(run_id, str)
        or not run_id
        or isinstance(command_id, bool)
        or not isinstance(command_id, int)
        or command_id < 1
        or isinstance(claimed_generation, bool)
        or not isinstance(claimed_generation, int)
        or claimed_generation < 0
    ):
        return False
    return matches_execution_lease(
        state,
        run_id=run_id,
        command_id=command_id,
        claimed_generation=claimed_generation,
        project_id=project_id,
    )


def matches_recovery_state(
    journal: Mapping[str, Any],
    state: Mapping[str, Any],
) -> bool:
    """Recognize only the recovery state produced for this exact journal."""

    claim_generation = journal.get("claim_generation")
    command_id = journal.get("command_id")
    project_id = journal.get("project_id")
    run_id = journal.get("run_id")
    if (
        isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or claim_generation < 0
        or isinstance(command_id, bool)
        or not isinstance(command_id, int)
        or command_id < 1
        or not isinstance(project_id, str)
        or not project_id
        or not isinstance(run_id, str)
        or not run_id
    ):
        return False
    return (
        state.get("status") == "RECOVERY_REQUIRED"
        and state.get("active_run") is None
        and state.get("worker_pid") is None
        and state.get("project_id") == project_id
        and _optional_int_equal(state.get("generation"), claim_generation + 1)
        and _optional_int_equal(state.get("latest_command"), command_id)
        and run_id in str(state.get("recovery_reason", ""))
    )


def _recovery_identity_matches(
    identity: Mapping[str, Any],
    *,
    project_id: str,
    command_id: int,
    run_id: str,
    claim_generation: int,
    source: str,
    kind: str,
    based_on_report: int,
    outcome: str,
) -> bool:
    """Return whether a normalized recovery report has the exact identity."""

    return (
        identity.get("project_id") == project_id
        and identity.get("command_id") == command_id
        and identity.get("run_id") == run_id
        and identity.get("claim_generation") == claim_generation
        and identity.get("source") == source
        and identity.get("kind") == kind
        and identity.get("based_on_report") == based_on_report
        and identity.get("outcome") == outcome
        and identity.get("interrupted") is True
        and identity.get("no_final_success_marker") is True
        and identity.get("external_side_effects_unknown") is True
    )


def _recovery_note_matches(
    note: Any,
    *,
    command_id: int,
    run_id: str,
    claim_generation: int,
) -> bool:
    if not isinstance(note, Mapping):
        return False
    return (
        note.get("resolved_command_id") == command_id
        and note.get("run_id") == run_id
        and note.get("claim_generation") == claim_generation
        and note.get("resolution") == RECOVERY_RESOLUTION
        and isinstance(note.get("resolved_at"), str)
        and bool(note.get("resolved_at"))
    )


def validate_recovery_resolution(
    *,
    state: Mapping[str, Any],
    journal: Mapping[str, Any],
    report_identity: Mapping[str, Any],
    project_id: str,
    command_id: int,
    run_id: str,
    claim_generation: int,
    expected_generation: int,
    source: str,
    kind: str,
    based_on_report: int,
    pending_report_path: str,
    expected_report_sha256: str,
    actual_report_sha256: str,
    canonical_report_exists: bool,
    canonical_report_matches: bool,
) -> str:
    """Validate the pure recovery-resolution decision.

    This function deliberately accepts only already-loaded values.  It does
    not read files, inspect Git, invoke an executor, or perform a write.  The
    return value is ``ALLOW`` for the one recovery transition and
    ``ALREADY_RESOLVED`` for an exact, already-applied result.  Any changed
    identity, generation, report integrity, or newer canonical state raises a
    protocol exception so callers fail closed.
    """

    _require_int(command_id, "command_id", minimum=1)
    _require_int(claim_generation, "claim_generation", minimum=0)
    _require_int(expected_generation, "expected_generation", minimum=0)
    _require_int(based_on_report, "based_on_report", minimum=0)
    if not isinstance(project_id, str) or not project_id:
        raise ProtocolViolation("recovery project_id must be non-empty")
    if not isinstance(run_id, str) or not run_id:
        raise ProtocolViolation("recovery run_id must be non-empty")
    if not isinstance(source, str) or source not in COMMAND_SOURCES:
        raise ProtocolViolation("recovery source is not a Protocol v2 source")
    if not isinstance(kind, str) or kind not in COMMAND_KINDS:
        raise ProtocolViolation("recovery kind is not a Protocol v2 kind")
    if not isinstance(pending_report_path, str) or not pending_report_path:
        raise ProtocolViolation("recovery pending_report_path must be non-empty")
    if (
        not isinstance(expected_report_sha256, str)
        or not SHA256_RE.fullmatch(expected_report_sha256)
        or not isinstance(actual_report_sha256, str)
        or not SHA256_RE.fullmatch(actual_report_sha256)
        or expected_report_sha256 != actual_report_sha256
    ):
        raise ProtocolConflict("reviewed pending report SHA-256 does not match")
    if not isinstance(canonical_report_exists, bool) or not isinstance(
        canonical_report_matches, bool
    ):
        raise ProtocolViolation("canonical report presence/match flags must be boolean")
    if canonical_report_exists and not canonical_report_matches:
        raise ProtocolConflict(
            "canonical report exists but does not match the reviewed recovery evidence"
        )

    journal_identity = {
        "project_id": journal.get("project_id"),
        "command_id": journal.get("command_id"),
        "run_id": journal.get("run_id"),
        "claim_generation": journal.get("claim_generation"),
    }
    if journal_identity != {
        "project_id": project_id,
        "command_id": command_id,
        "run_id": run_id,
        "claim_generation": claim_generation,
    }:
        raise ProtocolConflict("recovery journal identity does not match the requested run")
    if journal.get("journal_status") != "reconciled":
        raise ProtocolConflict("recovery journal is not reconciled")
    if journal.get("remote_publish_pending") is not False:
        raise ProtocolConflict("recovery journal still has remote publication pending")
    if not isinstance(journal.get("interruption_kind"), str) or not journal.get(
        "interruption_kind"
    ):
        raise ProtocolViolation("recovery journal interruption_kind is missing")
    if not isinstance(
        journal.get("interruption_reason_safe"), str
    ) or not journal.get("interruption_reason_safe"):
        raise ProtocolViolation("recovery journal interruption_reason_safe is missing")
    if journal.get("pending_report_path") != pending_report_path:
        raise ProtocolConflict("recovery journal pending report path does not match")
    expected_pending_report_path = (
        f"worker/runtime/{project_id}/pending-report-{command_id:03d}.md"
    )
    if pending_report_path != expected_pending_report_path:
        raise ProtocolConflict("recovery pending report path is not canonical")
    expected_canonical_report_path = (
        f"projects/{project_id}/reports/report-{command_id:03d}.md"
    )
    if journal.get("report_path") != expected_canonical_report_path:
        raise ProtocolConflict("recovery canonical report path is not canonical")
    if journal.get("external_side_effects_unknown") is not True:
        raise ProtocolConflict(
            "recovery resolution requires external_side_effects_unknown=true"
        )

    if not _recovery_identity_matches(
        report_identity,
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        claim_generation=claim_generation,
        source=source,
        kind=kind,
        based_on_report=based_on_report,
        outcome="BLOCKED",
    ):
        raise ProtocolConflict("canonical interrupted report identity does not match")
    if report_identity.get("pending_report_sha256") != actual_report_sha256:
        raise ProtocolConflict("canonical report is not bound to the reviewed report digest")
    if not isinstance(report_identity.get("interruption_classification"), str) or not report_identity.get(
        "interruption_classification"
    ):
        raise ProtocolViolation("canonical interrupted report classification is missing")

    status = state.get("status")
    if status == "REPORT_READY":
        if (
            state.get("project_id") == project_id
            and _optional_int_equal(state.get("generation"), expected_generation + 1)
            and _optional_int_equal(state.get("latest_command"), command_id)
            and _optional_int_equal(state.get("latest_report"), command_id)
            and _optional_int_equal(state.get("last_reviewed_report"), based_on_report)
            and state.get("active_run") is None
            and state.get("worker_pid") is None
            and state.get("human_required") is False
            and _recovery_note_matches(
                state.get("recovery_note"),
                command_id=command_id,
                run_id=run_id,
                claim_generation=claim_generation,
            )
            and canonical_report_exists
            and canonical_report_matches
        ):
            return "ALREADY_RESOLVED"
        raise ProtocolConflict(
            "recovery resolution is already superseded or its canonical state/report is inconsistent"
        )

    if status != "RECOVERY_REQUIRED":
        raise ProtocolConflict(
            "resolution already superseded by newer canonical state"
        )
    if state.get("project_id") != project_id:
        raise ProtocolConflict("canonical project identity does not match")
    if not _optional_int_equal(state.get("generation"), expected_generation):
        raise ProtocolConflict("canonical recovery generation does not match")
    if not _optional_int_equal(state.get("latest_command"), command_id):
        raise ProtocolConflict("canonical latest_command does not match")
    if not _optional_int_equal(state.get("latest_report"), based_on_report):
        raise ProtocolConflict("canonical latest_report does not match")
    if not _optional_int_equal(state.get("last_reviewed_report"), based_on_report):
        raise ProtocolConflict("canonical last_reviewed_report does not match")
    if state.get("active_run") is not None:
        raise ProtocolConflict("recovery resolution requires no active run")
    if state.get("worker_pid") is not None:
        raise ProtocolConflict("recovery resolution requires worker_pid=null")
    if state.get("human_required") is not False:
        raise ProtocolConflict("recovery resolution requires human_required=false")
    if state.get("finalized") is not False:
        raise ProtocolConflict("recovery resolution requires finalized=false")
    return "ALLOW"


def parse_final_result(final_message: str) -> dict[str, Any] | None:
    """Parse the existing strict final-delivery marker contract."""

    for line in final_message.splitlines():
        stripped = line.strip()
        if not stripped.startswith(FINAL_RESULT_PREFIX):
            continue
        raw = stripped[len(FINAL_RESULT_PREFIX) :].strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def final_delivery_marker_is_success(final_message: str) -> bool:
    result = parse_final_result(final_message)
    return bool(
        result
        and result.get("final_delivery") == "SUCCESS"
        and result.get("completion_email") == "SENT"
    )


def maybe_final_report_ready(
    previous_status: str,
    outcome: str,
    final_message: str,
) -> bool:
    """Preserve the strict FINALIZING -> FINAL_REPORT_READY decision."""

    return (
        previous_status == "FINALIZING"
        and outcome == "SUCCESS"
        and final_delivery_marker_is_success(final_message)
    )


def report_status_after_execution(
    kind: str,
    outcome: str,
    final_message: str,
) -> str:
    """Return the post-report state for manual or Worker finalization."""

    if kind == "FINALIZE" and outcome == "SUCCESS" and final_delivery_marker_is_success(
        final_message
    ):
        return "FINAL_REPORT_READY"
    return "REPORT_READY"
