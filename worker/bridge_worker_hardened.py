#!/usr/bin/env python3
"""Hardened entry point for the protocol-v2 Bridge Worker.

This wrapper keeps the stable worker implementation intact while adding:

1. strict Worker-owned lease / execution-result semantics;
2. host-scoped project mappings reloaded from the tracked remote registry after
   every Bridge sync;
3. pre-claim validation for remote workdirs and Git origins; and
4. a controlled restart signal when tracked Worker implementation changes.
"""

from __future__ import annotations

import state_roots

import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import bridge_worker as bw
import remote_project_registry as rpr
from supervisor_publication_gateway import SupervisorPublicationGateway
import worker_health
from codex_lifecycle import (
    MARKER_CAPTURE_FAILED,
    MARKER_INVALID,
    MARKER_PROCESS_EXITED_WITHOUT_MARKER,
    MARKER_PROCESS_TERMINATED_WITHOUT_MARKER,
    CodexRunResult,
)
from vnext_runtime.adoption import AdoptionMode
from vnext_runtime.handoff import (
    HandoffError,
    HandoffEvidenceStore,
    HandoffPhase,
    OuterControllerHandoff,
)

EXECUTION_RESULT_PREFIX = "BRIDGE_EXECUTION_JSON:"
WORKER_RESTART_CODE = 75
_HANDOFF_EVIDENCE_ENV = "BRIDGE_HANDOFF_EVIDENCE_PATH"
_HANDOFF_ATTEMPT_ENV = "BRIDGE_HANDOFF_ATTEMPT_ID"
_HANDOFF_ALLOWED_PHASES_ENV = "BRIDGE_HANDOFF_ALLOWED_PHASES"
_HANDOFF_CLAIM_PHASES = {"COMPLETED", "ROLLED_BACK"}
_ORIGINAL_BUILD_PROMPT = bw.build_prompt
_ORIGINAL_CODEX_RUN = bw.codex_run


def parse_execution_result(final_message: str) -> dict[str, Any] | None:
    """Return the single strict execution-result object, or None if invalid."""
    matches: list[dict[str, Any]] = []
    for raw_line in final_message.splitlines():
        line = raw_line.strip()
        if not line.startswith(EXECUTION_RESULT_PREFIX):
            continue
        payload = line[len(EXECUTION_RESULT_PREFIX) :].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        matches.append(parsed)
    if len(matches) != 1:
        return None
    status = str(matches[0].get("status", "")).upper()
    if status not in {"SUCCESS", "FAILED", "BLOCKED"}:
        return None
    matches[0]["status"] = status
    return matches[0]


def hardened_build_prompt(project_id: str, mission: str, command: str) -> str:
    base = _ORIGINAL_BUILD_PROMPT(project_id, mission, command)
    guard = r"""

===== WORKER-OWNED LEASE / RESULT CONTRACT =====
IMPORTANT: the local Bridge Worker has ALREADY atomically claimed this exact
command before starting you. The current CODEX_RUNNING / active_run lease is
expected and belongs to this invocation.

- DO NOT invoke `$bridge-manual`.
- DO NOT run `bridge_manual.py start` or `bridge_manual.py finish`.
- DO NOT attempt to claim, acquire, release, supersede, or rewrite any Bridge
  lease/state yourself.
- If you inspect Bridge state and see an active execution lease for this
  command, that is NOT a conflict for you. It is the Worker-held lease that
  authorizes this run. Execute the COMMAND directly.
- Do not edit AI-Agent-Bridge message-bus files.

Your final response MUST contain exactly one machine-readable execution line:

BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}

Use SUCCESS only if the requested command outcome was actually achieved and
verified. If the command was not completed, use exactly one of:

BRIDGE_EXECUTION_JSON: {"status":"FAILED","reason":"concise non-secret reason"}
BRIDGE_EXECUTION_JSON: {"status":"BLOCKED","reason":"concise non-secret reason"}

A normal process exit is not task success. The Worker will treat a missing,
invalid, duplicate, FAILED, or BLOCKED execution marker as a failed command.
For FINALIZE, this execution marker is required in addition to the existing
BRIDGE_FINAL_JSON marker.
"""
    return base + guard


def hardened_codex_run(**kwargs: Any) -> CodexRunResult:
    """Inject the strict parser; keep business result separate from process exit."""
    kwargs["marker_parser"] = parse_execution_result
    result = _ORIGINAL_CODEX_RUN(**kwargs)
    if result.marker is None:
        classification = result.marker_result
        if classification == MARKER_CAPTURE_FAILED:
            detail = result.runtime_error or "final-message file could not be read safely"
            result.contract_error = (
                f"{MARKER_CAPTURE_FAILED}: Bridge could not capture the independent "
                f"{EXECUTION_RESULT_PREFIX} result: {detail}"
            )
        elif classification == MARKER_INVALID:
            result.contract_error = (
                f"{MARKER_INVALID}: final response contained a missing, duplicate, "
                f"partial, corrupt, or invalid {EXECUTION_RESULT_PREFIX} marker."
            )
        elif classification == MARKER_PROCESS_TERMINATED_WITHOUT_MARKER:
            result.contract_error = (
                f"{MARKER_PROCESS_TERMINATED_WITHOUT_MARKER}: Codex was terminated "
                f"before writing one strict {EXECUTION_RESULT_PREFIX} marker."
            )
        else:
            result.marker_result = MARKER_PROCESS_EXITED_WITHOUT_MARKER
            result.contract_error = (
                f"{MARKER_PROCESS_EXITED_WITHOUT_MARKER}: Codex exited without "
                f"writing one strict {EXECUTION_RESULT_PREFIX} marker."
            )
    elif result.marker["status"] != "SUCCESS":
        reason = str(result.marker.get("reason", "")).strip()
        result.contract_error = f"Bridge command reported {result.marker['status']}"
        if reason:
            result.contract_error += f": {reason}"
    return result


def install_hardening() -> None:
    bw.build_prompt = hardened_build_prompt
    bw.codex_run = hardened_codex_run


def _resolve_config_path(bridge_root: Path, configured: str) -> Path:
    path = Path(configured)
    if not path.is_absolute():
        path = bridge_root / path
    return path.resolve()


def _validate_codex_config(config: dict[str, Any]) -> None:
    codex_command = str(config.get("codex_command", "codex"))
    if shutil.which(codex_command) is None and not Path(codex_command).exists():
        raise bw.WorkerError(f"Required Codex command not found: {codex_command}")
    bw.codex_args_from_config(config)


def _post_run_settlement_evidence_clear(
    bridge_root: Path,
    settlement: bw.WorkerRunSettlement,
) -> bool:
    """Require the current run to have no unresolved publication evidence."""

    try:
        metadata_path = bw.pending_report.canonical_metadata_path(
            bridge_root,
            settlement.project_id,
            settlement.command_id,
        )
        pending_path = bridge_root / Path(
            bw.pending_report.canonical_pending_relative_path(
                settlement.project_id,
                settlement.command_id,
            )
        )
        recovery_path = bw.recovery_journal.journal_path(
            bridge_root,
            settlement.project_id,
            settlement.run_id,
        )
    except Exception:
        return False
    return not any(
        path.exists() for path in (metadata_path, pending_path, recovery_path)
    )


def should_request_post_run_handoff_exit(
    bridge_root: Path,
    settlement: bw.WorkerRunSettlement,
) -> bool:
    """Validate a fresh exact handoff without writing or advancing it."""

    if not isinstance(settlement, bw.WorkerRunSettlement):
        return False
    if settlement.outcome != "SUCCESS":
        return False
    state: Mapping[str, Any] = settlement.final_state
    if not isinstance(state, Mapping):
        return False
    if state.get("status") not in {"REPORT_READY", "FINAL_REPORT_READY"}:
        return False
    if state.get("project_id") != settlement.project_id:
        return False
    if state.get("active_run", object()) is not None:
        return False
    if state.get("worker_pid", object()) is not None:
        return False
    if any(
        (
            isinstance(state.get(field), bool)
            or not isinstance(state.get(field), int)
            or state.get(field) != expected
        )
        for field, expected in (
            ("latest_command", settlement.command_id),
            ("latest_report", settlement.command_id),
        )
    ):
        return False
    generation = state.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= settlement.claim_generation
    ):
        return False

    report_path = (
        bridge_root
        / "projects"
        / settlement.project_id
        / "reports"
        / f"report-{settlement.command_id:03d}.md"
    )
    if not report_path.is_file() or not _post_run_settlement_evidence_clear(
        bridge_root,
        settlement,
    ):
        return False

    evidence_path = bridge_root / "worker" / "runtime" / "adoption-handoff.json"
    store = HandoffEvidenceStore(evidence_path)
    try:
        with store.locked():
            evidence = store.read_optional()
            if evidence is None or evidence.phase is not HandoffPhase.PREPARED:
                return False
            identity = evidence.identity
            if identity.mode is not AdoptionMode.MANUAL:
                return False
            if (
                identity.initiating_project_id != settlement.project_id
                or identity.initiating_command_id != settlement.command_id
                or identity.initiating_run_id != settlement.run_id
                or identity.initiating_claim_generation != settlement.claim_generation
                or identity.replay_initiating_command
            ):
                return False
            recovered = OuterControllerHandoff(store).recover(identity)
            return (
                recovered.phase is HandoffPhase.PREPARED
                and recovered.transition_seq == 0
                and not recovered.worker_boundary_quiesced
                and recovered.active_run_ids == ()
                and recovered.worker_exit_code is None
                and recovered.restart_requested_at is None
                and recovered.observed_launcher_identity is None
                and recovered.observed_worker_identity is None
                and recovered.observed_worker_sha is None
                and recovered.failure_reason is None
                and recovered.rollback_commit_sha is None
            )
    except (HandoffError, OSError, TypeError, ValueError):
        return False


def _post_run_handoff_callback(
    bridge_root: Path,
) -> Callable[[bw.WorkerRunSettlement], None]:
    """Create a one-shot callback for the current Worker process."""

    restart_requested = False

    def callback(settlement: bw.WorkerRunSettlement) -> None:
        nonlocal restart_requested
        if restart_requested:
            return
        if should_request_post_run_handoff_exit(bridge_root, settlement):
            restart_requested = True
            raise bw.PostRunHandoffRestartRequested(
                "fresh exact handoff is prepared after durable run settlement"
            )

    return callback


def _handoff_claims_allowed(
    bridge_root: Path,
    *,
    runtime_root: Path,
) -> bool:
    """Keep a Launcher-started Worker out of claims until terminal evidence."""

    evidence_raw = os.environ.get(_HANDOFF_EVIDENCE_ENV)
    attempt_id = os.environ.get(_HANDOFF_ATTEMPT_ENV)
    allowed_raw = os.environ.get(_HANDOFF_ALLOWED_PHASES_ENV)
    if evidence_raw is None and attempt_id is None and allowed_raw is None:
        return True
    if not evidence_raw or not attempt_id or not allowed_raw:
        return False
    allowed = {
        item.strip()
        for item in allowed_raw.split(",")
        if item.strip()
    }
    if not allowed or not allowed.issubset(_HANDOFF_CLAIM_PHASES):
        return False
    try:
        evidence_path = Path(evidence_raw).expanduser().resolve()
        expected_path = (
            Path(runtime_root).resolve() / "adoption-handoff.json"
        )
        if evidence_path != expected_path:
            return False
        evidence = HandoffEvidenceStore(evidence_path).read()
        return (
            evidence.identity.attempt_id == attempt_id
            and evidence.phase.value in allowed
        )
    except Exception:
        # A replacement Worker must fail closed on missing, stale, corrupt or
        # mismatched handoff evidence.  This helper never writes the evidence.
        return False


def _remote_project_is_safe_to_process(
    bridge_root: Path,
    project_id: str,
    project_cfg: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    if not project_cfg.get("_remote_registry"):
        return True
    state_path = bridge_root / "projects" / project_id / "state.json"
    if not state_path.is_file():
        return True
    try:
        state = bw.load_json(state_path)
    except Exception:
        return True
    if str(state.get("status", "")) not in bw.PENDING_STATES:
        return True
    try:
        rpr.validate_runtime_project(project_id, project_cfg, config, bridge_root)
        return True
    except rpr.RemoteProjectError as exc:
        print(
            f"[{bw.now_iso()}] {project_id}: remote mapping rejected before claim: {exc}",
            file=sys.stderr,
        )
        return False


def _record_worker_health(
    bridge_root: Path,
    *,
    runtime_root: Path | None = None,
    **updates: Any,
) -> None:
    """Health evidence is best effort and must not stop the worker."""
    try:
        worker_health.update_worker_health(
            bridge_root,
            runtime_root=runtime_root,
            **updates,
        )
    except Exception:
        return None


def _observe_project(
    bridge_root: Path,
    project_id: str,
    *,
    runtime_root: Path | None = None,
) -> tuple[str, int, int] | None:
    """Record the last locally observed protocol snapshot without raw content."""
    state_path = bridge_root / "projects" / project_id / "state.json"
    try:
        state = bw.load_json(state_path)
        status = str(state.get("status", ""))
        command_id = int(state.get("latest_command", 0))
        latest_report = int(state.get("latest_report", 0))
    except Exception:
        _record_worker_health(
            bridge_root,
            runtime_root=runtime_root,
            last_seen_project=project_id,
            last_failure_kind="state_observation_error",
        )
        return None
    updates: dict[str, Any] = {
        "last_seen_project": project_id,
        "last_seen_state": status,
    }
    if status in bw.PENDING_STATES and command_id > latest_report:
        updates["last_command_seen"] = f"{project_id}#{command_id:03d}"
    _record_worker_health(bridge_root, runtime_root=runtime_root, **updates)
    return status, command_id, latest_report


def _record_worker_exit(
    bridge_root: Path,
    exit_code: int,
    reason: str | None = None,
    *,
    runtime_root: Path | None = None,
) -> None:
    updates: dict[str, Any] = {
        "last_process_exit": exit_code,
        "last_process_exit_at": worker_health.now_iso(),
        "worker_pid": None,
    }
    if reason:
        updates["last_failure_kind"] = reason
    _record_worker_health(bridge_root, runtime_root=runtime_root, **updates)


def main(*, runtime_root: Path | None = None) -> int:
    install_hardening()
    args = bw.parse_args()
    code_root = state_roots.engine_root()
    bridge_root = state_roots.resolve_state_root(args.state_root, for_write=True)
    runtime_root = (
        runtime_root
        if runtime_root is not None
        else bridge_root / "worker" / "runtime"
    ).resolve()
    worker_started_at = worker_health.now_iso()
    _record_worker_health(
        bridge_root,
        runtime_root=runtime_root,
        host=platform.node() or "unknown",
        pid=os.getpid(),
        worker_pid=os.getpid(),
        worker_started_at=worker_started_at,
        owner_console_control_version=1,
        last_worker_start=worker_started_at,
        last_failure_kind=None,
    )

    bw.ensure_tool("git")
    config_path = _resolve_config_path(bridge_root, args.config)
    if not config_path.is_file():
        raise bw.WorkerError(f"Worker config does not exist: {config_path}")

    initial_config = rpr.load_runtime_config(config_path, bridge_root)
    state_roots.require_split_runtime_policy(bridge_root, initial_config)
    _validate_codex_config(initial_config)
    config = initial_config
    boot_head = rpr.git_head(code_root)
    lock_path = runtime_root / "worker.lock"
    poll_count = 0
    on_run_settled = _post_run_handoff_callback(bridge_root)
    supervisor_gateway = SupervisorPublicationGateway(bridge_root)
    coordinator: bw.WorkerCoordinator | None = None

    try:
        with bw.WorkerInstanceLock(lock_path):
            coordinator = bw.WorkerCoordinator(
                bridge_root,
                config,
                runtime_root=runtime_root,
                on_run_settled=on_run_settled,
            )
            bw.refresh_active_run_health(bridge_root, runtime_root=runtime_root)
            while True:
                poll_count += 1
                poll_at = worker_health.now_iso()
                _record_worker_health(
                    bridge_root,
                    runtime_root=runtime_root,
                    last_poll_at=poll_at,
                    last_poll_attempt=poll_at,
                    poll_count=poll_count,
                    worker_pid=os.getpid(),
                )
                try:
                    processed_any = coordinator.reap()
                    bw.sync_to_remote(bridge_root)
                    successful_fetch_at = worker_health.now_iso()
                    _record_worker_health(
                        bridge_root,
                        runtime_root=runtime_root,
                        last_successful_fetch_at=successful_fetch_at,
                        last_successful_fetch=successful_fetch_at,
                    )
                    # Reconcile durable interruption evidence immediately
                    # after fetch, even when this process is about to request
                    # a hot restart for updated Worker code.
                    processed_any = bool(
                        bw.reconcile_pending_recoveries(
                            bridge_root,
                            project_id=args.project,
                            config=config,
                        )
                    ) or processed_any
                    processed_any = bool(
                        bw.reconcile_pending_reports(
                            bridge_root,
                            project_id=args.project,
                            config=config,
                        )
                    ) or processed_any
                    bw.refresh_active_run_health(bridge_root, runtime_root=runtime_root)
                    if rpr.worker_code_changed(code_root, boot_head):
                        coordinator.begin_draining()
                        print(
                            f"[{bw.now_iso()}] Worker implementation changed on GitHub; "
                            f"requesting launcher restart (exit {WORKER_RESTART_CODE})."
                        )
                        _record_worker_exit(
                            bridge_root,
                            WORKER_RESTART_CODE,
                            "worker_code_changed",
                            runtime_root=runtime_root,
                        )
                        return WORKER_RESTART_CODE

                    gateway_results = supervisor_gateway.poll(project_id=args.project)
                    gateway_processed = any(
                        result.outcome in {"published", "already_applied"}
                        for result in gateway_results
                    )
                    last_gateway = gateway_results[-1] if gateway_results else None
                    _record_worker_health(
                        bridge_root,
                        runtime_root=runtime_root,
                        last_gateway_poll_at=worker_health.now_iso(),
                        last_gateway_request_id=(
                            last_gateway.request_id if last_gateway else None
                        ),
                        last_gateway_project=(
                            last_gateway.project_id if last_gateway else None
                        ),
                        last_gateway_command_id=(
                            last_gateway.command_id if last_gateway else None
                        ),
                        last_gateway_outcome=(
                            last_gateway.outcome if last_gateway else "none"
                        ),
                        last_gateway_reason=(
                            last_gateway.reason if last_gateway else "none"
                        ),
                    )
                    for result in gateway_results:
                        print(
                            f"[{bw.now_iso()}] supervisor_gateway "
                            f"request={result.request_id} outcome={result.outcome} "
                            f"project={result.project_id or 'unknown'} "
                            f"command={result.command_id or 0} reason={result.reason}"
                        )
                    processed_any = gateway_processed or processed_any

                    config = rpr.load_runtime_config(config_path, bridge_root)
                    state_roots.require_split_runtime_policy(bridge_root, config)
                    _validate_codex_config(config)
                    projects: dict[str, Any] = config["projects"]
                    for project_id, project_cfg in projects.items():
                        if args.project and project_id != args.project:
                            continue
                        if (
                            not isinstance(project_cfg, dict)
                            or not project_cfg.get("enabled", True)
                        ):
                            continue
                        if not _handoff_claims_allowed(
                            bridge_root,
                            runtime_root=runtime_root,
                        ):
                            _record_worker_health(
                                bridge_root,
                                runtime_root=runtime_root,
                                coordinator_lifecycle="DRAINING",
                            )
                            continue
                        _observe_project(
                            bridge_root,
                            project_id,
                            runtime_root=runtime_root,
                        )
                        if not _remote_project_is_safe_to_process(
                            bridge_root,
                            project_id,
                            project_cfg,
                            config,
                        ):
                            continue
                        coordinator.submit(project_id, project_cfg, config)
                    if args.once:
                        processed_any = coordinator.reap(wait=True) or processed_any
                    _record_worker_health(
                        bridge_root,
                        runtime_root=runtime_root,
                        last_successful_poll_at=worker_health.now_iso(),
                        last_failure_kind=None,
                    )
                    if args.once:
                        if not processed_any:
                            print(f"[{bw.now_iso()}] No pending command.")
                        _record_worker_exit(
                            bridge_root,
                            0,
                            runtime_root=runtime_root,
                        )
                        return 0
                except bw.PostRunHandoffRestartRequested:
                    print(
                        f"[{bw.now_iso()}] exact handoff prepared after run settlement; "
                        f"requesting launcher restart (exit {WORKER_RESTART_CODE})."
                    )
                    _record_worker_exit(
                        bridge_root,
                        WORKER_RESTART_CODE,
                        "post_run_handoff_prepared",
                        runtime_root=runtime_root,
                    )
                    bw.refresh_active_run_health(bridge_root, runtime_root=runtime_root)
                    return WORKER_RESTART_CODE
                except KeyboardInterrupt:
                    _record_worker_exit(
                        bridge_root,
                        130,
                        "keyboard_interrupt",
                        runtime_root=runtime_root,
                    )
                    return 130
                except Exception as exc:
                    print(
                        f"[{bw.now_iso()}] ERROR: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    _record_worker_health(
                        bridge_root,
                        runtime_root=runtime_root,
                        last_failure_kind=type(exc).__name__,
                    )
                    bw.refresh_active_run_health(bridge_root, runtime_root=runtime_root)
                    if args.once:
                        _record_worker_exit(
                            bridge_root,
                            1,
                            type(exc).__name__,
                            runtime_root=runtime_root,
                        )
                        return 1

                poll_seconds = max(5, int(config.get("poll_seconds", 30)))
                time.sleep(poll_seconds)
    except bw.WorkerError as exc:
        print(f"[{bw.now_iso()}] ERROR: {exc}", file=sys.stderr)
        _record_worker_exit(
            bridge_root,
            1,
            type(exc).__name__,
            runtime_root=runtime_root,
        )
        return 1
    finally:
        if coordinator is not None:
            coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
