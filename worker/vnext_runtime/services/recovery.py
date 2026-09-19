"""Protocol-v2 recovery reconciliation for the vNext Worker runtime.

This service owns only local recovery mechanics and preparation of one
Protocol-aware CAS mutation.  It never imports an executor/provider, creates a
command, replays an interrupted command, or keeps a canonical state/lease.
Local journals and pending reports remain evidence until the injected Durable
Store publisher commits the existing Protocol-v2 transition.
"""

from __future__ import annotations

import subprocess
import sys
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import bridge_common
import git_store
import pending_report
import protocol_core
import recovery_journal

from ..git_effects import (
    GitEffectError,
    GitLsRemoteRefReader,
    GitPushIntent,
    GitPushReconciler,
    split_target_id,
)
from ..models import RunIdentity
from ..recovery_evidence import (
    CanonicalRelation,
    CanonicalRunSnapshot,
    ContainmentEvidence,
    EvidenceState,
    JournalDisposition,
    LifecycleEvidence,
    PublicationEvidence,
    PushReconciliation,
    PushResolutionState,
    RecoveryClassification,
    RecoveryCommandKind,
    RecoveryEnvelope,
    RecoveryEvidenceError,
    RecoveryWal,
    ReportEvidence,
    classify_recovery,
)
from ..recovery_wal import DurableRunWal, DurableWalError


class RecoveryServiceError(bridge_common.WorkerError):
    """Recovery evidence is malformed or cannot be safely reconciled."""


class RecoveryServiceConflict(RecoveryServiceError):
    """Recovery identity or canonical state no longer matches."""


class InterruptedRunEvidence(Protocol):
    """Minimal result view needed when recording interruption evidence.

    The protocol deliberately avoids importing Codex lifecycle/provider types.
    """

    marker_result: str
    exit_code: int
    termination_reason: str | None


PublishCAS = Callable[..., dict[str, Any]]
SavePendingReport = Callable[[Path, int, str, str], None]
CollectEvidence = Callable[..., dict[str, Any]]
EmitAlert = Callable[[Path, dict[str, Any] | None, dict[str, Any]], None]
PushReconcile = Callable[[GitPushIntent], PushReconciliation]


@dataclass(frozen=True, slots=True)
class RecoveryIdentity:
    """Immutable identity of one already-claimed Protocol-v2 run."""

    project_id: str
    command_id: int
    run_id: str
    claim_generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("recovery project_id must be non-empty")
        if (
            isinstance(self.command_id, bool)
            or not isinstance(self.command_id, int)
            or self.command_id < 1
        ):
            raise ValueError("recovery command_id must be positive")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("recovery run_id must be non-empty")
        if (
            isinstance(self.claim_generation, bool)
            or not isinstance(self.claim_generation, int)
            or self.claim_generation < 0
        ):
            raise ValueError("recovery claim_generation must be non-negative")

    @classmethod
    def from_journal(cls, journal: Mapping[str, Any]) -> "RecoveryIdentity":
        try:
            identity = cls(
                project_id=journal["project_id"],
                command_id=journal["command_id"],
                run_id=journal["run_id"],
                claim_generation=journal["claim_generation"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryServiceConflict(
                "recovery journal does not contain an exact run identity"
            ) from exc
        return identity


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Typed, non-canonical description of a prepared recovery transition."""

    identity: RecoveryIdentity
    from_status: str
    to_status: str
    expected_generation: int
    next_generation: int
    reason: str
    automatic_rerun: str = "prohibited"

    def __post_init__(self) -> None:
        if self.from_status != "CODEX_RUNNING" or self.to_status != "RECOVERY_REQUIRED":
            raise ValueError("recovery decision must be the Protocol-v2 recovery transition")
        if self.next_generation != self.expected_generation + 1:
            raise ValueError("recovery decision generation arithmetic is invalid")
        if self.automatic_rerun != "prohibited":
            raise ValueError("recovery decisions must prohibit automatic rerun")


def _state_int(value: Any, default: int | None = -1) -> int | None:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_reason(value: Any) -> str:
    return str(value or "deferred recovery").replace("\x00", " ").replace("\n", " ")[:512]


def _worktree_dirty_evidence(workdir: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _unpushed_commits_evidence(workdir: Path) -> bool | None:
    try:
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if upstream.returncode != 0 or not upstream.stdout.strip():
            return None
        counts = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream.stdout.strip()}"],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values = counts.stdout.strip().split()
    if counts.returncode != 0 or len(values) != 2:
        return None
    try:
        return int(values[0]) > 0
    except ValueError:
        return None


def collect_recovery_evidence(
    *, workdir: Path, head_before: str | None, head_after: str | None
) -> dict[str, Any]:
    """Collect bounded local facts; these facts are never canonical success."""

    return {
        "head_before": head_before,
        "head_after": head_after,
        "worktree_dirty": _worktree_dirty_evidence(workdir),
        "local_commit_created": bool(
            head_before and head_after and head_before != head_after
        ),
        "unpushed_commits_present": _unpushed_commits_evidence(workdir),
        "external_side_effects_unknown": True,
    }


class RecoveryCoordinator:
    """Reconcile recovery evidence while delegating canonical writes to CAS."""

    __slots__ = (
        "bridge_root",
        "publish_cas",
        "emit_alert",
        "config",
        "now_iso",
        "save_pending_report",
        "collect_evidence",
        "reconcile_push",
    )

    def __init__(
        self,
        bridge_root: Path,
        *,
        publish_cas: PublishCAS | None = None,
        emit_alert: EmitAlert | None = None,
        config: dict[str, Any] | None = None,
        now_iso: Callable[[], str] = bridge_common.now_iso,
        save_pending_report: SavePendingReport = bridge_common.save_pending_report,
        collect_evidence: CollectEvidence = collect_recovery_evidence,
        reconcile_push: PushReconcile | None = None,
    ) -> None:
        self.bridge_root = Path(bridge_root).resolve()
        self.publish_cas = publish_cas or git_store.publish_cas
        self.emit_alert = emit_alert or self._ignore_alert
        self.config = config
        self.now_iso = now_iso
        self.save_pending_report = save_pending_report
        self.collect_evidence = collect_evidence
        self.reconcile_push = reconcile_push

    @staticmethod
    def _ignore_alert(
        _bridge_root: Path,
        _config: dict[str, Any] | None,
        _context: dict[str, Any],
    ) -> None:
        return None

    def prepare_recovery_decision(
        self,
        current: Mapping[str, Any],
        identity: RecoveryIdentity,
        *,
        reason: str,
    ) -> RecoveryDecision:
        """Validate exact lease identity and prepare a local typed decision."""

        if not protocol_core.matches_execution_lease(
            current,
            run_id=identity.run_id,
            command_id=identity.command_id,
            claimed_generation=identity.claim_generation,
            project_id=identity.project_id,
        ):
            raise RecoveryServiceConflict(
                "canonical state no longer matches the exact recovery identity"
            )
        generation = _state_int(current.get("generation"), -1)
        if generation is None or generation < 0:
            raise RecoveryServiceConflict("canonical recovery generation is invalid")
        return RecoveryDecision(
            identity=identity,
            from_status="CODEX_RUNNING",
            to_status="RECOVERY_REQUIRED",
            expected_generation=generation,
            next_generation=protocol_core.recovery_generation(generation),
            reason=_safe_reason(reason),
        )

    def recovery_payload(
        self,
        current: Mapping[str, Any],
        decision: RecoveryDecision,
        *,
        last_execution_error: str | None = None,
    ) -> dict[str, Any]:
        """Prepare only the existing canonical Protocol-v2 state payload."""

        identity = decision.identity
        updated = dict(current)
        updated["status"] = decision.to_status
        updated["generation"] = decision.next_generation
        updated["active_run"] = None
        updated["worker_pid"] = None
        updated["recovery_reason"] = decision.reason
        if last_execution_error is not None:
            updated["last_execution_error"] = last_execution_error
        updated["updated_at"] = self.now_iso()
        # Keep this local assertion explicit: no payload may change the run's
        # identity to compensate for an unexpected canonical state.
        if identity.project_id != current.get("project_id"):
            raise RecoveryServiceConflict("recovery project identity changed while preparing payload")
        return updated

    def mark_expired_lease(
        self,
        project_id: str,
        state_path: Path,
        *,
        config: dict[str, Any] | None = None,
        lease_expired: Callable[[Mapping[str, Any]], bool],
    ) -> bool:
        state = bridge_common.load_json(state_path)
        if state.get("status") != "CODEX_RUNNING" or not lease_expired(state):
            return False
        active = state.get("active_run") or {}
        if not isinstance(active, Mapping):
            raise RecoveryServiceConflict("expired execution has no active lease identity")
        identity = RecoveryIdentity(
            project_id=project_id,
            command_id=_state_int(active.get("command_id"), 0) or 0,
            run_id=str(active.get("run_id", "")),
            claim_generation=_state_int(active.get("claimed_generation"), _state_int(state.get("generation"), 0) or 0) or 0,
        )
        lease_expires_at = str(active.get("lease_expires_at", "")) or None
        reason = (
            "execution lease expired"
            f" for run {identity.run_id or 'unknown'}"
            f" command {identity.command_id or 'unknown'}"
            f" claim_generation {identity.claim_generation}"
            + (f" lease_expires_at {lease_expires_at}" if lease_expires_at else "")
            + "; automatic rerun prohibited"
        )

        def expected(current: dict[str, Any]) -> bool:
            return lease_expired(current) and protocol_core.matches_execution_lease(
                current,
                run_id=identity.run_id,
                command_id=identity.command_id,
                claimed_generation=identity.claim_generation,
                project_id=project_id,
            )

        def already(current: dict[str, Any]) -> bool:
            return (
                current.get("status") == "RECOVERY_REQUIRED"
                and current.get("active_run") is None
                and current.get("worker_pid") is None
                and current.get("project_id") == project_id
                and _state_int(current.get("generation"), -1)
                == protocol_core.recovery_generation(identity.claim_generation)
                and _state_int(current.get("latest_command"), -1)
                == identity.command_id
                and identity.run_id in str(current.get("recovery_reason", ""))
            )

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            if current.get("project_id") == project_id:
                decision = self.prepare_recovery_decision(current, identity, reason=reason)
                updated = self.recovery_payload(current, decision)
            else:
                # Legacy callers sometimes provide only the state fields used
                # by the CAS fake.  The real Durable Store checks ``expected``
                # before calling this builder; never use this compatibility
                # branch for an actual canonical state.
                updated = dict(current)
                updated["status"] = "RECOVERY_REQUIRED"
                updated["generation"] = protocol_core.recovery_generation(
                    _state_int(current.get("generation"), 0) or 0
                )
                updated["active_run"] = None
                updated["worker_pid"] = None
                updated["recovery_reason"] = reason
                updated["updated_at"] = self.now_iso()
            return {state_path: bridge_common.json_text(updated)}

        final_state = self.publish_cas(
            bridge_root=self.bridge_root,
            state_path=state_path,
            expected=expected,
            already_applied=already,
            payload_builder=payload,
            message=f"bridge: recovery required for {project_id}",
        )
        self.emit_alert(
            self.bridge_root,
            config if config is not None else self.config,
            {
                "timestamp": self.now_iso(),
                "project_id": project_id,
                "command_id": identity.command_id,
                "run_id": identity.run_id or "unknown",
                "alert_kind": "lease_expired",
                "current_bridge_status": str(final_state.get("status", "RECOVERY_REQUIRED")),
                "original_status": "CODEX_RUNNING",
                "generation": _state_int(final_state.get("generation"), None),
                "generation_before": _state_int(state.get("generation"), None),
                "claim_generation": identity.claim_generation,
                "interruption_recovery_type": "expired execution lease",
                "recovery_reason_safe": str(final_state.get("recovery_reason", reason)),
                "codex_terminated": None,
                "worker_is_alive": True,
                "journal_saved": False,
                "pending_report_saved": False,
                "remote_recovery_cas_success": True,
                "remote_publish_pending": False,
                "worktree_dirty": None,
                "local_commit_created": None,
                "unpushed_commits_present": None,
                "external_side_effects_unknown": True,
                "lease_expires_at": lease_expires_at,
            },
        )
        print(f"[{self.now_iso()}] {project_id}: expired lease -> RECOVERY_REQUIRED")
        return True

    def publish_network_recovery(
        self,
        *,
        state_path: Path,
        project_id: str,
        command_id: int,
        run_id: str,
        claim_generation: int,
        reason: str,
        report_text: str,
        runtime_dir: Path,
        workdir: Path | None = None,
        head_before: str | None = None,
        head_after: str | None = None,
        claimed_at: str | None = None,
        lease_expires_at: str | None = None,
        report_path: Path | None = None,
        run_result: InterruptedRunEvidence | None = None,
        interrupted_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist evidence, then publish exactly one recovery CAS transition."""

        identity = RecoveryIdentity(project_id, command_id, run_id, claim_generation)
        workdir = workdir or self.bridge_root
        interrupted_at = interrupted_at or self.now_iso()
        evidence = self.collect_evidence(
            workdir=workdir, head_before=head_before, head_after=head_after
        )
        pending_report_path = runtime_dir / f"pending-report-{command_id:03d}.md"
        canonical_report_path = report_path or (
            self.bridge_root / "projects" / project_id / "reports" / f"report-{command_id:03d}.md"
        )
        marker_status = getattr(run_result, "marker_result", None) or "NETWORK_INTERRUPTED"
        raw_exit_code = getattr(run_result, "exit_code", None)
        process_exit_code = raw_exit_code if isinstance(raw_exit_code, int) else 125
        termination_reason = getattr(run_result, "termination_reason", None) or "network_guard"
        journal_data: dict[str, Any] = {
            "schema_version": recovery_journal.SCHEMA_VERSION,
            "project_id": project_id,
            "command_id": command_id,
            "run_id": run_id,
            "claim_generation": claim_generation,
            "interrupted_at": interrupted_at,
            "interruption_kind": "network_guard",
            "interruption_reason_safe": reason,
            "claimed_at": claimed_at,
            "lease_expires_at": lease_expires_at,
            **evidence,
            "report_path": _evidence_path(canonical_report_path, self.bridge_root),
            "pending_report_path": _evidence_path(pending_report_path, self.bridge_root),
            "marker_status": marker_status,
            "process_exit_code": process_exit_code,
            "termination_reason": termination_reason,
            "remote_publish_pending": True,
            "journal_status": "pending",
        }
        journal_file = recovery_journal.journal_path(self.bridge_root, project_id, run_id)
        normalized_journal = recovery_journal.write_journal(journal_file, journal_data)
        safe_reason = str(normalized_journal.get("interruption_reason_safe") or "network interruption")
        recovery_reason = (
            f"network guard interrupted Codex run {run_id}; automatic rerun prohibited; {safe_reason}"
        )
        self.save_pending_report(runtime_dir, command_id, report_text, recovery_reason)

        def expected(current: dict[str, Any]) -> bool:
            return protocol_core.matches_execution_lease(
                current,
                run_id=identity.run_id,
                command_id=identity.command_id,
                claimed_generation=identity.claim_generation,
                project_id=identity.project_id,
            )

        def already(current: dict[str, Any]) -> bool:
            return protocol_core.matches_recovery_state(journal_data, current)

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            if current.get("project_id") == project_id:
                decision = self.prepare_recovery_decision(current, identity, reason=recovery_reason)
                updated = self.recovery_payload(
                    current,
                    decision,
                    last_execution_error="network guard interruption; manual recovery required",
                )
            else:
                # The legacy unit-test CAS seam supplies only the fields it
                # needs for this payload builder.  Real Durable Store calls
                # must satisfy the exact identity predicate above first.
                updated = dict(current)
                updated["status"] = "RECOVERY_REQUIRED"
                updated["generation"] = protocol_core.recovery_generation(
                    _state_int(current.get("generation"), 0) or 0
                )
                updated["active_run"] = None
                updated["worker_pid"] = None
                updated["recovery_reason"] = recovery_reason
                updated["last_execution_error"] = "network guard interruption; manual recovery required"
                updated["updated_at"] = self.now_iso()
            return {state_path: bridge_common.json_text(updated)}

        final_state = self.publish_cas(
            bridge_root=self.bridge_root,
            state_path=state_path,
            expected=expected,
            already_applied=already,
            payload_builder=payload,
            message=f"bridge: network recovery required for {project_id}",
        )
        acknowledged = False
        try:
            recovery_journal.update_journal(
                journal_file,
                remote_publish_pending=False,
                journal_status="reconciled",
                reconciled_at=self.now_iso(),
                reconciliation_reason="remote RECOVERY_REQUIRED publication confirmed",
            )
            acknowledged = True
        except recovery_journal.RecoveryJournalError as exc:
            print(
                f"[{self.now_iso()}] {project_id}: remote recovery published but local journal acknowledgement failed: {type(exc).__name__}",
                file=sys.stderr,
            )
        return {
            "state": final_state,
            "journal_path": journal_file,
            "pending_report_path": pending_report_path,
            "journal": {
                **normalized_journal,
                "remote_publish_pending": not acknowledged,
                "journal_status": "reconciled" if acknowledged else "pending",
            },
            "remote_recovery_cas_success": True,
            "remote_publish_pending": not acknowledged,
        }

    def _canonical_snapshot(
        self,
        state: Mapping[str, Any],
        identity: RunIdentity,
    ) -> CanonicalRunSnapshot:
        """Extract only the canonical facts accepted by the pure classifier."""

        if state.get("project_id") != identity.project_id:
            raise RecoveryServiceConflict("canonical project identity does not match the exact run")
        status = state.get("status")
        if not isinstance(status, str) or not status:
            raise RecoveryServiceConflict("canonical status is not exact")

        def required_int(key: str, minimum: int) -> int:
            value = _state_int(state.get(key), None)
            if value is None or value < minimum:
                raise RecoveryServiceConflict(f"canonical {key} is not exact")
            return value

        active_identity: RunIdentity | None = None
        active = state.get("active_run")
        if active is not None:
            if not isinstance(active, Mapping):
                raise RecoveryServiceConflict("canonical active_run is not exact")
            try:
                active_identity = RunIdentity(
                    project_id=identity.project_id,
                    command_id=active["command_id"],
                    run_id=active["run_id"],
                    claim_generation=active["claimed_generation"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RecoveryServiceConflict("canonical active_run identity is not exact") from exc
        return CanonicalRunSnapshot(
            project_id=identity.project_id,
            status=status,
            generation=required_int("generation", 0),
            latest_command=required_int("latest_command", 0),
            latest_report=required_int("latest_report", 0),
            active_identity=active_identity,
        )

    @staticmethod
    def _run_identity_from_metadata(metadata: Mapping[str, Any]) -> RunIdentity:
        try:
            return RunIdentity(
                project_id=metadata["project_id"],
                command_id=metadata["command_id"],
                run_id=metadata["run_id"],
                claim_generation=metadata["claim_generation"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryServiceConflict("typed evidence does not contain an exact run identity") from exc

    @staticmethod
    def _report_marker_state(report_text: str) -> EvidenceState:
        marker_values = [
            line.split(":", 1)[1].strip().upper()
            for line in report_text.splitlines()
            if line.strip().startswith("- marker_result:")
            and ":" in line
        ]
        if "VALID" in marker_values:
            return EvidenceState.YES
        if marker_values and all(value in {"MISSING", "INVALID", "FAILED", "BLOCKED", "N/A"} for value in marker_values):
            return EvidenceState.NO
        return EvidenceState.UNKNOWN

    @staticmethod
    def _report_outcome(report_text: str, expected: str) -> str:
        values = [
            line.split(":", 1)[1].strip().upper()
            for line in report_text.splitlines()
            if line.strip().startswith("- outcome:") and ":" in line
        ]
        if len(values) != 1 or values[0] != expected:
            raise RecoveryServiceConflict(
                "typed report outcome does not bind the pending sidecar"
            )
        return values[0]

    def _repository_for_project(
        self,
        project_id: str,
        config: Mapping[str, Any] | None,
    ) -> Path:
        candidate_config = config if config is not None else self.config
        if isinstance(candidate_config, Mapping):
            projects = candidate_config.get("projects")
            if isinstance(projects, Mapping):
                project_config = projects.get(project_id)
                if isinstance(project_config, Mapping):
                    workdir = project_config.get("workdir")
                    if isinstance(workdir, str) and workdir.strip():
                        candidate = Path(workdir).expanduser().resolve()
                        if candidate.is_dir():
                            return candidate
        return self.bridge_root

    def _reconcile_push_effect(
        self,
        effect: object,
        identity: RunIdentity,
        *,
        config: Mapping[str, Any] | None,
    ) -> PushReconciliation:
        """Reconcile one unconfirmed effect, or preserve an explicit unknown."""

        effect_id = getattr(effect, "effect_id", None)
        target_id = getattr(effect, "target_id", None)
        expected_object = getattr(effect, "expected_object", None)
        precondition_object = getattr(effect, "precondition_object", None)
        if not all(isinstance(value, str) for value in (effect_id, target_id, expected_object)):
            raise RecoveryServiceConflict("WAL push intent fields are not exact")
        if not isinstance(precondition_object, str):
            return PushReconciliation(
                effect_id=effect_id,
                target_id=target_id,
                expected_object=expected_object,
                state=PushResolutionState.UNKNOWN,
            )
        try:
            remote, target_ref = split_target_id(target_id)
            intent = GitPushIntent(
                identity=identity,
                effect_id=effect_id,
                remote=remote,
                target_ref=target_ref,
                expected_object=expected_object,
                precondition_object=precondition_object,
            )
        except (GitEffectError, TypeError, ValueError):
            return PushReconciliation(
                effect_id=effect_id,
                target_id=target_id,
                expected_object=expected_object,
                precondition_object=precondition_object,
                state=PushResolutionState.AMBIGUOUS,
            )
        try:
            if self.reconcile_push is not None:
                result = self.reconcile_push(intent)
            else:
                reader = GitLsRemoteRefReader(
                    self._repository_for_project(identity.project_id, config)
                )
                result = GitPushReconciler(reader).reconcile(intent)
        except (
            GitEffectError,
            OSError,
            subprocess.SubprocessError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            return PushReconciliation(
                effect_id=effect.effect_id,
                target_id=effect.target_id,
                expected_object=effect.expected_object,
                precondition_object=effect.precondition_object,
                state=PushResolutionState.UNKNOWN,
            )
        if not isinstance(result, PushReconciliation):
            raise RecoveryServiceConflict("Git reconciler returned a non-typed result")
        return result

    def _typed_envelope(
        self,
        evidence: pending_report.PendingReportEvidence,
        state: Mapping[str, Any],
        wal: RecoveryWal,
        *,
        config: Mapping[str, Any] | None,
    ) -> tuple[CanonicalRunSnapshot, RecoveryEnvelope]:
        metadata = evidence.metadata
        identity = self._run_identity_from_metadata(metadata)
        canonical = self._canonical_snapshot(state, identity)
        if canonical.latest_report >= identity.command_id:
            try:
                canonical_report = evidence.report_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RecoveryServiceConflict(
                    "canonical latest_report advanced without the exact report"
                ) from exc
            if hashlib.sha256(canonical_report.encode("utf-8")).hexdigest() != metadata["report_sha256"]:
                raise RecoveryServiceConflict(
                    "canonical latest_report does not contain the exact pending report"
                )
        materialized = [
            event
            for event in wal.events
            if event.kind.value == "REPORT_MATERIALIZED"
        ]
        publish_intents = [
            event
            for event in wal.events
            if event.kind.value == "REPORT_PUBLISH_INTENT"
        ]
        report_digest = hashlib.sha256(evidence.report_text.encode("utf-8")).hexdigest()
        if materialized and materialized[0].artifact_digest != report_digest:
            raise RecoveryServiceConflict("WAL report digest contradicts pending report integrity")
        if materialized and (
            not publish_intents
            or publish_intents[0].artifact_digest != report_digest
        ):
            raise RecoveryServiceConflict(
                "WAL report publication intent does not bind the exact report"
            )
        materialized_state = EvidenceState.YES if materialized else EvidenceState.UNKNOWN
        lifecycle = LifecycleEvidence(
            process_created=(
                EvidenceState.YES
                if any(event.kind.value == "PROCESS_CREATED" for event in wal.events)
                else EvidenceState.UNKNOWN
            ),
            process_exited=(
                EvidenceState.YES
                if any(event.kind.value == "PROCESS_EXITED" for event in wal.events)
                else EvidenceState.UNKNOWN
            ),
        )
        containment_intact = EvidenceState.UNKNOWN
        if any(event.kind.value == "CONTAINMENT_RECONCILED" for event in wal.events):
            containment_intact = EvidenceState.YES
        push_effects = wal.push_effects()
        if canonical.relation_to(identity) in {
            CanonicalRelation.REPORT_ALREADY_PUBLISHED,
            CanonicalRelation.STATE_ADVANCED,
        }:
            # Canonical history is authoritative; no external observation is
            # needed to supersede local evidence that can no longer win a CAS.
            reconciliations: tuple[PushReconciliation, ...] = ()
        else:
            reconciliations = tuple(
                self._reconcile_push_effect(effect, identity, config=config)
                for effect in push_effects
                if not effect.confirmed
            )
        egress_events = [
            event
            for event in wal.events
            if event.kind.value == "PROJECT_EGRESS_RECONCILED"
        ]
        if egress_events and egress_events[0].observed_object == "NO":
            project_egress = EvidenceState.NO
            unclassified_egress = EvidenceState.NO
            local_effects = EvidenceState.YES
        elif egress_events and egress_events[0].observed_object == "YES":
            project_egress = EvidenceState.YES
            unclassified_egress = EvidenceState.UNKNOWN
            local_effects = EvidenceState.YES
        elif push_effects:
            # A Git effect receipt is semantic evidence only for that exact
            # Git ref.  It must not be promoted into a claim about unrelated
            # project egress or other local effects.
            project_egress = EvidenceState.UNKNOWN
            unclassified_egress = EvidenceState.UNKNOWN
            local_effects = EvidenceState.UNKNOWN
        else:
            project_egress = EvidenceState.UNKNOWN
            unclassified_egress = EvidenceState.UNKNOWN
            local_effects = EvidenceState.UNKNOWN
        return canonical, RecoveryEnvelope(
            schema_version=1,
            identity=identity,
            command_kind=RecoveryCommandKind(metadata["kind"]),
            journal_disposition=JournalDisposition(metadata["journal_status"].upper()),
            lifecycle=lifecycle,
            report=ReportEvidence(
                final_marker_valid=self._report_marker_state(evidence.report_text),
                materialized=materialized_state,
                integrity_valid=(
                    EvidenceState.YES
                    if materialized_state is EvidenceState.YES
                    else EvidenceState.UNKNOWN
                ),
                outcome=self._report_outcome(
                    evidence.report_text,
                    str(metadata["outcome"]),
                ),
            ),
            containment=ContainmentEvidence(
                containment_intact=containment_intact,
                project_egress_observed=project_egress,
                unclassified_project_egress=unclassified_egress,
                local_effects_reconciled=local_effects,
            ),
            publication=PublicationEvidence(store_available=EvidenceState.YES),
            wal=wal,
            push_reconciliations=reconciliations,
        )

    def _transition_typed_recovery(
        self,
        evidence: pending_report.PendingReportEvidence,
        identity: RunIdentity,
        state: Mapping[str, Any],
        *,
        reason: str,
    ) -> str:
        """Retain uncertainty and make only the exact recovery CAS when safe."""

        if state.get("status") == "RECOVERY_REQUIRED":
            pending_report.mark_evidence_conflict_best_effort(
                evidence.metadata_path,
                "canonical RECOVERY_REQUIRED already exists; automatic resolution refused",
            )
            return "conflict"
        if _state_int(state.get("latest_report"), -1) >= identity.command_id:
            pending_report.mark_evidence_conflict_best_effort(
                evidence.metadata_path,
                "canonical history advanced while typed recovery evidence remained pending",
            )
            return "conflict"
        if not protocol_core.matches_execution_lease(
            state,
            run_id=identity.run_id,
            command_id=identity.command_id,
            claimed_generation=identity.claim_generation,
            source=str(evidence.metadata["source"]),
            project_id=identity.project_id,
        ):
            pending_report.mark_evidence_conflict_best_effort(
                evidence.metadata_path,
                "typed recovery evidence no longer matches the canonical lease",
            )
            return "conflict"

        state_path = evidence.state_path

        def expected(current: dict[str, Any]) -> bool:
            return protocol_core.matches_execution_lease(
                current,
                run_id=identity.run_id,
                command_id=identity.command_id,
                claimed_generation=identity.claim_generation,
                source=str(evidence.metadata["source"]),
                project_id=identity.project_id,
            )

        def already(current: dict[str, Any]) -> bool:
            return current.get("status") == "RECOVERY_REQUIRED"

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            decision = self.prepare_recovery_decision(
                current,
                RecoveryIdentity(
                    identity.project_id,
                    identity.command_id,
                    identity.run_id,
                    identity.claim_generation,
                ),
                reason=reason,
            )
            updated = self.recovery_payload(
                current,
                decision,
                last_execution_error="typed recovery evidence is incomplete or ambiguous; manual recovery required",
            )
            return {state_path: bridge_common.json_text(updated)}

        try:
            self.publish_cas(
                bridge_root=self.bridge_root,
                state_path=state_path,
                expected=expected,
                already_applied=already,
                payload_builder=payload,
                message=f"bridge: typed recovery required for {identity.project_id}",
            )
        except bridge_common.CASConflict:
            pending_report.mark_evidence_conflict_best_effort(
                evidence.metadata_path,
                "canonical state changed during typed recovery CAS",
            )
            return "conflict"
        except bridge_common.WorkerError:
            # A temporary Durable Store failure is not a reason to discard the
            # evidence; the next startup can retry the same recovery CAS.
            return "pending"
        pending_report.mark_evidence_conflict_best_effort(
            evidence.metadata_path,
            reason,
        )
        return "recovery"

    def _reconcile_typed_report(
        self,
        metadata_path: Path,
        *,
        config: Mapping[str, Any] | None,
    ) -> str:
        try:
            evidence = pending_report.load_evidence(self.bridge_root, metadata_path)
        except (OSError, pending_report.PendingReportError) as exc:
            pending_report.mark_evidence_conflict_best_effort(
                metadata_path,
                f"typed pending evidence validation failed: {type(exc).__name__}",
            )
            return "conflict"
        if evidence.metadata.get("evidence_mode") != "typed":
            return "ignored"
        identity = self._run_identity_from_metadata(evidence.metadata)
        try:
            state = bridge_common.load_json(evidence.state_path)
        except (OSError, bridge_common.WorkerError):
            # Canonical unavailability is a publication/reconciliation defer,
            # not positive evidence of a contradiction.
            return "pending"
        try:
            wal_path = pending_report.canonical_wal_path(
                self.bridge_root,
                identity.project_id,
                identity.run_id,
            )
            wal = DurableRunWal.read(wal_path, identity=identity).wal
            canonical, envelope = self._typed_envelope(
                evidence,
                state,
                wal,
                config=config,
            )
        except (OSError, DurableWalError, RecoveryEvidenceError, RecoveryServiceError) as exc:
            return self._transition_typed_recovery(
                evidence,
                identity,
                state,
                reason=f"typed recovery evidence cannot be safely classified: {type(exc).__name__}",
            )
        assessment = classify_recovery(canonical, envelope)
        if assessment.classification is RecoveryClassification.SUPERSEDED:
            pending_report.mark_evidence_status(
                evidence,
                status="superseded",
                reason=assessment.reason,
            )
            return "superseded"
        if assessment.classification in {
            RecoveryClassification.CONFLICT,
            RecoveryClassification.REQUIRE_RECOVERY,
        }:
            return self._transition_typed_recovery(
                evidence,
                identity,
                state,
                reason=assessment.reason,
            )
        if assessment.classification is RecoveryClassification.DEFER_PUBLICATION:
            return "pending"
        if assessment.classification in {
            RecoveryClassification.PUBLISH_COMPLETED_REPORT,
            RecoveryClassification.PUBLISH_INTERRUPTED_REPORT,
        }:
            return pending_report.reconcile_evidence(
                evidence,
                publish_cas=self.publish_cas,
            )
        if assessment.classification is RecoveryClassification.NO_ACTION:
            return "ignored"
        return self._transition_typed_recovery(
            evidence,
            identity,
            state,
            reason=assessment.reason,
        )

    def _mark_journal(self, path: Path, *, status: str, reason: str) -> None:
        try:
            recovery_journal.update_journal(
                path,
                remote_publish_pending=False,
                journal_status=status,
                reconciled_at=self.now_iso(),
                reconciliation_reason=reason,
            )
        except recovery_journal.RecoveryJournalError as exc:
            print(
                f"[{self.now_iso()}] recovery journal acknowledgement failed for {path.name}: {type(exc).__name__}",
                file=sys.stderr,
            )

    def _journal_alert_context(
        self,
        journal: Mapping[str, Any],
        *,
        alert_kind: str,
        reason: str,
        current_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = str(journal.get("project_id", "unknown-project"))
        command_id = _state_int(journal.get("command_id"), 0)
        run_id = str(journal.get("run_id", "unknown-run"))
        pending_raw = str(journal.get("pending_report_path", ""))
        pending_path = self.bridge_root / pending_raw if pending_raw else None
        state = current_state or {}
        return {
            "timestamp": self.now_iso(),
            "project_id": project_id,
            "command_id": command_id,
            "run_id": run_id,
            "alert_kind": alert_kind,
            "current_bridge_status": str(state.get("status", "unknown")),
            "original_status": "CODEX_RUNNING",
            "generation": _state_int(state.get("generation"), None),
            "generation_before": _state_int(journal.get("claim_generation"), None),
            "claim_generation": _state_int(journal.get("claim_generation"), None),
            "interruption_recovery_type": str(journal.get("interruption_kind", "deferred recovery")),
            "recovery_reason_safe": _safe_reason(reason or journal.get("interruption_reason_safe")),
            "codex_terminated": True,
            "worker_is_alive": True,
            "journal_saved": True,
            "pending_report_saved": pending_path.exists() if pending_path else None,
            "remote_recovery_cas_success": False,
            "remote_publish_pending": journal.get("remote_publish_pending"),
            "worktree_dirty": journal.get("worktree_dirty"),
            "local_commit_created": journal.get("local_commit_created"),
            "unpushed_commits_present": journal.get("unpushed_commits_present"),
            "external_side_effects_unknown": journal.get("external_side_effects_unknown"),
            "lease_expires_at": journal.get("lease_expires_at"),
            "claimed_at": journal.get("claimed_at"),
            "reconciliation_reason": journal.get("reconciliation_reason", ""),
        }

    def _reconcile_one_journal(self, journal_path: Path, journal: dict[str, Any]) -> bool:
        identity = RecoveryIdentity.from_journal(journal)
        project_root = (self.bridge_root / "projects").resolve()
        project_dir = (self.bridge_root / "projects" / identity.project_id).resolve()
        try:
            project_dir.relative_to(project_root)
        except ValueError:
            self._mark_journal(journal_path, status="conflict", reason="journal project_id is outside the Bridge project root")
            return True
        state_path = project_dir / "state.json"
        if not state_path.is_file():
            return False
        try:
            state = bridge_common.load_json(state_path)
        except bridge_common.WorkerError:
            return False
        report_path = project_dir / "reports" / f"report-{identity.command_id:03d}.md"
        if report_path.exists() or (_state_int(state.get("latest_report"), -1) or -1) >= identity.command_id:
            self._mark_journal(journal_path, status="superseded", reason="remote state already has a report or advanced latest_report")
            return True
        if protocol_core.matches_recovery_state(journal, state):
            self._mark_journal(journal_path, status="reconciled", reason="remote RECOVERY_REQUIRED already matches this interrupted run")
            return True
        if state.get("status") != "CODEX_RUNNING":
            status = "superseded" if state.get("status") in {"DONE", "FINAL_REPORT_READY"} else "conflict"
            self._mark_journal(journal_path, status=status, reason=f"remote canonical status is {state.get('status', 'unknown')}; recovery publication refused")
            return True
        if not protocol_core.matches_recovery_running_state(journal, state):
            self._mark_journal(journal_path, status="conflict", reason="remote generation or active_run identity no longer matches")
            return True
        reason = f"network guard interrupted Codex run {identity.run_id}; automatic rerun prohibited; {_safe_reason(journal.get('interruption_reason_safe'))}"

        def expected(current: dict[str, Any]) -> bool:
            return protocol_core.matches_recovery_running_state(journal, current)

        def already(current: dict[str, Any]) -> bool:
            return protocol_core.matches_recovery_state(journal, current)

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            decision = self.prepare_recovery_decision(current, identity, reason=reason)
            updated = self.recovery_payload(current, decision, last_execution_error="network guard interruption; manual recovery required")
            return {state_path: bridge_common.json_text(updated)}

        try:
            self.publish_cas(
                bridge_root=self.bridge_root,
                state_path=state_path,
                expected=expected,
                already_applied=already,
                payload_builder=payload,
                message=f"bridge: reconcile interrupted {identity.project_id} run {identity.run_id}",
            )
        except bridge_common.CASConflict:
            self._mark_journal(journal_path, status="conflict", reason="remote state changed during recovery CAS")
            return True
        except bridge_common.WorkerError as exc:
            print(f"[{self.now_iso()}] {identity.project_id}: deferred recovery still pending ({type(exc).__name__})", file=sys.stderr)
            return False
        self._mark_journal(journal_path, status="reconciled", reason="remote RECOVERY_REQUIRED reconciled with Protocol v2 CAS")
        print(f"[{self.now_iso()}] {identity.project_id}: deferred recovery reconciled for run {identity.run_id} (automatic rerun prohibited)")
        return True

    def pending_network_recovery_projects(self, *, project_id: str | None = None) -> set[str]:
        blocked: set[str] = set()
        for path in recovery_journal.pending_journal_paths(self.bridge_root, project_id=project_id):
            candidate = path.parent.parent.name
            try:
                journal = recovery_journal.read_journal(path)
            except recovery_journal.RecoveryJournalError:
                blocked.add(candidate)
                continue
            if journal.get("journal_status") == "pending" and bool(journal.get("remote_publish_pending")):
                blocked.add(candidate)
        return blocked

    def reconcile_pending_recoveries(self, *, project_id: str | None = None, config: dict[str, Any] | None = None) -> int:
        completed = 0
        for path in recovery_journal.pending_journal_paths(self.bridge_root, project_id=project_id):
            try:
                journal = recovery_journal.read_journal(path)
            except recovery_journal.RecoveryJournalError as exc:
                print(f"[{self.now_iso()}] invalid recovery journal {path.name}: {type(exc).__name__}", file=sys.stderr)
                self.emit_alert(self.bridge_root, config if config is not None else self.config, {"timestamp": self.now_iso(), "project_id": path.parent.parent.name, "command_id": 0, "run_id": path.stem, "alert_kind": "deferred_recovery_error", "current_bridge_status": "unknown", "original_status": "CODEX_RUNNING", "interruption_recovery_type": "deferred recovery journal inspection", "recovery_reason_safe": "local recovery journal cannot be safely read", "codex_terminated": None, "worker_is_alive": True, "journal_saved": True, "pending_report_saved": None, "remote_recovery_cas_success": False, "remote_publish_pending": None, "external_side_effects_unknown": True})
                continue
            if not journal.get("remote_publish_pending") or journal.get("journal_status") != "pending":
                continue
            try:
                if self._reconcile_one_journal(path, journal):
                    completed += 1
            except (OSError, ValueError, TypeError) as exc:
                print(f"[{self.now_iso()}] deferred recovery inspection failed for {path.name}: {type(exc).__name__}", file=sys.stderr)
                project_state_path = self.bridge_root / "projects" / str(journal.get("project_id", "")) / "state.json"
                self.emit_alert(self.bridge_root, config if config is not None else self.config, self._journal_alert_context(journal, alert_kind="deferred_recovery_error", reason="deferred recovery cannot be safely compensated", current_state=self._state_for_alert(project_state_path)))
            else:
                try:
                    reconciled = recovery_journal.read_journal(path)
                except recovery_journal.RecoveryJournalError:
                    reconciled = journal
                if reconciled.get("journal_status") == "conflict":
                    project_state_path = self.bridge_root / "projects" / str(journal.get("project_id", "")) / "state.json"
                    self.emit_alert(self.bridge_root, config if config is not None else self.config, self._journal_alert_context(reconciled, alert_kind="deferred_recovery_conflict", reason=str(reconciled.get("reconciliation_reason", "deferred recovery conflict")), current_state=self._state_for_alert(project_state_path)))
        return completed

    def _state_for_alert(self, state_path: Path) -> dict[str, Any]:
        try:
            return bridge_common.load_json(state_path)
        except Exception:
            return {}

    def reconcile_pending_reports(self, *, project_id: str | None = None, config: dict[str, Any] | None = None) -> int:
        typed_completed = 0
        blocked = self.pending_network_recovery_projects(project_id=project_id)
        for metadata_path in pending_report.metadata_paths(self.bridge_root, project_id):
            if metadata_path.parent.name in blocked:
                continue
            try:
                raw = bridge_common.load_json(metadata_path)
            except (OSError, bridge_common.WorkerError):
                continue
            if not isinstance(raw, Mapping) or raw.get("evidence_mode", "legacy") != "typed":
                continue
            try:
                result = self._reconcile_typed_report(
                    metadata_path,
                    config=config,
                )
            except (OSError, TypeError, ValueError, RecoveryServiceError) as exc:
                print(
                    f"[{self.now_iso()}] typed pending report reconciliation failed for "
                    f"{metadata_path.name}: {type(exc).__name__}",
                    file=sys.stderr,
                )
                continue
            if result in {"reconciled", "superseded"}:
                typed_completed += 1

        legacy_completed = pending_report.reconcile_pending_reports(
            self.bridge_root,
            project_id=project_id,
            blocked_project_ids=blocked,
            publish_cas=self.publish_cas,
            evidence_mode="legacy",
        )
        return typed_completed + legacy_completed


def _evidence_path(path: Path, bridge_root: Path) -> str:
    try:
        return git_store.relative_path(path, bridge_root)
    except ValueError:
        return str(path)
