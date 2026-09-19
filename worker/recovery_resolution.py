#!/usr/bin/env python3
"""Explicit operator-only resolution for an interrupted Bridge run.

This boundary is intentionally separate from ``pending_report.py``.  The
completed-report reconciler proves that an execution finished and that only
publication was deferred.  Recovery resolution instead records a human's
forensic decision that an interrupted execution is now the canonical report.

The module only loads evidence, validates exact identity/integrity, and
publishes one Protocol-v2 CAS transition.  It never imports or calls the
Worker executor, Codex lifecycle, claim path, or command-generation path.
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
from typing import Any, Mapping

import bridge_common
import git_store
import protocol_core
import recovery_journal


RECOVERY_RESOLUTION_EVENT = "operator.resolve_recovery"
CANONICAL_OUTCOME = "BLOCKED"
PENDING_OUTCOMES = frozenset(
    {"NETWORK_INTERRUPTED", "BLOCKED", "PARTIAL", "FAILED"}
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
BULLET_META_RE = re.compile(r"^\s*-\s+([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")


class RecoveryResolutionError(bridge_common.WorkerError):
    """Recovery evidence is malformed or cannot be safely resolved."""


class RecoveryResolutionConflict(RecoveryResolutionError):
    """The canonical state changed or is already owned by newer history."""


@dataclass(frozen=True)
class ResolutionRequest:
    bridge_root: Path
    project_id: str
    command_id: int
    run_id: str
    claim_generation: int
    expected_generation: int
    source: str
    kind: str


@dataclass(frozen=True)
class ResolutionEvidence:
    request: ResolutionRequest
    state: dict[str, Any]
    journal: dict[str, Any]
    command_meta: dict[str, Any]
    pending_report_path: Path
    pending_report_relative_path: str
    pending_report_text: str
    pending_report_sha256: str
    pending_report_header: dict[str, str]
    canonical_report_path: Path
    canonical_report_text: str | None
    canonical_report_sha256: str | None
    proposed_report_text: str
    proposed_report_sha256: str
    canonical_report_matches: bool
    decision: str
    report_byte_identical_to_pending: bool
    canonical_outcome: str
    interruption_classification: str


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecoveryResolutionError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryResolutionError(f"{label} must be a non-negative integer")
    return value


def _safe_project_id(value: str) -> str:
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value):
        raise RecoveryResolutionError("project must be a safe project id")
    return value


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise RecoveryResolutionError("run-id must be a safe run id")
    return value


def _canonical_report_relative_path(project_id: str, command_id: int) -> str:
    return f"projects/{_safe_project_id(project_id)}/reports/report-{_positive_int(command_id, 'command_id'):03d}.md"


def _canonical_pending_relative_path(project_id: str, command_id: int) -> str:
    return f"worker/runtime/{_safe_project_id(project_id)}/pending-report-{_positive_int(command_id, 'command_id'):03d}.md"


def _safe_relative_path(root: Path, relative: str, *, required_parent: Path | None = None) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RecoveryResolutionError("evidence path must be non-empty")
    root = root.resolve()
    path = (root / Path(relative)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RecoveryResolutionError("evidence path escapes the Bridge root") from exc
    if required_parent is not None:
        try:
            path.relative_to(required_parent.resolve())
        except ValueError as exc:
            raise RecoveryResolutionError("evidence path escapes its allowed directory") from exc
    return path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_utf8_bytes(path: Path, label: str) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
        return raw, raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryResolutionError(f"{label} is missing or not valid UTF-8") from exc


def _parse_bullet_header(text: str, *, stop_at_heading: str | None = None) -> dict[str, str]:
    """Parse the bounded normalized header without trusting duplicate fields."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip().startswith("## "):
            break
        if stop_at_heading is not None and line.strip() == stop_at_heading:
            break
        match = BULLET_META_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key in values:
            raise RecoveryResolutionError(f"report header repeats field {key}")
        values[key] = value.strip().strip("`")
    return values


def _header_int(header: Mapping[str, str], key: str, *, minimum: int = 0) -> int:
    value = header.get(key)
    try:
        parsed = int(value) if value is not None else -1
    except (TypeError, ValueError) as exc:
        raise RecoveryResolutionError(f"report header {key} is not an integer") from exc
    if parsed < minimum:
        raise RecoveryResolutionError(f"report header {key} is invalid")
    return parsed


def _load_state(request: ResolutionRequest) -> dict[str, Any]:
    state_path = request.bridge_root / "projects" / request.project_id / "state.json"
    try:
        state = bridge_common.load_json(state_path)
    except (OSError, bridge_common.WorkerError) as exc:
        raise RecoveryResolutionError("canonical state.json cannot be read") from exc
    return state


def _load_command_meta(request: ResolutionRequest) -> dict[str, Any]:
    command_path = (
        request.bridge_root
        / "projects"
        / request.project_id
        / "commands"
        / f"command-{request.command_id:03d}.md"
    )
    try:
        command_text = command_path.read_text(encoding="utf-8")
        meta = protocol_core.parse_command_metadata(command_text)
        protocol_core.validate_command_metadata(meta)
    except (OSError, UnicodeDecodeError, protocol_core.ProtocolViolation) as exc:
        raise RecoveryResolutionError("canonical command metadata cannot be validated") from exc
    if int(meta["command_id"]) != request.command_id:
        raise RecoveryResolutionError("canonical command id does not match the request")
    if meta["source"] != request.source or meta["kind"] != request.kind:
        raise RecoveryResolutionConflict("canonical command source/kind does not match the request")
    return dict(meta)


def _load_journal(request: ResolutionRequest) -> tuple[dict[str, Any], Path]:
    path = recovery_journal.journal_path(
        request.bridge_root,
        request.project_id,
        request.run_id,
    )
    try:
        journal = recovery_journal.read_journal(path)
    except recovery_journal.RecoveryJournalError as exc:
        raise RecoveryResolutionError("recovery journal cannot be validated") from exc
    expected_pending = _canonical_pending_relative_path(
        request.project_id,
        request.command_id,
    )
    expected_report = _canonical_report_relative_path(
        request.project_id,
        request.command_id,
    )
    if journal.get("project_id") != request.project_id:
        raise RecoveryResolutionConflict("recovery journal project identity does not match")
    if journal.get("command_id") != request.command_id:
        raise RecoveryResolutionConflict("recovery journal command identity does not match")
    if journal.get("run_id") != request.run_id:
        raise RecoveryResolutionConflict("recovery journal run identity does not match")
    if journal.get("claim_generation") != request.claim_generation:
        raise RecoveryResolutionConflict("recovery journal claim generation does not match")
    if journal.get("journal_status") != "reconciled":
        raise RecoveryResolutionConflict("recovery journal is not reconciled")
    if journal.get("remote_publish_pending") is not False:
        raise RecoveryResolutionConflict("recovery journal still has remote publication pending")
    if journal.get("pending_report_path") != expected_pending:
        raise RecoveryResolutionConflict("recovery journal pending report path is not canonical")
    if journal.get("report_path") != expected_report:
        raise RecoveryResolutionConflict("recovery journal report path is not canonical")
    if not journal.get("interruption_kind") or not journal.get("interruption_reason_safe"):
        raise RecoveryResolutionError("recovery journal interruption evidence is incomplete")
    if journal.get("external_side_effects_unknown") is not True:
        raise RecoveryResolutionConflict(
            "recovery journal must preserve external_side_effects_unknown=true"
        )
    return journal, path


def _validate_pending_report(
    request: ResolutionRequest,
    *,
    journal: Mapping[str, Any],
    command_meta: Mapping[str, Any],
) -> tuple[Path, str, str, dict[str, str]]:
    pending_relative = str(journal["pending_report_path"])
    runtime_root = (request.bridge_root / "worker" / "runtime").resolve()
    pending_path = _safe_relative_path(
        request.bridge_root,
        pending_relative,
        required_parent=runtime_root,
    )
    expected_path = (
        request.bridge_root
        / "worker"
        / "runtime"
        / request.project_id
        / f"pending-report-{request.command_id:03d}.md"
    ).resolve()
    if pending_path != expected_path:
        raise RecoveryResolutionConflict("pending report path does not match exact resolution identity")
    raw, text = _read_utf8_bytes(pending_path, "pending report")
    digest = _sha256_bytes(raw)
    header = _parse_bullet_header(text)
    required = {"command_id", "outcome", "source", "based_on_report", "run_id", "claim_generation"}
    missing = sorted(required - set(header))
    if missing:
        raise RecoveryResolutionError(
            "pending report normalized header is missing: " + ", ".join(missing)
        )
    if _header_int(header, "command_id", minimum=1) != request.command_id:
        raise RecoveryResolutionConflict("pending report command identity does not match")
    if header["source"] != request.source:
        raise RecoveryResolutionConflict("pending report source identity does not match")
    if _header_int(header, "based_on_report", minimum=0) != int(command_meta["based_on_report"]):
        raise RecoveryResolutionConflict("pending report based_on_report does not match the command")
    if header["run_id"] != request.run_id:
        raise RecoveryResolutionConflict("pending report run identity does not match")
    if _header_int(header, "claim_generation", minimum=0) != request.claim_generation:
        raise RecoveryResolutionConflict("pending report claim generation does not match")
    if "kind" in header and header["kind"] != request.kind:
        raise RecoveryResolutionConflict("pending report kind identity does not match")
    outcome = header["outcome"]
    if outcome not in PENDING_OUTCOMES:
        raise RecoveryResolutionError(f"unsupported interrupted pending outcome: {outcome!r}")
    if outcome == "SUCCESS" or re.search(r"(?im)^\s*-\s*outcome:\s*SUCCESS\s*$", text):
        raise RecoveryResolutionConflict("interrupted recovery evidence cannot have outcome=SUCCESS")
    if re.search(r'(?i)BRIDGE_EXECUTION_JSON:\s*\{[^\n]*"status"\s*:\s*"SUCCESS"', text):
        raise RecoveryResolutionConflict("interrupted recovery evidence contains a successful final marker")
    marker_status = str(journal.get("marker_status", "")).upper()
    if marker_status in {"SUCCESS", "VALID"}:
        raise RecoveryResolutionConflict("recovery journal records a successful final marker")
    return pending_path, text, digest, header


def _interruption_classification(
    pending_header: Mapping[str, str],
    journal: Mapping[str, Any],
) -> str:
    raw_outcome = str(pending_header.get("outcome", "")).strip()
    if raw_outcome == "NETWORK_INTERRUPTED":
        return raw_outcome
    kind = str(journal.get("interruption_kind", "")).strip()
    if kind:
        return kind.upper()
    return "INTERRUPTED_EXECUTION"


def _build_proposed_report(
    request: ResolutionRequest,
    *,
    command_meta: Mapping[str, Any],
    journal: Mapping[str, Any],
    pending_report_text: str,
    pending_report_sha256: str,
    interruption_classification: str,
) -> str:
    """Build a deterministic canonical wrapper without changing evidence."""

    # The legacy pending artifact is read as raw UTF-8 so its reviewed digest
    # remains a digest of the exact bytes on disk.  Normalize only the copy
    # embedded in the canonical Markdown payload; otherwise Windows text-mode
    # writes can turn mixed CRLF/LF input into a report that fails its own
    # post-publication integrity check.
    normalized_pending_report_text = pending_report_text.replace("\r\n", "\n").replace("\r", "\n")
    pending_header = _parse_bullet_header(normalized_pending_report_text)
    canonical_markers = {
        "- recovery_resolution: interrupted_report_published",
        "- no_final_success_marker: true",
        "- external_side_effects_unknown: true",
    }
    if (
        pending_header.get("outcome") == CANONICAL_OUTCOME
        and pending_header.get("kind") == request.kind
        and all(marker in pending_report_text for marker in canonical_markers)
        and f"- pending_report_sha256: {pending_report_sha256}" in pending_report_text
    ):
        return normalized_pending_report_text

    header = [
        f"# Report {request.command_id:03d} — {request.project_id}",
        "",
        f"- command_id: {request.command_id}",
        f"- outcome: {CANONICAL_OUTCOME}",
        f"- source: {request.source}",
        f"- kind: {request.kind}",
        f"- based_on_report: {int(command_meta['based_on_report'])}",
        f"- run_id: `{request.run_id}`",
        f"- claim_generation: {request.claim_generation}",
        f"- recovery_resolution: {protocol_core.RECOVERY_RESOLUTION}",
        f"- interruption_classification: {interruption_classification}",
        f"- interruption_kind: {journal['interruption_kind']}",
        "- no_final_success_marker: true",
        "- external_side_effects_unknown: true",
        f"- pending_report_sha256: {pending_report_sha256}",
        "",
        "## Recovery Resolution",
        "",
        (
            "This report canonically records the already interrupted execution "
            "after human forensic review. Recovery Resolution never reruns the "
            "interrupted command and does not assert product-task success."
        ),
        "",
        f"- interruption_reason_safe: {journal['interruption_reason_safe']}",
        "- canonical_interrupted_outcome: BLOCKED",
        "- automatic_rerun: prohibited",
        "",
        "## Preserved Interrupted Pending Report",
        "",
        f"<!-- pending-report-sha256: {pending_report_sha256} -->",
    ]
    prefix = "\n".join(header) + "\n"
    return prefix + normalized_pending_report_text


def _canonical_report_matches(path: Path, proposed_text: str) -> tuple[bool, str | None, str | None]:
    if not path.exists():
        return False, None, None
    raw, text = _read_utf8_bytes(path, "canonical report")
    digest = _sha256_bytes(raw)
    # The existing Windows atomic text helper may materialize Git payloads as
    # CRLF while the in-memory payload uses LF.  Line-ending representation is
    # not report evidence; compare the decoded content canonically while still
    # returning the exact on-disk SHA-256 for observability.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    expected = proposed_text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized == expected, digest, text


def _report_identity(
    request: ResolutionRequest,
    *,
    command_meta: Mapping[str, Any],
    pending_report_sha256: str,
    interruption_classification: str,
) -> dict[str, Any]:
    return {
        "project_id": request.project_id,
        "command_id": request.command_id,
        "run_id": request.run_id,
        "claim_generation": request.claim_generation,
        "source": request.source,
        "kind": request.kind,
        "based_on_report": int(command_meta["based_on_report"]),
        "outcome": CANONICAL_OUTCOME,
        "interrupted": True,
        "no_final_success_marker": True,
        "external_side_effects_unknown": True,
        "pending_report_sha256": pending_report_sha256,
        "interruption_classification": interruption_classification,
    }


def _decision(
    evidence: ResolutionEvidence,
    state: Mapping[str, Any],
    *,
    canonical_report_exists: bool,
    canonical_report_matches: bool,
) -> str:
    request = evidence.request
    try:
        return protocol_core.validate_recovery_resolution(
            state=state,
            journal=evidence.journal,
            report_identity=_report_identity(
                request,
                command_meta=evidence.command_meta,
                pending_report_sha256=evidence.pending_report_sha256,
                interruption_classification=evidence.interruption_classification,
            ),
            project_id=request.project_id,
            command_id=request.command_id,
            run_id=request.run_id,
            claim_generation=request.claim_generation,
            expected_generation=request.expected_generation,
            source=request.source,
            kind=request.kind,
            based_on_report=int(evidence.command_meta["based_on_report"]),
            pending_report_path=evidence.pending_report_relative_path,
            expected_report_sha256=evidence.pending_report_sha256,
            actual_report_sha256=evidence.pending_report_sha256,
            canonical_report_exists=canonical_report_exists,
            canonical_report_matches=canonical_report_matches,
        )
    except (protocol_core.ProtocolViolation, protocol_core.ProtocolConflict) as exc:
        raise RecoveryResolutionConflict(str(exc)) from exc


def inspect_resolution(request: ResolutionRequest) -> ResolutionEvidence:
    """Read and validate a resolution snapshot without canonical writes."""

    request = ResolutionRequest(
        bridge_root=Path(request.bridge_root).resolve(),
        project_id=_safe_project_id(request.project_id),
        command_id=_positive_int(request.command_id, "command_id"),
        run_id=_safe_run_id(request.run_id),
        claim_generation=_nonnegative_int(request.claim_generation, "claim_generation"),
        expected_generation=_nonnegative_int(request.expected_generation, "expected_generation"),
        source=request.source,
        kind=request.kind,
    )
    command_meta = _load_command_meta(request)
    journal, _ = _load_journal(request)
    state = _load_state(request)
    pending_path, pending_text, pending_digest, pending_header = _validate_pending_report(
        request,
        journal=journal,
        command_meta=command_meta,
    )
    interruption_classification = _interruption_classification(pending_header, journal)
    proposed_text = _build_proposed_report(
        request,
        command_meta=command_meta,
        journal=journal,
        pending_report_text=pending_text,
        pending_report_sha256=pending_digest,
        interruption_classification=interruption_classification,
    )
    proposed_digest = _sha256_bytes(proposed_text.encode("utf-8"))
    canonical_path = (
        request.bridge_root
        / "projects"
        / request.project_id
        / "reports"
        / f"report-{request.command_id:03d}.md"
    ).resolve()
    canonical_exists = canonical_path.exists()
    canonical_matches, canonical_digest, canonical_text = _canonical_report_matches(
        canonical_path,
        proposed_text,
    )
    pending_relative = str(journal["pending_report_path"])
    placeholder = ResolutionEvidence(
        request=request,
        state=state,
        journal=journal,
        command_meta=command_meta,
        pending_report_path=pending_path,
        pending_report_relative_path=pending_relative,
        pending_report_text=pending_text,
        pending_report_sha256=pending_digest,
        pending_report_header=pending_header,
        canonical_report_path=canonical_path,
        canonical_report_text=canonical_text,
        canonical_report_sha256=canonical_digest,
        proposed_report_text=proposed_text,
        proposed_report_sha256=proposed_digest,
        canonical_report_matches=canonical_matches,
        decision="",
        report_byte_identical_to_pending=proposed_text == pending_text,
        canonical_outcome=CANONICAL_OUTCOME,
        interruption_classification=interruption_classification,
    )
    decision = _decision(
        placeholder,
        state,
        canonical_report_exists=canonical_exists,
        canonical_report_matches=canonical_matches,
    )
    return ResolutionEvidence(**{**placeholder.__dict__, "decision": decision})


def _recovery_note(request: ResolutionRequest, resolved_at: str) -> dict[str, Any]:
    return {
        "resolved_command_id": request.command_id,
        "run_id": request.run_id,
        "claim_generation": request.claim_generation,
        "resolved_at": resolved_at,
        "resolution": protocol_core.RECOVERY_RESOLUTION,
    }


def _state_payload(
    current: Mapping[str, Any],
    request: ResolutionRequest,
    *,
    resolved_at: str,
) -> dict[str, Any]:
    updated = dict(current)
    updated["status"] = "REPORT_READY"
    updated["generation"] = protocol_core.recovery_generation(int(current["generation"]))
    updated["latest_report"] = request.command_id
    updated["active_run"] = None
    updated["worker_pid"] = None
    updated["human_required"] = False
    updated.pop("recovery_reason", None)
    updated.pop("last_execution_error", None)
    updated["recovery_note"] = _recovery_note(request, resolved_at)
    updated["updated_at"] = resolved_at
    return updated


def resolve_resolution(
    request: ResolutionRequest,
    *,
    expected_report_sha256: str,
) -> dict[str, Any]:
    """Publish the reviewed interrupted report and the CAS state transition."""

    expected_digest = str(expected_report_sha256).lower()
    if not SHA256_RE.fullmatch(expected_digest):
        raise RecoveryResolutionError("expected-report-sha256 must be 64 lowercase hexadecimal characters")
    evidence = inspect_resolution(request)
    if evidence.pending_report_sha256 != expected_digest:
        raise RecoveryResolutionConflict(
            "expected-report-sha256 does not match the freshly computed pending report digest"
        )
    if evidence.decision == "ALREADY_RESOLVED":
        return {
            "result": "ALREADY_RESOLVED",
            "event": RECOVERY_RESOLUTION_EVENT,
            "project_id": request.project_id,
            "command_id": request.command_id,
            "run_id": request.run_id,
            "claim_generation": request.claim_generation,
            "generation": evidence.state.get("generation"),
            "latest_report": evidence.state.get("latest_report"),
            "pending_report_sha256": evidence.pending_report_sha256,
            "canonical_report_sha256": evidence.canonical_report_sha256,
            "canonical_report_byte_identical_to_pending": evidence.report_byte_identical_to_pending,
            "outcome": evidence.canonical_outcome,
        }

    resolved_at = bridge_common.now_iso()
    state_path = request.bridge_root / "projects" / request.project_id / "state.json"
    canonical_path = evidence.canonical_report_path

    def snapshot_decision(current: Mapping[str, Any]) -> str:
        exists = canonical_path.exists()
        matches, _, _ = _canonical_report_matches(canonical_path, evidence.proposed_report_text)
        try:
            return _decision(
                evidence,
                current,
                canonical_report_exists=exists,
                canonical_report_matches=matches,
            )
        except RecoveryResolutionConflict:
            raise

    def expected(current: dict[str, Any]) -> bool:
        return snapshot_decision(current) == "ALLOW"

    def already_applied(current: dict[str, Any]) -> bool:
        return snapshot_decision(current) == "ALREADY_RESOLVED"

    def payload_builder(current: dict[str, Any]) -> dict[Path, str]:
        updated = _state_payload(current, request, resolved_at=resolved_at)
        return {
            canonical_path: evidence.proposed_report_text,
            state_path: bridge_common.json_text(updated),
        }

    try:
        final_state = git_store.publish_cas(
            bridge_root=request.bridge_root,
            state_path=state_path,
            expected=expected,
            already_applied=already_applied,
            payload_builder=payload_builder,
            message=(
                f"bridge: resolve interrupted {request.project_id} "
                f"run {request.run_id}"
            ),
        )
    except (git_store.CASConflict, bridge_common.WorkerError) as exc:
        raise RecoveryResolutionConflict(
            "recovery resolution CAS publication failed safely"
        ) from exc

    final_canonical_matches, final_canonical_digest, _ = _canonical_report_matches(
        canonical_path,
        evidence.proposed_report_text,
    )
    if not final_canonical_matches:
        raise RecoveryResolutionError("resolution returned without the expected canonical report")
    return {
        "result": "RESOLVED",
        "event": RECOVERY_RESOLUTION_EVENT,
        "project_id": request.project_id,
        "command_id": request.command_id,
        "run_id": request.run_id,
        "claim_generation": request.claim_generation,
        "generation_before": request.expected_generation,
        "generation_after": final_state.get("generation"),
        "latest_command": final_state.get("latest_command"),
        "latest_report": final_state.get("latest_report"),
        "status": final_state.get("status"),
        "pending_report_sha256": evidence.pending_report_sha256,
        "canonical_report_sha256": final_canonical_digest,
        "canonical_report_byte_identical_to_pending": evidence.report_byte_identical_to_pending,
        "outcome": evidence.canonical_outcome,
    }


def _request_from_namespace(args: argparse.Namespace) -> ResolutionRequest:
    return ResolutionRequest(
        bridge_root=Path(args.bridge_root),
        project_id=args.project,
        command_id=args.command_id,
        run_id=args.run_id,
        claim_generation=args.claim_generation,
        expected_generation=args.expected_generation,
        source=args.source,
        kind=args.kind,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bridge-root", default=None)
    parser.add_argument("--project", required=True)
    parser.add_argument("--command-id", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--claim-generation", type=int, required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--source", choices=sorted(protocol_core.COMMAND_SOURCES), required=True)
    parser.add_argument("--kind", choices=sorted(protocol_core.COMMAND_KINDS), required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicit human Recovery Resolution; never executes or reruns Codex."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="read and print a validated recovery snapshot")
    _add_common_arguments(inspect_parser)
    resolve_parser = subparsers.add_parser("resolve", help="publish the reviewed interrupted report")
    _add_common_arguments(resolve_parser)
    resolve_parser.add_argument("--expected-report-sha256", required=True)
    return parser


def _inspection_json(evidence: ResolutionEvidence) -> dict[str, Any]:
    state = evidence.state
    journal = evidence.journal
    return {
        "event": RECOVERY_RESOLUTION_EVENT,
        "operation": "inspect",
        "project_id": evidence.request.project_id,
        "command_id": evidence.request.command_id,
        "run_id": evidence.request.run_id,
        "claim_generation": evidence.request.claim_generation,
        "expected_generation": evidence.request.expected_generation,
        "canonical_state": {
            "status": state.get("status"),
            "generation": state.get("generation"),
            "latest_command": state.get("latest_command"),
            "latest_report": state.get("latest_report"),
            "last_reviewed_report": state.get("last_reviewed_report"),
            "active_run": state.get("active_run"),
            "worker_pid": state.get("worker_pid"),
        },
        "journal": {
            "journal_status": journal.get("journal_status"),
            "remote_publish_pending": journal.get("remote_publish_pending"),
            "interruption_kind": journal.get("interruption_kind"),
            "interruption_reason_safe": journal.get("interruption_reason_safe"),
            "pending_report_path": journal.get("pending_report_path"),
            "report_path": journal.get("report_path"),
            "external_side_effects_unknown": journal.get("external_side_effects_unknown"),
            "marker_status": journal.get("marker_status"),
        },
        "pending_report_path": evidence.pending_report_relative_path,
        "pending_report_sha256": evidence.pending_report_sha256,
        "pending_report_identity": {
            "command_id": _header_int(evidence.pending_report_header, "command_id", minimum=1),
            "outcome": evidence.pending_report_header.get("outcome"),
            "source": evidence.pending_report_header.get("source"),
            "based_on_report": _header_int(evidence.pending_report_header, "based_on_report", minimum=0),
            "run_id": evidence.pending_report_header.get("run_id"),
            "claim_generation": _header_int(evidence.pending_report_header, "claim_generation", minimum=0),
        },
        "canonical_report_path": _canonical_report_relative_path(
            evidence.request.project_id,
            evidence.request.command_id,
        ),
        "canonical_report_exists": evidence.canonical_report_text is not None,
        "canonical_report_sha256": evidence.canonical_report_sha256,
        "canonical_report_byte_identical_to_pending": evidence.report_byte_identical_to_pending,
        "proposed_canonical_report_sha256": evidence.proposed_report_sha256,
        "proposed_outcome": evidence.canonical_outcome,
        "interruption_classification": evidence.interruption_classification,
        "proposed_transition": {
            "event": RECOVERY_RESOLUTION_EVENT,
            "from": "RECOVERY_REQUIRED",
            "to": "REPORT_READY",
            "generation": f"{evidence.request.expected_generation} -> {evidence.request.expected_generation + 1}",
            "latest_report": f"{evidence.request.command_id - 1} -> {evidence.request.command_id}",
            "automatic_rerun": "prohibited",
        },
        "decision": evidence.decision,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.bridge_root = state_roots.resolve_state_root(args.bridge_root, for_write=args.operation != "inspect")
    try:
        request = _request_from_namespace(args)
        if args.operation == "inspect":
            evidence = inspect_resolution(request)
            print("RECOVERY_RESOLUTION_INSPECT_JSON:")
            print(json.dumps(_inspection_json(evidence), ensure_ascii=False, indent=2))
            return 0
        result = resolve_resolution(
            request,
            expected_report_sha256=args.expected_report_sha256,
        )
        print("RECOVERY_RESOLUTION_JSON:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RecoveryResolutionError, protocol_core.ProtocolViolation, protocol_core.ProtocolConflict) as exc:
        print(f"RECOVERY_RESOLUTION_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
