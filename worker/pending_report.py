"""Durable reconciliation for completed unattended Worker reports.

This module is deliberately limited to publication recovery.  It never
claims a lease, starts Codex, reads command input, or decides to execute a
new command.  A pending report is trusted only when its metadata, path,
identity, and SHA-256 integrity all agree with the canonical Protocol-v2
state that is still owned by the original execution lease.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import bridge_common
import git_store
import protocol_core


SCHEMA_VERSION = 1
JOURNAL_STATUSES = frozenset({"pending", "reconciled", "superseded", "conflict"})
OUTCOMES = frozenset({"SUCCESS", "FAILED", "BLOCKED"})
TARGET_STATUSES = frozenset({"REPORT_READY", "FINAL_REPORT_READY", "RECOVERY_REQUIRED"})
META_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "command_id",
        "run_id",
        "claim_generation",
        "source",
        "kind",
        "previous_status",
        "outcome",
        "target_status",
        "report_path",
        "pending_report_path",
        "report_sha256",
        "created_at",
        "last_execution_error",
        "remote_publish_pending",
        "journal_status",
        "reconciled_at",
        "reconciliation_reason",
        "evidence_mode",
    }
)
REQUIRED_FIELDS = META_FIELDS - {
    "reconciled_at",
    "reconciliation_reason",
    # Schema-v1 sidecars are legacy evidence.
    "evidence_mode",
}
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[^\x00\r\n]{1,80}$")
SAFE_ERROR_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s]+"),
    re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s]+"),
    re.compile(r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[-A-Za-z0-9_]{8,}\b"),
)


class PendingReportError(ValueError):
    """Pending evidence is malformed, stale, or not safely publishable."""


@dataclass(frozen=True)
class PendingReportEvidence:
    bridge_root: Path
    metadata_path: Path
    report_path: Path
    pending_report_path: Path
    state_path: Path
    metadata: dict[str, Any]
    report_text: str


PublishCAS = Callable[..., dict[str, Any]]


def _safe_project_id(value: Any) -> str:
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value):
        raise PendingReportError("pending report project_id is invalid")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PendingReportError(f"pending report {label} is invalid")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PendingReportError(f"pending report {label} is invalid")
    return value


def _safe_timestamp(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise PendingReportError(f"pending report {label} is invalid")
    return value


def safe_execution_error(value: Any) -> str | None:
    """Bound and redact the small error snapshot persisted in metadata/state."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise PendingReportError("last_execution_error must be a string or null")
    text = value.replace("\x00", " ")
    for pattern in SAFE_ERROR_PATTERNS:
        text = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex
                else "[REDACTED_TOKEN]"
            ),
            text,
        )
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 1000:
        text = text[:1000]
    return text or None


def canonical_report_relative_path(project_id: str, command_id: int) -> str:
    project = _safe_project_id(project_id)
    command = _positive_int(command_id, "command_id")
    return f"projects/{project}/reports/report-{command:03d}.md"


def canonical_pending_relative_path(project_id: str, command_id: int) -> str:
    project = _safe_project_id(project_id)
    command = _positive_int(command_id, "command_id")
    return f"worker/runtime/{project}/pending-report-{command:03d}.md"


def canonical_metadata_path(bridge_root: Path, project_id: str, command_id: int) -> Path:
    project = _safe_project_id(project_id)
    command = _positive_int(command_id, "command_id")
    return (
        bridge_root
        / "worker"
        / "runtime"
        / project
        / f"pending-report-{command:03d}.meta.json"
    )


def canonical_wal_path(bridge_root: Path, project_id: str, run_id: str) -> Path:
    """Return the exact run WAL path under the protected runtime boundary."""

    project = _safe_project_id(project_id)
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise PendingReportError("pending report run_id is invalid")
    return (
        bridge_root
        / "worker"
        / "runtime"
        / project
        / "runs"
        / run_id
        / "recovery.wal"
    )


def validate_metadata(
    data: dict[str, Any],
    *,
    expected_project_id: str | None = None,
    expected_command_id: int | None = None,
) -> dict[str, Any]:
    """Validate and normalize the allowlisted machine-readable sidecar."""

    if not isinstance(data, dict):
        raise PendingReportError("pending report metadata must be an object")
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        raise PendingReportError("pending report metadata missing: " + ", ".join(missing))
    unknown = sorted(set(data) - META_FIELDS)
    if unknown:
        raise PendingReportError(
            "pending report metadata contains unsupported fields: " + ", ".join(unknown)
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise PendingReportError("unsupported pending report metadata schema_version")

    project_id = _safe_project_id(data.get("project_id"))
    command_id = _positive_int(data.get("command_id"), "command_id")
    if expected_project_id is not None and project_id != expected_project_id:
        raise PendingReportError("pending report project identity does not match its path")
    if expected_command_id is not None and command_id != expected_command_id:
        raise PendingReportError("pending report command identity does not match its path")

    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise PendingReportError("pending report run_id is invalid")
    claim_generation = _nonnegative_int(data.get("claim_generation"), "claim_generation")
    source = data.get("source")
    if not isinstance(source, str) or source not in protocol_core.COMMAND_SOURCES:
        raise PendingReportError("pending report source is invalid")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in protocol_core.COMMAND_KINDS:
        raise PendingReportError("pending report kind is invalid")
    previous_status = data.get("previous_status")
    if not isinstance(previous_status, str) or previous_status not in protocol_core.ALLOWED_STATES:
        raise PendingReportError("pending report previous_status is invalid")
    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise PendingReportError("pending report outcome is invalid")
    target_status = data.get("target_status")
    if not isinstance(target_status, str) or target_status not in TARGET_STATUSES:
        raise PendingReportError("pending report target_status is invalid")
    if target_status == "FINAL_REPORT_READY" and not (
        kind == "FINALIZE" and outcome == "SUCCESS" and previous_status == "FINALIZING"
    ):
        raise PendingReportError("FINAL_REPORT_READY target does not match finalization identity")

    report_path = data.get("report_path")
    pending_path = data.get("pending_report_path")
    if report_path != canonical_report_relative_path(project_id, command_id):
        raise PendingReportError("pending report target path is not canonical")
    if pending_path != canonical_pending_relative_path(project_id, command_id):
        raise PendingReportError("pending report evidence path is not canonical")
    report_sha256 = data.get("report_sha256")
    if not isinstance(report_sha256, str) or not SHA256_RE.fullmatch(report_sha256):
        raise PendingReportError("pending report SHA-256 is invalid")
    created_at = _safe_timestamp(data.get("created_at"), "created_at")
    reconciled_at = _safe_timestamp(
        data.get("reconciled_at"), "reconciled_at", nullable=True
    )
    last_execution_error = safe_execution_error(data.get("last_execution_error"))
    remote_publish_pending = data.get("remote_publish_pending")
    if not isinstance(remote_publish_pending, bool):
        raise PendingReportError("remote_publish_pending must be boolean")
    journal_status = data.get("journal_status")
    if journal_status not in JOURNAL_STATUSES:
        raise PendingReportError("pending report journal_status is invalid")
    if journal_status == "pending" and not remote_publish_pending:
        raise PendingReportError("pending journal must retain remote_publish_pending")
    if journal_status != "pending" and remote_publish_pending:
        raise PendingReportError("reconciled/conflict journal cannot remain publish-pending")
    reconciliation_reason = data.get("reconciliation_reason")
    if reconciliation_reason is not None:
        reconciliation_reason = safe_execution_error(reconciliation_reason)
    evidence_mode = data.get("evidence_mode", "legacy")
    if not isinstance(evidence_mode, str) or evidence_mode not in {"legacy", "typed"}:
        raise PendingReportError("pending report evidence_mode is invalid")

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "command_id": command_id,
        "run_id": run_id,
        "claim_generation": claim_generation,
        "source": source,
        "kind": kind,
        "previous_status": previous_status,
        "outcome": outcome,
        "target_status": target_status,
        "report_path": report_path,
        "pending_report_path": pending_path,
        "report_sha256": report_sha256,
        "created_at": created_at,
        "last_execution_error": last_execution_error,
        "remote_publish_pending": remote_publish_pending,
        "journal_status": journal_status,
        "reconciled_at": reconciled_at,
        "reconciliation_reason": reconciliation_reason,
        "evidence_mode": evidence_mode,
    }


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_runtime_path(bridge_root: Path, relative: str) -> Path:
    root = (bridge_root / "worker" / "runtime").resolve()
    path = (bridge_root / Path(relative)).resolve()
    if not _within(path, root):
        raise PendingReportError("pending report path escapes worker runtime")
    return path


def _safe_canonical_path(bridge_root: Path, relative: str) -> Path:
    root = bridge_root.resolve()
    path = (bridge_root / Path(relative)).resolve()
    if not _within(path, root):
        raise PendingReportError("pending report canonical path escapes bridge root")
    return path


def load_evidence(bridge_root: Path, metadata_path: Path) -> PendingReportEvidence:
    """Load metadata and require the pending report's exact integrity contract."""

    bridge_root = bridge_root.resolve()
    runtime_root = (bridge_root / "worker" / "runtime").resolve()
    metadata_path = metadata_path.resolve()
    if not _within(metadata_path, runtime_root) or not metadata_path.name.endswith(
        ".meta.json"
    ):
        raise PendingReportError("pending metadata path is outside the allowed runtime directory")
    match = re.fullmatch(r"pending-report-(\d+)\.meta\.json", metadata_path.name)
    if not match:
        raise PendingReportError("pending metadata filename is invalid")
    expected_project_id = metadata_path.parent.name
    command_id = int(match.group(1))
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingReportError("pending metadata cannot be read") from exc
    metadata = validate_metadata(
        raw,
        expected_project_id=expected_project_id,
        expected_command_id=command_id,
    )
    if metadata_path != canonical_metadata_path(bridge_root, expected_project_id, command_id).resolve():
        raise PendingReportError("pending metadata path is not canonical")

    report_path = _safe_canonical_path(bridge_root, metadata["report_path"])
    pending_path = _safe_runtime_path(bridge_root, metadata["pending_report_path"])
    state_path = bridge_root / "projects" / expected_project_id / "state.json"
    if not _within(state_path.resolve(), bridge_root.resolve()):
        raise PendingReportError("pending state path escapes bridge root")
    try:
        report_text = pending_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PendingReportError("pending report file is missing or unreadable") from exc
    report_sha256 = hashlib.sha256(report_text.encode("utf-8")).hexdigest()
    if report_sha256 != metadata["report_sha256"]:
        raise PendingReportError("pending report SHA-256 does not match metadata")
    return PendingReportEvidence(
        bridge_root=bridge_root,
        metadata_path=metadata_path,
        report_path=report_path,
        pending_report_path=pending_path,
        state_path=state_path,
        metadata=metadata,
        report_text=report_text,
    )


def save_worker_pending_report(
    *,
    bridge_root: Path,
    project_id: str,
    command_id: int,
    run_id: str,
    claim_generation: int,
    source: str,
    kind: str,
    previous_status: str,
    outcome: str,
    target_status: str,
    report_text: str,
    reason: str,
    last_execution_error: str | None,
    evidence_mode: str = "legacy",
) -> Path:
    """Persist human evidence and the machine-readable no-auto-rerun sidecar."""

    if not isinstance(report_text, str):
        raise PendingReportError("pending report text must be a string")
    pending_path = bridge_root / Path(
        canonical_pending_relative_path(project_id, command_id)
    )
    metadata_path = canonical_metadata_path(bridge_root, project_id, command_id)
    metadata = validate_metadata(
        {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "command_id": command_id,
            "run_id": run_id,
            "claim_generation": claim_generation,
            "source": source,
            "kind": kind,
            "previous_status": previous_status,
            "outcome": outcome,
            "target_status": target_status,
            "report_path": canonical_report_relative_path(project_id, command_id),
            "pending_report_path": canonical_pending_relative_path(project_id, command_id),
            "report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
            "created_at": bridge_common.now_iso(),
            "last_execution_error": safe_execution_error(last_execution_error),
            "remote_publish_pending": True,
            "journal_status": "pending",
            "reconciled_at": None,
            "reconciliation_reason": None,
            "evidence_mode": evidence_mode,
        }
    )
    bridge_common.write_text_atomic(pending_path, report_text)
    bridge_common.write_text_atomic(
        pending_path.with_name(pending_path.stem + ".reason.txt"),
        reason.rstrip() + "\n",
    )
    bridge_common.write_text_atomic(
        metadata_path,
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )
    return metadata_path


def _write_status(
    evidence: PendingReportEvidence,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    if status not in JOURNAL_STATUSES or status == "pending":
        raise PendingReportError("invalid pending report status transition")
    data = validate_metadata(json.loads(evidence.metadata_path.read_text(encoding="utf-8")))
    data["remote_publish_pending"] = False
    data["journal_status"] = status
    data["reconciled_at"] = bridge_common.now_iso()
    data["reconciliation_reason"] = safe_execution_error(reason)
    data = validate_metadata(data)
    bridge_common.write_text_atomic(
        evidence.metadata_path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )
    return data


def _mark_conflict_best_effort(metadata_path: Path, reason: str) -> None:
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        data = validate_metadata(raw, expected_project_id=metadata_path.parent.name)
        data["remote_publish_pending"] = False
        data["journal_status"] = "conflict"
        data["reconciled_at"] = bridge_common.now_iso()
        data["reconciliation_reason"] = safe_execution_error(reason)
        data = validate_metadata(data)
        bridge_common.write_text_atomic(
            metadata_path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, json.JSONDecodeError, PendingReportError):
        # A malformed sidecar cannot safely be rewritten.  Its existence and
        # the bounded diagnostic below remain durable evidence for inspection.
        return None


def mark_evidence_status(
    evidence: PendingReportEvidence,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    """A bounded acknowledgement for a coordinator-classified sidecar."""

    return _write_status(evidence, status=status, reason=reason)


def mark_evidence_conflict_best_effort(metadata_path: Path, reason: str) -> None:
    """Retain malformed/contradictory evidence without deleting it."""

    _mark_conflict_best_effort(metadata_path, reason)


def _canonical_report_matches(evidence: PendingReportEvidence) -> bool:
    try:
        text = evidence.report_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return hashlib.sha256(text.encode("utf-8")).hexdigest() == evidence.metadata[
        "report_sha256"
    ]


def _state_int(state: dict[str, Any], key: str, default: int = -1) -> int:
    value = state.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _reconcile_one(
    evidence: PendingReportEvidence,
    *,
    publish_cas: PublishCAS | None = None,
) -> str:
    publisher = publish_cas or git_store.publish_cas
    metadata = evidence.metadata
    if metadata["journal_status"] != "pending" or not metadata["remote_publish_pending"]:
        return "ignored"
    try:
        state = bridge_common.load_json(evidence.state_path)
    except (OSError, bridge_common.WorkerError) as exc:
        print(
            f"[{bridge_common.now_iso()}] pending report state unavailable for "
            f"{metadata['project_id']}#{metadata['command_id']:03d}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return "pending"

    command_id = metadata["command_id"]
    run_matches = protocol_core.matches_execution_lease(
        state,
        run_id=metadata["run_id"],
        command_id=command_id,
        claimed_generation=metadata["claim_generation"],
        source=metadata["source"],
        project_id=metadata["project_id"],
    )
    latest_report = _state_int(state, "latest_report")
    report_exists = evidence.report_path.exists()
    if report_exists or latest_report >= command_id:
        if report_exists and latest_report >= command_id and _canonical_report_matches(evidence):
            if not run_matches:
                _write_status(
                    evidence,
                    status="superseded",
                    reason="canonical report and latest_report already contain this exact pending result",
                )
                return "superseded"
            _mark_conflict_best_effort(
                evidence.metadata_path,
                "canonical report advanced while the original execution lease still matched",
            )
            return "conflict"
        _mark_conflict_best_effort(
            evidence.metadata_path,
            "canonical report/state is inconsistent with pending report integrity",
        )
        return "conflict"

    if latest_report != command_id - 1 or not run_matches:
        _mark_conflict_best_effort(
            evidence.metadata_path,
            "canonical state no longer matches the exact pending execution lease",
        )
        return "conflict"

    def expected(current: dict[str, Any]) -> bool:
        return (
            _state_int(current, "latest_report") == command_id - 1
            and not evidence.report_path.exists()
            and protocol_core.matches_execution_lease(
                current,
                run_id=metadata["run_id"],
                command_id=command_id,
                claimed_generation=metadata["claim_generation"],
                source=metadata["source"],
                project_id=metadata["project_id"],
            )
        )

    def already_applied(current: dict[str, Any]) -> bool:
        return (
            evidence.report_path.exists()
            and _state_int(current, "latest_report") >= command_id
            and _canonical_report_matches(evidence)
            and not protocol_core.matches_execution_lease(
                current,
                run_id=metadata["run_id"],
                command_id=command_id,
                claimed_generation=metadata["claim_generation"],
                source=metadata["source"],
                project_id=metadata["project_id"],
            )
        )

    def payload_builder(current: dict[str, Any]) -> dict[Path, str]:
        updated = dict(current)
        updated["latest_report"] = command_id
        updated["generation"] = protocol_core.report_generation(
            _state_int(current, "generation", 0)
        )
        updated["active_run"] = None
        updated["worker_pid"] = None
        updated["status"] = metadata["target_status"]
        updated["updated_at"] = bridge_common.now_iso()
        if metadata["last_execution_error"] is None:
            updated.pop("last_execution_error", None)
        else:
            updated["last_execution_error"] = metadata["last_execution_error"]
        return {
            evidence.report_path: evidence.report_text,
            evidence.state_path: bridge_common.json_text(updated),
        }

    try:
        publisher(
            bridge_root=evidence.bridge_root,
            state_path=evidence.state_path,
            expected=expected,
            already_applied=already_applied,
            payload_builder=payload_builder,
            message=(
                f"bridge: reconcile pending {metadata['project_id']} "
                f"report {command_id:03d}"
            ),
        )
    except git_store.CASConflict:
        # A lost CAS is not evidence that the report should be retried.  Read
        # the current canonical state once and reclassify the exact evidence;
        # this recognizes a winning publisher without overwriting newer state.
        try:
            raced_state = bridge_common.load_json(evidence.state_path)
        except (OSError, bridge_common.WorkerError):
            _mark_conflict_best_effort(
                evidence.metadata_path,
                "canonical state changed during pending report CAS; publication refused",
            )
            return "conflict"
        raced_latest = _state_int(raced_state, "latest_report")
        raced_matches = protocol_core.matches_execution_lease(
            raced_state,
            run_id=metadata["run_id"],
            command_id=command_id,
            claimed_generation=metadata["claim_generation"],
            source=metadata["source"],
            project_id=metadata["project_id"],
        )
        if (
            raced_latest >= command_id
            and evidence.report_path.exists()
            and _canonical_report_matches(evidence)
            and not raced_matches
        ):
            _write_status(
                evidence,
                status="superseded",
                reason="a concurrent canonical publisher won the exact report CAS",
            )
            return "superseded"
        _mark_conflict_best_effort(
            evidence.metadata_path,
            "canonical state changed during pending report CAS; publication refused",
        )
        return "conflict"
    except bridge_common.WorkerError as exc:
        print(
            f"[{bridge_common.now_iso()}] pending report publication deferred for "
            f"{metadata['project_id']}#{command_id:03d}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return "pending"

    _write_status(
        evidence,
        status="reconciled",
        reason="completed pending report published with Protocol v2 generation/CAS",
    )
    print(
        f"[{bridge_common.now_iso()}] {metadata['project_id']}: pending report "
        f"{command_id:03d} reconciled without rerunning Codex"
    )
    return "reconciled"


def _metadata_paths(bridge_root: Path, project_id: str | None) -> list[Path]:
    runtime = bridge_root / "worker" / "runtime"
    if project_id is None:
        roots = [path for path in runtime.iterdir() if path.is_dir()] if runtime.is_dir() else []
    else:
        try:
            roots = [runtime / _safe_project_id(project_id)]
        except PendingReportError:
            return []
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        paths.extend(root.glob("pending-report-*.meta.json"))
    return sorted(paths, key=lambda path: str(path).casefold())


def metadata_paths(bridge_root: Path, project_id: str | None = None) -> list[Path]:
    """List canonical pending sidecars without interpreting their contents."""

    return _metadata_paths(bridge_root, project_id)


def reconcile_evidence(
    evidence: PendingReportEvidence,
    *,
    publish_cas: PublishCAS | None = None,
) -> str:
    """Reconcile one already-validated report through publication-only CAS."""

    return _reconcile_one(evidence, publish_cas=publish_cas)


def reconcile_pending_reports(
    bridge_root: Path,
    *,
    project_id: str | None = None,
    blocked_project_ids: set[str] | None = None,
    publish_cas: PublishCAS | None = None,
    evidence_mode: str | None = None,
) -> int:
    """Scan and safely publish completed Worker reports, never executing Codex."""

    blocked = blocked_project_ids or set()
    completed = 0
    for metadata_path in _metadata_paths(bridge_root, project_id):
        candidate_project = metadata_path.parent.name
        if candidate_project in blocked:
            print(
                f"[{bridge_common.now_iso()}] {candidate_project}: pending report "
                "deferred while network recovery journal remains pending"
            )
            continue
        if evidence_mode is not None:
            try:
                raw_mode = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                ).get("evidence_mode", "legacy")
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if raw_mode != evidence_mode:
                continue
        try:
            evidence = load_evidence(bridge_root, metadata_path)
        except (OSError, PendingReportError) as exc:
            _mark_conflict_best_effort(
                metadata_path,
                f"pending report evidence validation failed: {type(exc).__name__}",
            )
            print(
                f"[{bridge_common.now_iso()}] invalid pending report "
                f"{metadata_path.name}: {type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        try:
            result = _reconcile_one(evidence, publish_cas=publish_cas)
        except (OSError, PendingReportError, TypeError, ValueError) as exc:
            print(
                f"[{bridge_common.now_iso()}] pending report reconciliation failed for "
                f"{metadata_path.name}: {type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        if result in {"reconciled", "superseded"}:
            completed += 1
    return completed
