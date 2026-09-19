#!/usr/bin/env python3
"""
Manual fast-lane helper for AI-Agent-Bridge protocol v2.

This helper does not execute Codex. It only:
1) atomically publishes+claims a manual command before a human-driven Codex turn
   touches the target project, and
2) publishes that turn's final report and releases the lease.

It uses a short-lived temporary clone so it can race safely with the continuously
running Bridge Worker, which owns the normal local bridge clone.
"""

from __future__ import annotations

import state_roots

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import bridge_common as common
import git_store
import protocol_core
import report_builder
import worker_execution_control

WorkerError = common.WorkerError
CASConflict = common.CASConflict

MANUAL_SOURCE = "manual_chatgpt"
MANUAL_GIT_USER_NAME = "AI-Agent-Bridge Manual"
MANUAL_GIT_USER_EMAIL = "bridge-manual@users.noreply.github.com"
ALLOWED_KINDS = protocol_core.COMMAND_KINDS
COMMAND_FILE_RE = re.compile(r"^command-(\d+)\.md$", re.IGNORECASE)
WITHDRAWAL_RESOLUTION = protocol_core.WITHDRAWAL_RESOLUTION


def script_bridge_root() -> Path:
    return state_roots.resolve_state_root()


def project_paths(bridge_root: Path, project_id: str) -> tuple[Path, Path]:
    project_dir = bridge_root / "projects" / project_id
    state_path = project_dir / "state.json"
    if not state_path.exists():
        raise WorkerError(f"Unknown bridge project: {project_id}")
    return project_dir, state_path


def next_command_id(project_dir: Path, state: dict[str, Any]) -> int:
    ids = [
        int(state.get("latest_command", 0)),
        int(state.get("latest_report", 0)),
    ]
    commands_dir = project_dir / "commands"
    if commands_dir.exists():
        for path in commands_dir.iterdir():
            match = COMMAND_FILE_RE.match(path.name)
            if match:
                ids.append(int(match.group(1)))
    return max(ids, default=0) + 1


def command_history_ids(project_dir: Path) -> list[int]:
    """Return every command id represented by an immutable command file."""

    commands_dir = project_dir / "commands"
    if not commands_dir.exists():
        return []
    result: list[int] = []
    for path in commands_dir.iterdir():
        match = COMMAND_FILE_RE.match(path.name)
        if match:
            result.append(int(match.group(1)))
    return sorted(set(result))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def command_metadata_from_path(path: Path, *, filename_command_id: int) -> dict[str, Any]:
    """Read and validate one command's complete v2 metadata envelope."""

    try:
        meta = protocol_core.parse_command_metadata(
            path.read_text(encoding="utf-8")
        )
        protocol_core.validate_command_metadata(meta)
        errors = protocol_core.command_contract_errors(
            meta,
            filename_command_id=filename_command_id,
        )
    except (OSError, UnicodeError, protocol_core.ProtocolViolation) as exc:
        raise WorkerError(f"Invalid canonical command {path.name}: {exc}") from exc
    if errors:
        raise WorkerError(
            f"Invalid canonical command {path.name}: " + "; ".join(errors)
        )
    return meta


def report_path(project_dir: Path, command_id: int) -> Path:
    return project_dir / "reports" / f"report-{command_id:03d}.md"


def canonical_command_meta(
    project_dir: Path,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    if state.get("status") != "COMMAND_READY":
        return None
    command_id = int(state.get("latest_command", 0))
    path = project_dir / "commands" / f"command-{command_id:03d}.md"
    if not path.exists():
        raise WorkerError(f"Canonical command file is missing: {path}")
    try:
        meta = protocol_core.parse_command_metadata(path.read_text(encoding="utf-8"))
        protocol_core.validate_command_metadata(meta)
        return meta
    except protocol_core.ProtocolViolation as exc:
        raise WorkerError(str(exc)) from exc


def validate_manual_start(
    state: dict[str, Any],
    existing_meta: dict[str, Any] | None,
) -> int | None:
    try:
        decision = protocol_core.manual_start_decision(state, existing_meta)
    except protocol_core.ProtocolConflict as exc:
        raise CASConflict(str(exc)) from exc
    except protocol_core.ProtocolViolation as exc:
        raise WorkerError(str(exc)) from exc
    return decision.supersedes_command_id


def render_manual_command(
    *,
    command_id: int,
    body: str,
    based_on_report: int,
    expected_generation: int,
    kind: str,
    request_id: str,
    supersedes_command_id: int | None,
    withdraws_command_id: int | None = None,
) -> str:
    metadata: dict[str, Any] = {
        "command_id": command_id,
        "source": MANUAL_SOURCE,
        "based_on_report": based_on_report,
        "expected_generation": expected_generation,
        "kind": kind,
        "manual_request_id": request_id,
    }
    if supersedes_command_id is not None:
        metadata["supersedes_command_id"] = supersedes_command_id
    if withdraws_command_id is not None:
        metadata["withdraws_command_id"] = withdraws_command_id

    meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    return (
        f"<!-- bridge-command: {meta} -->\n"
        f"# Command {command_id:03d} — manual fast lane\n\n"
        f"{body.rstrip()}\n"
    )


def build_manual_claim_state(
    *,
    current: dict[str, Any],
    command_id: int,
    run_id: str,
    request_id: str,
    kind: str,
    supersedes_command_id: int | None,
    lease_hours: float,
    withdraws_command_id: int | None = None,
) -> dict[str, Any]:
    updated = dict(current)
    base_generation = int(current.get("generation", 0))
    latest_report = int(current.get("latest_report", 0))
    claimed_at = common.now_dt()

    # Manual publication and claim are one atomic commit, but consume two logical
    # generations: one for command publication and one for execution claim.
    generation_plan = protocol_core.manual_claim_generations(base_generation)
    publication_generation = generation_plan.publication_generation
    claimed_generation = generation_plan.claimed_generation

    updated["status"] = "CODEX_RUNNING"
    updated["generation"] = claimed_generation
    updated["latest_command"] = command_id
    updated["last_reviewed_report"] = latest_report
    updated["active_run"] = {
        "run_id": run_id,
        "command_id": command_id,
        "source": MANUAL_SOURCE,
        "kind": kind,
        "based_on_report": latest_report,
        "base_generation": base_generation,
        "publication_generation": publication_generation,
        "claimed_generation": claimed_generation,
        "claimed_at": claimed_at.isoformat(timespec="seconds"),
        "lease_expires_at": (
            claimed_at + timedelta(hours=lease_hours)
        ).isoformat(timespec="seconds"),
        "executor_host": platform.node() or "unknown",
        "executor_pid": os.getpid(),
        "manual_request_id": request_id,
    }
    if supersedes_command_id is not None:
        updated["active_run"]["supersedes_command_id"] = supersedes_command_id
    if withdraws_command_id is not None:
        updated["active_run"]["withdraws_command_id"] = withdraws_command_id
    updated["worker_host"] = platform.node() or "unknown"
    updated["worker_pid"] = None
    updated["updated_at"] = claimed_at.isoformat(timespec="seconds")
    return updated


def manual_report_markdown(
    *,
    project_id: str,
    command_id: int,
    outcome: str,
    final_message: str,
    active_run: dict[str, Any],
) -> str:
    return report_builder.build_manual_report(
        project_id=project_id,
        command_id=command_id,
        outcome=outcome,
        final_message=final_message,
        active_run=active_run,
        completed_at=common.now_iso(),
        executor_host=platform.node() or "unknown",
    )


def final_status(kind: str, outcome: str, final_message: str) -> str:
    return protocol_core.report_status_after_execution(
        kind,
        outcome,
        final_message,
    )


def active_run_matches(
    state: dict[str, Any],
    *,
    run_id: str,
    command_id: int,
    claimed_generation: int,
) -> bool:
    return protocol_core.matches_execution_lease(
        state,
        run_id=run_id,
        command_id=command_id,
        claimed_generation=claimed_generation,
        source=MANUAL_SOURCE,
    )


def original_remote_info(bridge_root: Path) -> tuple[str, str]:
    remote = git_store.git(bridge_root, "remote", "get-url", "origin").stdout.strip()
    if not remote:
        raise WorkerError("Bridge clone has no origin remote.")
    branch = git_store.current_branch(bridge_root)
    return remote, branch


class TemporaryBridgeClone:
    def __init__(self, source_root: Path):
        self.source_root = source_root
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None

    def __enter__(self) -> Path:
        remote, branch = original_remote_info(self.source_root)
        self.temp = tempfile.TemporaryDirectory(prefix="ai-agent-bridge-manual-")
        self.root = Path(self.temp.name) / "repo"
        result = git_store.run_process(
            [
                "git",
                "clone",
                "--quiet",
                "--single-branch",
                "--branch",
                branch,
                remote,
                str(self.root),
            ],
            cwd=Path(self.temp.name),
        )
        if result.returncode != 0:
            self.temp.cleanup()
            self.temp = None
            raise WorkerError(
                "Unable to create temporary Bridge clone for manual CAS operations.\n"
                + result.stderr[-4000:]
            )
        # A fresh temporary clone has no user identity. Configure it locally so
        # the helper can publish its atomic command/claim commit without
        # changing the caller's global Git configuration.
        git_store.git(self.root, "config", "user.name", MANUAL_GIT_USER_NAME)
        git_store.git(self.root, "config", "user.email", MANUAL_GIT_USER_EMAIL)
        state_roots.inherit_local_binding(self.source_root, self.root)
        return self.root

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None
            self.root = None


def start_manual(
    *,
    source_root: Path,
    project_id: str,
    body: str,
    kind: str,
    lease_hours: float,
) -> dict[str, Any]:
    kind = kind.upper()
    if kind not in ALLOWED_KINDS:
        raise WorkerError(f"Unsupported manual command kind: {kind}")
    if not body.strip():
        raise WorkerError("Manual command body must not be empty.")

    request_id = uuid.uuid4().hex

    with TemporaryBridgeClone(source_root) as bridge_root:
        project_dir, state_path = project_paths(bridge_root, project_id)
        snapshot = common.load_json(state_path)
        existing_meta = canonical_command_meta(project_dir, snapshot)
        supersedes = validate_manual_start(snapshot, existing_meta)

        if kind == "FINALIZE" and snapshot.get("status") != "REPORT_READY":
            raise CASConflict("Manual FINALIZE may start only from REPORT_READY.")

        snapshot_key = {
            "status": snapshot.get("status"),
            "generation": int(snapshot.get("generation", -1)),
            "latest_command": int(snapshot.get("latest_command", -1)),
            "latest_report": int(snapshot.get("latest_report", -1)),
        }

        def expected(current: dict[str, Any]) -> bool:
            return (
                not current.get("active_run")
                and current.get("status") == snapshot_key["status"]
                and int(current.get("generation", -1)) == snapshot_key["generation"]
                and int(current.get("latest_command", -1))
                == snapshot_key["latest_command"]
                and int(current.get("latest_report", -1))
                == snapshot_key["latest_report"]
            )

        def already(current: dict[str, Any]) -> bool:
            active = current.get("active_run")
            return bool(
                current.get("status") == "CODEX_RUNNING"
                and isinstance(active, dict)
                and active.get("manual_request_id") == request_id
            )

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            worker_execution_control.require_new_execution(bridge_root)
            command_id = next_command_id(project_dir, current)
            run_id = f"manual-{command_id:03d}-{request_id[:12]}"
            generation = int(current.get("generation", 0))
            publication_generation = protocol_core.publication_generation(generation)
            latest_report = int(current.get("latest_report", 0))
            command_path = (
                project_dir / "commands" / f"command-{command_id:03d}.md"
            )
            if command_path.exists():
                raise CASConflict(
                    f"Refusing to overwrite existing command file: {command_path.name}"
                )
            command_text = render_manual_command(
                command_id=command_id,
                body=body,
                based_on_report=latest_report,
                expected_generation=publication_generation,
                kind=kind,
                request_id=request_id,
                supersedes_command_id=supersedes,
            )
            updated = build_manual_claim_state(
                current=current,
                command_id=command_id,
                run_id=run_id,
                request_id=request_id,
                kind=kind,
                supersedes_command_id=supersedes,
                lease_hours=lease_hours,
            )
            return {
                command_path: command_text,
                state_path: common.json_text(updated),
            }

        result = git_store.publish_cas(
            bridge_root=bridge_root,
            state_path=state_path,
            expected=expected,
            already_applied=already,
            payload_builder=payload,
            message=f"bridge: manual claim {project_id}",
        )

        active = result.get("active_run")
        if not isinstance(active, dict) or active.get("manual_request_id") != request_id:
            raise WorkerError("Manual claim publish returned an unexpected state.")

        return {
            "project_id": project_id,
            "command_id": int(active["command_id"]),
            "run_id": str(active["run_id"]),
            "claimed_generation": int(active["claimed_generation"]),
            "based_on_report": int(active["based_on_report"]),
            "kind": str(active.get("kind", kind)),
            "supersedes_command_id": active.get("supersedes_command_id"),
            "lease_expires_at": active.get("lease_expires_at"),
            "status": result.get("status"),
        }


def withdraw_and_manual_start(
    *,
    source_root: Path,
    project_id: str,
    target_command_id: int,
    body: str,
    reason: str,
    lease_hours: float,
) -> dict[str, Any]:
    """Atomically withdraw one unclaimed manual command and claim its replacement.

    This is intentionally narrower than either ordinary manual start or a
    generic cancellation operation.  The target must still be the exact
    canonical, unclaimed manual EXECUTE command with no report.  The new
    command and the state transition (including the immutable withdrawal
    record and the real execution lease) are committed by one shared
    ``git_store.publish_cas`` transaction.  No REPORT_READY state or report is
    created for the withdrawn command.
    """

    if isinstance(target_command_id, bool) or not isinstance(target_command_id, int):
        raise WorkerError("--command-id must be a positive integer.")
    if target_command_id < 1:
        raise WorkerError("--command-id must be a positive integer.")
    if not isinstance(body, str) or not body.strip():
        raise WorkerError("Replacement command body must not be empty.")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkerError("--reason must be non-empty text.")
    if (
        isinstance(lease_hours, bool)
        or not isinstance(lease_hours, (int, float))
        or not math.isfinite(lease_hours)
        or lease_hours <= 0
    ):
        raise WorkerError("--lease-hours must be greater than zero.")

    request_id = uuid.uuid4().hex

    with TemporaryBridgeClone(source_root) as bridge_root:
        project_dir, state_path = project_paths(bridge_root, project_id)
        snapshot = common.load_json(state_path)
        initial_state_bytes = state_path.read_bytes()
        initial_state_sha256 = sha256_bytes(initial_state_bytes)

        target_path = project_dir / "commands" / f"command-{target_command_id:03d}.md"
        if not target_path.exists():
            raise WorkerError(f"Canonical target command file is missing: {target_path}")
        target_bytes = target_path.read_bytes()
        target_sha256 = sha256_bytes(target_bytes)
        target_meta = command_metadata_from_path(
            target_path,
            filename_command_id=target_command_id,
        )
        target_report_path = report_path(project_dir, target_command_id)

        replacement_command_id = next_command_id(project_dir, snapshot)
        base_generation = int(snapshot.get("generation", -1))
        publication_generation = protocol_core.publication_generation(base_generation)
        command_text = render_manual_command(
            command_id=replacement_command_id,
            body=body,
            based_on_report=int(snapshot.get("latest_report", -1)),
            expected_generation=publication_generation,
            kind="EXECUTE",
            request_id=request_id,
            supersedes_command_id=None,
            withdraws_command_id=target_command_id,
        )
        replacement_meta = protocol_core.parse_command_metadata(command_text)

        def decide(current: dict[str, Any]) -> protocol_core.WithdrawAndManualStartDecision:
            try:
                return protocol_core.withdraw_and_manual_start_decision(
                    current,
                    target_command_id=target_command_id,
                    target_meta=target_meta,
                    target_report_exists=target_report_path.exists(),
                    reason=reason.strip(),
                    replacement_meta=replacement_meta,
                    replacement_filename_command_id=replacement_command_id,
                    existing_command_ids=command_history_ids(project_dir),
                    existing_withdrawal_command_ids=(
                        record.get("command_id")
                        for record in current.get("withdrawn_commands", [])
                        if isinstance(record, dict)
                    ),
                )
            except protocol_core.ProtocolConflict as exc:
                raise CASConflict(str(exc)) from exc
            except protocol_core.ProtocolViolation as exc:
                raise WorkerError(str(exc)) from exc

        initial_decision = decide(snapshot)
        if initial_decision.replacement_command_id != replacement_command_id:
            raise CASConflict("Replacement command id changed during preparation.")

        run_id = f"manual-{replacement_command_id:03d}-{request_id[:12]}"
        already_applied_seen = False
        initial_snapshot_key = {
            "status": snapshot.get("status"),
            "generation": snapshot.get("generation"),
            "latest_command": snapshot.get("latest_command"),
            "latest_report": snapshot.get("latest_report"),
        }

        def withdrawal_records(current: dict[str, Any]) -> list[dict[str, Any]]:
            raw = current.get("withdrawn_commands", [])
            if raw is None:
                return []
            if not isinstance(raw, list) or any(
                not isinstance(record, dict) for record in raw
            ):
                raise CASConflict(
                    "Canonical withdrawn_commands is malformed; refusing to append."
                )
            normalized: list[dict[str, Any]] = []
            for record in raw:
                try:
                    protocol_core.validate_withdrawal_record(record)
                except protocol_core.ProtocolViolation as exc:
                    raise CASConflict(
                        "Canonical withdrawn_commands contains an invalid record."
                    ) from exc
                normalized.append(dict(record))
            return normalized

        def initial_snapshot_matches(current: dict[str, Any]) -> bool:
            try:
                if sha256_bytes(state_path.read_bytes()) != initial_state_sha256:
                    return False
                for key, expected_value in initial_snapshot_key.items():
                    if current.get(key) != expected_value:
                        return False
                if current.get("active_run") is not None:
                    return False
                if sha256_bytes(target_path.read_bytes()) != target_sha256:
                    return False
                if target_report_path.exists():
                    return False
                records = withdrawal_records(current)
                if any(
                    record.get("command_id") == target_command_id
                    for record in records
                ):
                    return False
                decision = decide(current)
                return (
                    decision.target_command_id == target_command_id
                    and decision.replacement_command_id == replacement_command_id
                    and decision.publication_generation == publication_generation
                    and decision.claimed_generation == initial_decision.claimed_generation
                )
            except (OSError, CASConflict, WorkerError):
                return False

        def build_withdrawal_record(
            active: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "command_id": target_command_id,
                "source": target_meta["source"],
                "kind": target_meta["kind"],
                "command_sha256": target_sha256,
                "reason": reason.strip(),
                "withdrawn_at": active["claimed_at"],
                "replacement_command_id": replacement_command_id,
                "resolution": WITHDRAWAL_RESOLUTION,
            }

        def already(current: dict[str, Any]) -> bool:
            nonlocal already_applied_seen
            active = current.get("active_run")
            if not (
                current.get("status") == "CODEX_RUNNING"
                and current.get("generation") == initial_decision.claimed_generation
                and current.get("latest_command") == replacement_command_id
                and current.get("latest_report") == initial_snapshot_key["latest_report"]
                and isinstance(active, dict)
            ):
                return False
            if not (
                active.get("run_id") == run_id
                and active.get("command_id") == replacement_command_id
                and active.get("source") == MANUAL_SOURCE
                and active.get("kind") == "EXECUTE"
                and active.get("based_on_report") == initial_snapshot_key["latest_report"]
                and active.get("base_generation") == initial_snapshot_key["generation"]
                and active.get("publication_generation") == publication_generation
                and active.get("claimed_generation") == initial_decision.claimed_generation
                and active.get("manual_request_id") == request_id
                and active.get("withdraws_command_id") == target_command_id
                and isinstance(active.get("claimed_at"), str)
            ):
                return False
            try:
                if sha256_bytes(target_path.read_bytes()) != target_sha256:
                    return False
                if target_report_path.exists() or report_path(project_dir, replacement_command_id).exists():
                    return False
                if not replacement_path.exists() or replacement_path.read_text(encoding="utf-8") != command_text:
                    return False
                records = withdrawal_records(current)
                expected_record = build_withdrawal_record(active)
                if expected_record not in records:
                    return False
                already_applied_seen = True
                return True
            except (OSError, CASConflict, WorkerError):
                return False

        replacement_path = (
            project_dir / "commands" / f"command-{replacement_command_id:03d}.md"
        )

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            worker_execution_control.require_new_execution(bridge_root)
            if not initial_snapshot_matches(current):
                raise CASConflict(
                    "Canonical state or command SHA changed before atomic withdrawal."
                )
            if replacement_path.exists():
                raise CASConflict(
                    f"Refusing to overwrite existing command file: {replacement_path.name}"
                )
            active_state = build_manual_claim_state(
                current=current,
                command_id=replacement_command_id,
                run_id=run_id,
                request_id=request_id,
                kind="EXECUTE",
                supersedes_command_id=None,
                lease_hours=lease_hours,
                withdraws_command_id=target_command_id,
            )
            active = active_state.get("active_run")
            if not isinstance(active, dict):
                raise WorkerError("Atomic manual claim did not create an execution lease.")
            record = build_withdrawal_record(active)
            records = withdrawal_records(current)
            if any(
                existing.get("command_id") == target_command_id
                for existing in records
            ):
                raise CASConflict(
                    "Canonical state already contains a withdrawal for the target command."
                )
            active_state["withdrawn_commands"] = records + [record]
            return {
                replacement_path: command_text,
                state_path: common.json_text(active_state),
            }

        result = git_store.publish_cas(
            bridge_root=bridge_root,
            state_path=state_path,
            expected=initial_snapshot_matches,
            already_applied=already,
            payload_builder=payload,
            message=(
                f"bridge: withdraw command {target_command_id:03d} and manual claim "
                f"{replacement_command_id:03d} {project_id}"
            ),
        )

        final_state = common.load_json(state_path)
        final_state_bytes = state_path.read_bytes()
        final_command_bytes = replacement_path.read_bytes()
        final_active = final_state.get("active_run")
        already_applied_result = already_applied_seen
        expected_final_meta = protocol_core.parse_command_metadata(
            final_command_bytes.decode("utf-8")
        )
        final_already_matches = already(final_state)
        immutable_history_matches = (
            expected_final_meta.get("withdraws_command_id") == target_command_id
            and "supersedes_command_id" not in expected_final_meta
            and replacement_path.read_text(encoding="utf-8") == command_text
            and sha256_bytes(target_path.read_bytes()) == target_sha256
        )
        if not final_already_matches:
            raise WorkerError(
                "Atomic withdrawal/manual claim returned an unexpected canonical state."
            )
        if not immutable_history_matches:
            raise WorkerError("Atomic withdrawal/manual claim failed immutable history verification.")

        return {
            "event": "operator.withdraw_and_manual_start",
            "project_id": project_id,
            "target_command_id": target_command_id,
            "target_command_sha256": target_sha256,
            "replacement_command_id": replacement_command_id,
            "replacement_command_sha256": sha256_bytes(final_command_bytes),
            "withdraws_command_id": target_command_id,
            "supersedes_command_id": None,
            "withdrawal_resolution": WITHDRAWAL_RESOLUTION,
            "report_created": False,
            "executor_invoked": False,
            "generation_before": int(initial_snapshot_key["generation"]),
            "publication_generation": publication_generation,
            "generation_after": int(final_state["generation"]),
            "status": final_state["status"],
            "run_id": final_active.get("run_id") if isinstance(final_active, dict) else None,
            "active_run_present": isinstance(final_active, dict),
            "state_sha256_before": initial_state_sha256,
            "state_sha256_after": sha256_bytes(final_state_bytes),
            "already_applied": already_applied_result,
        }


def finish_manual(
    *,
    source_root: Path,
    project_id: str,
    run_id: str,
    command_id: int,
    claimed_generation: int,
    outcome: str,
    final_message: str,
) -> dict[str, Any]:
    outcome = outcome.upper()
    if outcome not in {"SUCCESS", "PARTIAL", "FAILED"}:
        raise WorkerError(f"Unsupported outcome: {outcome}")
    if not final_message.strip():
        raise WorkerError("Manual final report must not be empty.")

    stable_runtime = source_root / "worker" / "runtime" / "manual" / project_id

    with TemporaryBridgeClone(source_root) as bridge_root:
        project_dir, state_path = project_paths(bridge_root, project_id)
        state = common.load_json(state_path)
        active = state.get("active_run")
        if not active_run_matches(
            state,
            run_id=run_id,
            command_id=command_id,
            claimed_generation=claimed_generation,
        ):
            report_path = project_dir / "reports" / f"report-{command_id:03d}.md"
            if (
                int(state.get("latest_report", -1)) == command_id
                and state.get("active_run") is None
                and report_path.exists()
            ):
                return {
                    "project_id": project_id,
                    "command_id": command_id,
                    "run_id": run_id,
                    "status": state.get("status"),
                    "already_published": True,
                }
            raise CASConflict(
                "Manual run no longer owns the canonical execution lease."
            )

        assert isinstance(active, dict)
        kind = str(active.get("kind", "EXECUTE")).upper()
        report_text = manual_report_markdown(
            project_id=project_id,
            command_id=command_id,
            outcome=outcome,
            final_message=final_message,
            active_run=active,
        )
        report_path = project_dir / "reports" / f"report-{command_id:03d}.md"

        def expected(current: dict[str, Any]) -> bool:
            return active_run_matches(
                current,
                run_id=run_id,
                command_id=command_id,
                claimed_generation=claimed_generation,
            )

        def already(current: dict[str, Any]) -> bool:
            return (
                int(current.get("latest_report", -1)) == command_id
                and current.get("active_run") is None
                and current.get("status") in {"REPORT_READY", "FINAL_REPORT_READY"}
            )

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            if report_path.exists():
                raise CASConflict(
                    f"Refusing to overwrite existing report file: {report_path.name}"
                )
            updated = dict(current)
            updated["latest_report"] = command_id
            updated["generation"] = protocol_core.report_generation(
                int(current.get("generation", 0))
            )
            updated["status"] = final_status(kind, outcome, final_message)
            updated["active_run"] = None
            updated["worker_pid"] = None
            updated["updated_at"] = common.now_iso()
            if outcome == "FAILED":
                updated["last_execution_error"] = (
                    "manual Codex execution reported FAILED"
                )
            elif outcome == "PARTIAL":
                updated["last_execution_error"] = (
                    "manual Codex execution reported PARTIAL"
                )
            else:
                updated.pop("last_execution_error", None)
            return {
                report_path: report_text,
                state_path: common.json_text(updated),
            }

        try:
            result = git_store.publish_cas(
                bridge_root=bridge_root,
                state_path=state_path,
                expected=expected,
                already_applied=already,
                payload_builder=payload,
                message=(
                    f"bridge: manual report {project_id} command {command_id:03d}"
                ),
            )
        except (CASConflict, WorkerError) as exc:
            common.save_pending_report(
                stable_runtime,
                command_id,
                report_text,
                (
                    "Manual Codex already finished, but its report could not be safely "
                    "published. Do not rerun the task automatically.\n"
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            raise

        return {
            "project_id": project_id,
            "command_id": command_id,
            "run_id": run_id,
            "status": result.get("status"),
            "latest_report": int(result.get("latest_report", -1)),
            "generation": int(result.get("generation", -1)),
            "already_published": False,
        }


def read_input_file(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        raise WorkerError(
            "Provide --command-file/--report-file or pipe UTF-8 text."
        )
    return sys.stdin.read()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI-Agent-Bridge protocol-v2 manual fast lane"
    )
    parser.add_argument("--state-root", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)

    start = sub.add_parser("start", help="Publish and claim a manual command")
    start.add_argument("--project", required=True)
    start.add_argument("--command-file")
    start.add_argument("--kind", choices=sorted(ALLOWED_KINDS), default="EXECUTE")
    start.add_argument("--lease-hours", type=float, default=6.0)

    withdraw_start = sub.add_parser(
        "withdraw-and-start",
        help="Atomically withdraw an unclaimed manual command and claim its replacement",
    )
    withdraw_start.add_argument("--project", required=True)
    withdraw_start.add_argument("--command-id", type=int, required=True)
    withdraw_start.add_argument("--command-file")
    withdraw_start.add_argument("--reason", required=True)
    withdraw_start.add_argument("--lease-hours", type=float, default=6.0)

    finish = sub.add_parser("finish", help="Publish a manual Codex report")
    finish.add_argument("--project", required=True)
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--command-id", type=int, required=True)
    finish.add_argument("--claimed-generation", type=int, required=True)
    finish.add_argument(
        "--outcome",
        choices=["SUCCESS", "PARTIAL", "FAILED"],
        required=True,
    )
    finish.add_argument("--report-file")

    status = sub.add_parser("status", help="Show current canonical project state")
    status.add_argument("--project", required=True)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = state_roots.resolve_state_root(args.state_root, for_write=True)

    try:
        common.ensure_tool("git")

        if args.action == "start":
            if args.lease_hours <= 0:
                raise WorkerError("--lease-hours must be greater than zero.")
            body = read_input_file(args.command_file)
            result = start_manual(
                source_root=source_root,
                project_id=args.project,
                body=body,
                kind=args.kind,
                lease_hours=args.lease_hours,
            )
            print(
                "BRIDGE_MANUAL_CLAIM: "
                + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            )
            return 0

        if args.action == "withdraw-and-start":
            body = read_input_file(args.command_file)
            result = withdraw_and_manual_start(
                source_root=source_root,
                project_id=args.project,
                target_command_id=args.command_id,
                body=body,
                reason=args.reason,
                lease_hours=args.lease_hours,
            )
            print(
                "BRIDGE_WITHDRAW_AND_MANUAL_CLAIM: "
                + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            )
            return 0

        if args.action == "finish":
            final_message = read_input_file(args.report_file)
            result = finish_manual(
                source_root=source_root,
                project_id=args.project,
                run_id=args.run_id,
                command_id=args.command_id,
                claimed_generation=args.claimed_generation,
                outcome=args.outcome,
                final_message=final_message,
            )
            print(
                "BRIDGE_MANUAL_REPORT: "
                + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            )
            return 0

        if args.action == "status":
            with TemporaryBridgeClone(source_root) as bridge_root:
                _project_dir, state_path = project_paths(bridge_root, args.project)
                print(common.json_text(common.load_json(state_path)), end="")
            return 0

        raise WorkerError(f"Unknown action: {args.action}")

    except (CASConflict, WorkerError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
