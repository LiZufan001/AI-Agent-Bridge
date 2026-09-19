"""Project-isolated Supervisor planning helpers.

This module deliberately models the current one-focus Supervisor architecture:
compact canonical accounting may cover many projects, but a pass deep-loads and
plans exactly one focus project.  The Worker remains authoritative for local
admission/resources/claim/execution; these helpers never model Worker capacity.

Protocol v2 remains the source of truth.  All selection/diagnostic state here is
non-canonical and may be recomputed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import git_store
import protocol_core
from supervisor_publication_gateway import validate_exact_command_for_publication


class PortfolioSchedulingError(RuntimeError):
    """Base error for conservative Supervisor planning failures."""


class SnapshotError(PortfolioSchedulingError):
    """Compact canonical state/fact input is unusable."""


class ContextIsolationError(PortfolioSchedulingError):
    """A focus planning context contains cross-project/invalid evidence."""


class SchedulingClass(str, Enum):
    RUNNING = "RUNNING"
    COMMAND_PENDING = "COMMAND_PENDING"
    PLAN_ELIGIBLE = "PLAN_ELIGIBLE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    RECOVERY_OR_FAILED = "RECOVERY_OR_FAILED"
    TERMINAL = "TERMINAL"


class PriorityBand(IntEnum):
    SAFETY_RECOVERY = 0
    OWNER_BLOCKER_FEEDBACK = 1
    CORRECTNESS_SECURITY = 2
    DEPENDENCY_UNBLOCKING = 3
    ROADMAP = 4
    DISCRETIONARY_POLISH = 5


_KNOWN_STATUSES = frozenset(
    {
        "COMMAND_READY",
        "CODEX_RUNNING",
        "REPORT_READY",
        "FINALIZING",
        "FINAL_REPORT_READY",
        "HUMAN_REQUIRED",
        "RECOVERY_REQUIRED",
        "FAILED",
        "DONE",
    }
)
_COMMAND_FILE_RE = re.compile(r"^command-(\d+)\.md$", re.IGNORECASE)
MANUAL_SOURCE = "manual_chatgpt"


def _bounded_project_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise SnapshotError("project id is missing or exceeds the bounded limit")
    return value.strip()


def _bounded_nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SnapshotError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class Level1Facts:
    """Small pre-derived ordering hints; never semantic body content."""

    recovery_authorized: bool = False
    material_owner_blocker: bool = False
    material_feedback_hint: bool = False
    correctness_hint: bool = False
    security_hint: bool = False
    dependency_unblocking: bool = False
    discretionary_polish: bool = False
    fairness_deferrals: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.fairness_deferrals, bool)
            or not isinstance(self.fairness_deferrals, int)
            or self.fairness_deferrals < 0
        ):
            raise SnapshotError("fairness_deferrals must be a non-negative integer")
        for name in (
            "recovery_authorized",
            "material_owner_blocker",
            "material_feedback_hint",
            "correctness_hint",
            "security_hint",
            "dependency_unblocking",
            "discretionary_polish",
        ):
            if not isinstance(getattr(self, name), bool):
                raise SnapshotError(f"{name} must be boolean")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "Level1Facts":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise SnapshotError("level-1 facts must be an object")
        allowed = {
            "recovery_authorized",
            "material_owner_blocker",
            "material_feedback_hint",
            "correctness_hint",
            "security_hint",
            "dependency_unblocking",
            "discretionary_polish",
            "fairness_deferrals",
        }
        if set(raw) - allowed:
            raise SnapshotError("unsupported level-1 fact")
        return cls(**dict(raw))


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """Compact non-body facts for one portfolio project."""

    project_id: str
    status: str
    generation: int
    latest_command: int
    latest_report: int
    last_reviewed_report: int | None = None
    facts: Level1Facts = field(default_factory=Level1Facts)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _bounded_project_id(self.project_id))
        if self.status not in _KNOWN_STATUSES:
            raise SnapshotError("unknown Protocol-v2 status")
        object.__setattr__(self, "generation", _bounded_nonnegative(self.generation, "generation"))
        object.__setattr__(self, "latest_command", _bounded_nonnegative(self.latest_command, "latest_command"))
        object.__setattr__(self, "latest_report", _bounded_nonnegative(self.latest_report, "latest_report"))
        if self.last_reviewed_report is not None:
            object.__setattr__(
                self,
                "last_reviewed_report",
                _bounded_nonnegative(self.last_reviewed_report, "last_reviewed_report"),
            )
        if not isinstance(self.facts, Level1Facts):
            raise SnapshotError("facts must be Level1Facts")

    @classmethod
    def from_state(
        cls,
        project_id: str,
        state: Mapping[str, Any],
        *,
        facts: Level1Facts | Mapping[str, Any] | None = None,
    ) -> "ProjectSnapshot":
        if not isinstance(state, Mapping):
            raise SnapshotError("canonical state must be an object")
        status = state.get("status")
        if not isinstance(status, str):
            raise SnapshotError("canonical status must be text")
        if "generation" not in state:
            raise SnapshotError("canonical state is missing generation")
        return cls(
            project_id=project_id,
            status=status,
            generation=state["generation"],
            latest_command=state.get("latest_command", 0),
            latest_report=state.get("latest_report", 0),
            last_reviewed_report=state.get("last_reviewed_report"),
            facts=facts if isinstance(facts, Level1Facts) else Level1Facts.from_mapping(facts),
        )

    @property
    def scheduling_class(self) -> SchedulingClass:
        return classify_snapshot(self)


StateLoader = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
FactsLoader = Callable[[str, Mapping[str, Any], Mapping[str, Any]], Level1Facts | Mapping[str, Any] | None]


def derive_portfolio_snapshot(
    projects: Mapping[str, Mapping[str, Any]],
    state_loader: StateLoader,
    *,
    facts_loader: FactsLoader | None = None,
) -> tuple[ProjectSnapshot, ...]:
    """Read compact canonical state/facts for every enabled portfolio entry."""

    if not isinstance(projects, Mapping):
        raise SnapshotError("projects must be an object")
    snapshots: list[ProjectSnapshot] = []
    for raw_project_id in sorted(projects, key=str):
        project_id = _bounded_project_id(raw_project_id)
        config = projects[raw_project_id]
        if not isinstance(config, Mapping):
            raise SnapshotError("project configuration must be an object")
        if not bool(config.get("enabled", True)):
            continue
        state = state_loader(project_id, config)
        if not isinstance(state, Mapping):
            raise SnapshotError("state loader returned a non-object")
        facts = facts_loader(project_id, config, state) if facts_loader else None
        snapshots.append(ProjectSnapshot.from_state(project_id, state, facts=facts))
    return tuple(snapshots)


def classify_snapshot(snapshot: ProjectSnapshot) -> SchedulingClass:
    mapping = {
        "CODEX_RUNNING": SchedulingClass.RUNNING,
        "COMMAND_READY": SchedulingClass.COMMAND_PENDING,
        "FINALIZING": SchedulingClass.COMMAND_PENDING,
        "REPORT_READY": SchedulingClass.PLAN_ELIGIBLE,
        "FINAL_REPORT_READY": SchedulingClass.PLAN_ELIGIBLE,
        "HUMAN_REQUIRED": SchedulingClass.HUMAN_REQUIRED,
        "RECOVERY_REQUIRED": SchedulingClass.RECOVERY_OR_FAILED,
        "FAILED": SchedulingClass.RECOVERY_OR_FAILED,
        "DONE": SchedulingClass.TERMINAL,
    }
    return mapping[snapshot.status]


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    """Small diagnostics policy; one-focus selection is an invariant, not a knob."""

    max_diagnostic_entries: int = 64

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_diagnostic_entries, bool)
            or not isinstance(self.max_diagnostic_entries, int)
            or self.max_diagnostic_entries < 1
        ):
            raise ValueError("max_diagnostic_entries must be an integer >= 1")


def priority_band(snapshot: ProjectSnapshot) -> PriorityBand:
    facts = snapshot.facts
    if facts.recovery_authorized:
        return PriorityBand.SAFETY_RECOVERY
    if facts.material_owner_blocker or facts.material_feedback_hint:
        return PriorityBand.OWNER_BLOCKER_FEEDBACK
    if facts.correctness_hint or facts.security_hint:
        return PriorityBand.CORRECTNESS_SECURITY
    if facts.dependency_unblocking:
        return PriorityBand.DEPENDENCY_UNBLOCKING
    if facts.discretionary_polish:
        return PriorityBand.DISCRETIONARY_POLISH
    return PriorityBand.ROADMAP


def priority_reason(snapshot: ProjectSnapshot) -> str:
    return {
        PriorityBand.SAFETY_RECOVERY: "safety_recovery_authorized",
        PriorityBand.OWNER_BLOCKER_FEEDBACK: "material_owner_blocker_or_feedback_hint",
        PriorityBand.CORRECTNESS_SECURITY: "correctness_or_security_hint",
        PriorityBand.DEPENDENCY_UNBLOCKING: "dependency_unblocking",
        PriorityBand.ROADMAP: "normal_goal_or_roadmap_work",
        PriorityBand.DISCRETIONARY_POLISH: "discretionary_polish",
    }[priority_band(snapshot)]


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    selected: tuple[ProjectSnapshot, ...]
    deferred: Mapping[str, str]
    next_fairness_deferrals: Mapping[str, int]


def select_projects(
    snapshots: Sequence[ProjectSnapshot],
    policy: PortfolioPolicy | None = None,
) -> SelectionDecision:
    """Choose exactly one deep focus; all other eligible work is deferred."""

    _ = policy or PortfolioPolicy()
    eligible = [s for s in snapshots if s.scheduling_class is SchedulingClass.PLAN_ELIGIBLE]

    # Importance class always wins. Fairness only breaks ties inside the same
    # substantive band; it never promotes polish over an Owner/safety boundary.
    ordered = sorted(
        eligible,
        key=lambda item: (
            int(priority_band(item)),
            -item.facts.fairness_deferrals,
            item.project_id,
        ),
    )
    selected = tuple(ordered[:1])
    selected_ids = {s.project_id for s in selected}
    deferred: dict[str, str] = {}
    next_counts: dict[str, int] = {}
    for item in ordered:
        if item.project_id in selected_ids:
            next_counts[item.project_id] = 0
        else:
            deferred[item.project_id] = "deferred_this_pass"
            next_counts[item.project_id] = item.facts.fairness_deferrals + 1
    return SelectionDecision(selected, deferred, next_counts)


@dataclass(frozen=True, slots=True)
class PlanningEvidence:
    project_id: str
    kind: str
    body: str

    def __post_init__(self) -> None:
        _bounded_project_id(self.project_id)
        if not isinstance(self.kind, str) or not self.kind or len(self.kind) > 64:
            raise ContextIsolationError("evidence kind is not bounded")
        if not isinstance(self.body, str):
            raise ContextIsolationError("evidence body must be text")


@dataclass(frozen=True, slots=True)
class ProjectPlanningContext:
    """Semantic/body context for exactly one selected focus project."""

    project_id: str
    project_root: Path
    canonical_state: Mapping[str, Any]
    mission: str
    supervisor_policy: str
    reports: tuple[PlanningEvidence, ...] = ()
    owner_actions: tuple[PlanningEvidence, ...] = ()
    feedback: tuple[PlanningEvidence, ...] = ()
    goals: tuple[PlanningEvidence, ...] = ()
    sources: tuple[PlanningEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _bounded_project_id(self.project_id))
        if not isinstance(self.project_root, Path):
            raise ContextIsolationError("planning context root must be a Path")
        collections = (self.reports, self.owner_actions, self.feedback, self.goals, self.sources)
        if any(not isinstance(items, tuple) for items in collections):
            raise ContextIsolationError("planning evidence collections must be immutable tuples")

    def validate_isolated(self, requested_project_id: str) -> None:
        requested = _bounded_project_id(requested_project_id)
        if self.project_id != requested:
            raise ContextIsolationError("planning context project identity mismatch")
        if not isinstance(self.canonical_state, Mapping):
            raise ContextIsolationError("planning context state must be an object")
        for evidence in (
            *self.reports,
            *self.owner_actions,
            *self.feedback,
            *self.goals,
            *self.sources,
        ):
            if not isinstance(evidence, PlanningEvidence) or evidence.project_id != requested:
                raise ContextIsolationError("planning evidence belongs to another project")
        if not isinstance(self.mission, str) or not isinstance(self.supervisor_policy, str):
            raise ContextIsolationError("planning context text is invalid")


class PlanningContextLoader(Protocol):
    def __call__(self, project_id: str) -> ProjectPlanningContext: ...


class IsolatedPlanningContextLoader:
    """Validate one focus context without imposing guessed body/time quotas."""

    def __init__(self, loader: PlanningContextLoader):
        self._loader = loader

    def load(self, project_id: str) -> ProjectPlanningContext:
        context = self._loader(_bounded_project_id(project_id))
        if not isinstance(context, ProjectPlanningContext):
            raise ContextIsolationError("context loader returned an invalid context")
        context.validate_isolated(project_id)
        return context


@dataclass(frozen=True, slots=True)
class CommandPlan:
    body: str
    kind: str = "EXECUTE"
    source: str = "scheduled_chatgpt"
    executor: Mapping[str, str] | None = None
    manual_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.body, str) or not self.body.strip():
            raise PortfolioSchedulingError("planner returned an empty command body")
        if self.kind not in protocol_core.COMMAND_KINDS:
            raise PortfolioSchedulingError("planner returned an unsupported command kind")
        if self.source not in protocol_core.COMMAND_SOURCES:
            raise PortfolioSchedulingError("planner returned an unsupported command source")
        if self.manual_request_id is not None and (
            not isinstance(self.manual_request_id, str)
            or not self.manual_request_id.strip()
            or len(self.manual_request_id) > 256
        ):
            raise PortfolioSchedulingError("manual_request_id must be bounded non-empty text")


@dataclass(frozen=True, slots=True)
class PublicationResult:
    published: bool
    reason: str
    state: Mapping[str, Any] | None = None


class PublicationGateway(Protocol):
    def publish(self, project_id: str, snapshot: ProjectSnapshot, plan: CommandPlan) -> PublicationResult: ...


@dataclass(frozen=True, slots=True)
class PassDiagnostics:
    pass_id: str
    timestamp: str
    projects_scanned: int
    classification_counts: Mapping[str, int]
    selected_project_ids: tuple[str, ...]
    publication_successes: tuple[str, ...]
    dispositions: Mapping[str, str]
    selection_reasons: Mapping[str, str]
    result: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_id": self.pass_id,
            "timestamp": self.timestamp,
            "projects_scanned": self.projects_scanned,
            "classification_counts": dict(self.classification_counts),
            "selected_project_ids": list(self.selected_project_ids),
            "publication_successes": list(self.publication_successes),
            "dispositions": dict(self.dispositions),
            "selection_reasons": dict(self.selection_reasons),
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class PortfolioPassResult:
    diagnostics: PassDiagnostics
    publications: Mapping[str, PublicationResult]


Planner = Callable[[ProjectPlanningContext], CommandPlan | None]
IntegrityCheck = Callable[[], bool]


class SupervisorPortfolioPass:
    """Perform compact accounting and deep-plan at most one focus project."""

    def __init__(
        self,
        *,
        policy: PortfolioPolicy | None = None,
        context_loader: IsolatedPlanningContextLoader,
        planner: Planner,
        publisher: PublicationGateway,
        integrity_check: IntegrityCheck | None = None,
    ) -> None:
        self.policy = policy or PortfolioPolicy()
        self.context_loader = context_loader
        self.planner = planner
        self.publisher = publisher
        self.integrity_check = integrity_check or (lambda: True)

    def run(
        self,
        snapshots: Sequence[ProjectSnapshot],
        *,
        pass_id: str,
        timestamp: str,
    ) -> PortfolioPassResult:
        if not isinstance(pass_id, str) or not pass_id or len(pass_id) > 128:
            raise ValueError("pass_id must be bounded text")
        if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 64:
            raise ValueError("timestamp must be bounded text")

        counts = {member.value: 0 for member in SchedulingClass}
        for snapshot in snapshots:
            counts[snapshot.scheduling_class.value] += 1

        if not self._integrity_is_trustworthy():
            return PortfolioPassResult(
                PassDiagnostics(
                    pass_id, timestamp, len(snapshots), counts, (), (), {}, {},
                    "control_plane_integrity_uncertain",
                ),
                {},
            )

        selection = select_projects(snapshots, self.policy)
        selected_ids = tuple(item.project_id for item in selection.selected)
        reasons = {item.project_id: priority_reason(item) for item in selection.selected}
        dispositions: dict[str, str] = dict(selection.deferred)
        publications: dict[str, PublicationResult] = {}
        successes: list[str] = []

        for snapshot in snapshots:
            if snapshot.project_id in dispositions or snapshot.project_id in selected_ids:
                continue
            dispositions[snapshot.project_id] = _compact_disposition(snapshot)

        if selection.selected:
            snapshot = selection.selected[0]
            if not self._integrity_is_trustworthy():
                dispositions[snapshot.project_id] = "control_plane_integrity_uncertain"
            else:
                try:
                    context = self.context_loader.load(snapshot.project_id)
                    plan = self.planner(context)
                    if plan is None:
                        dispositions[snapshot.project_id] = "reviewed_no_op"
                    else:
                        result = self.publisher.publish(snapshot.project_id, snapshot, plan)
                        publications[snapshot.project_id] = result
                        if result.published:
                            successes.append(snapshot.project_id)
                            dispositions[snapshot.project_id] = "planned_published"
                        else:
                            dispositions[snapshot.project_id] = _safe_reason(result.reason)
                except ContextIsolationError:
                    dispositions[snapshot.project_id] = "planning_context_isolated_or_invalid"
                except (PortfolioSchedulingError, SnapshotError):
                    dispositions[snapshot.project_id] = "project_local_planning_failure"
                except Exception:
                    dispositions[snapshot.project_id] = "project_local_publication_failure"

        result_name = (
            "published" if successes else
            "no_safe_publication" if dispositions else
            "no_op"
        )
        limit = self.policy.max_diagnostic_entries
        bounded = dict(sorted(dispositions.items())[:limit])
        return PortfolioPassResult(
            PassDiagnostics(
                pass_id,
                timestamp,
                len(snapshots),
                counts,
                selected_ids,
                tuple(successes),
                bounded,
                reasons,
                result_name,
            ),
            publications,
        )

    def _integrity_is_trustworthy(self) -> bool:
        try:
            return bool(self.integrity_check())
        except Exception:
            return False


def _compact_disposition(snapshot: ProjectSnapshot) -> str:
    return {
        SchedulingClass.RUNNING: "running_pending_worker",
        SchedulingClass.COMMAND_PENDING: "running_pending_worker",
        SchedulingClass.HUMAN_REQUIRED: "human_required",
        SchedulingClass.RECOVERY_OR_FAILED: "recovery_ambiguous",
        SchedulingClass.TERMINAL: "accounted_terminal",
        SchedulingClass.PLAN_ELIGIBLE: "deferred_this_pass",
    }[snapshot.scheduling_class]


def _safe_reason(value: str) -> str:
    allowed = {
        "cas_race",
        "command_pending",
        "conservative_no_op",
        "manual_supersede_rejected",
        "already_published",
        "publication_failed",
        "state_changed",
        "already_ready",
    }
    return value if isinstance(value, str) and value in allowed else "publication_failed"


class CasProjectPublisher:
    """Protocol-v2 CAS publisher retained for local/manual compatibility tests.

    Normal cloud Scheduled publication still uses the staged publication gateway.
    This class does not inspect or reserve Worker resources.
    """

    def __init__(self, bridge_root: Path):
        self.bridge_root = bridge_root

    def publish(self, project_id: str, snapshot: ProjectSnapshot, plan: CommandPlan) -> PublicationResult:
        if snapshot.status == "COMMAND_READY":
            if plan.source == MANUAL_SOURCE:
                return self.publish_manual_priority(project_id, snapshot, plan)
            return PublicationResult(False, "command_pending")

        project_root = self.bridge_root / "projects" / _bounded_project_id(project_id)
        state_path = project_root / "state.json"
        command_id = snapshot.latest_command + 1
        target_status = "FINALIZING" if plan.kind == "FINALIZE" or plan.source == "finalizer" else "COMMAND_READY"

        def expected(current: dict[str, Any]) -> bool:
            return (
                current.get("status") == snapshot.status
                and current.get("generation") == snapshot.generation
                and current.get("latest_command") == snapshot.latest_command
                and current.get("latest_report") == snapshot.latest_report
                and current.get("last_reviewed_report") == snapshot.last_reviewed_report
                and current.get("active_run") is None
            )

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            actual_command_id = int(current.get("latest_command", 0)) + 1
            if actual_command_id != command_id:
                raise protocol_core.ProtocolConflict("project command identity changed")
            command_text = _render_command(command_id, current, plan)
            updated = dict(current)
            updated["status"] = target_status
            updated["generation"] = protocol_core.publication_generation(int(current["generation"]))
            updated["latest_command"] = command_id
            updated["last_reviewed_report"] = int(current["latest_report"])
            updated["active_run"] = None
            updated["updated_at"] = _now_iso()
            command_path = project_root / "commands" / f"command-{command_id:03d}.md"
            if command_path.exists():
                raise protocol_core.ProtocolConflict("command file already exists")
            validate_exact_command_for_publication(
                command_text.encode("utf-8"),
                state=updated,
                target_status=target_status,
                command_id=command_id,
            )
            return {
                command_path: command_text,
                state_path: json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            }

        try:
            state = git_store.publish_cas(
                bridge_root=self.bridge_root,
                state_path=state_path,
                expected=expected,
                already_applied=lambda _current: False,
                payload_builder=payload,
                message=f"bridge: supervisor portfolio command {project_id}",
            )
        except (protocol_core.ProtocolConflict, git_store.CASConflict):
            return PublicationResult(False, "cas_race")
        except Exception:
            return PublicationResult(False, "publication_failed")
        return PublicationResult(state.get("latest_command") == command_id, "published", state)

    def publish_manual_priority(
        self,
        project_id: str,
        snapshot: ProjectSnapshot,
        plan: CommandPlan,
    ) -> PublicationResult:
        if plan.source != MANUAL_SOURCE or plan.kind != "EXECUTE" or snapshot.status != "COMMAND_READY":
            return PublicationResult(False, "manual_supersede_rejected")

        project_root = self.bridge_root / "projects" / _bounded_project_id(project_id)
        state_path = project_root / "state.json"
        try:
            initial = _load_state(state_path)
            existing_command_id = int(snapshot.latest_command) + 1
            if _manual_priority_already_applied(
                project_root, initial, snapshot=snapshot, plan=plan, command_id=existing_command_id
            ):
                return PublicationResult(True, "already_published", initial)
            initial_decision, _ = _validate_manual_target(project_root, initial)
            if not _snapshot_matches_state(initial, snapshot):
                return PublicationResult(False, "cas_race")
            if initial_decision.supersedes_command_id != snapshot.latest_command:
                return PublicationResult(False, "cas_race")
            command_id = _next_command_id(project_root, initial)
        except (OSError, ValueError, json.JSONDecodeError, protocol_core.ProtocolViolation):
            return PublicationResult(False, "manual_supersede_rejected")

        if command_id <= snapshot.latest_command:
            return PublicationResult(False, "manual_supersede_rejected")

        def expected(current: dict[str, Any]) -> bool:
            if not _snapshot_matches_state(current, snapshot):
                return False
            try:
                decision, _ = _validate_manual_target(project_root, current)
            except (OSError, ValueError, json.JSONDecodeError, protocol_core.ProtocolViolation):
                return False
            return decision.supersedes_command_id == snapshot.latest_command

        already_applied = False

        def already(current: dict[str, Any]) -> bool:
            nonlocal already_applied
            already_applied = _manual_priority_already_applied(
                project_root, current, snapshot=snapshot, plan=plan, command_id=command_id
            )
            return already_applied

        def payload(current: dict[str, Any]) -> dict[Path, str]:
            decision, _ = _validate_manual_target(project_root, current)
            if _next_command_id(project_root, current) != command_id:
                raise protocol_core.ProtocolConflict("project command identity changed during manual supersede")
            command_path = project_root / "commands" / f"command-{command_id:03d}.md"
            if command_path.exists():
                raise protocol_core.ProtocolConflict(f"refusing to overwrite existing command file: {command_path.name}")
            command_text = _render_command(
                command_id,
                current,
                plan,
                supersedes_command_id=decision.supersedes_command_id,
            )
            updated = dict(current)
            updated["status"] = "COMMAND_READY"
            updated["generation"] = decision.publication_generation
            updated["latest_command"] = command_id
            updated["active_run"] = None
            updated["updated_at"] = _now_iso()
            validate_exact_command_for_publication(
                command_text.encode("utf-8"),
                state=updated,
                target_status="COMMAND_READY",
                command_id=command_id,
            )
            return {
                command_path: command_text,
                state_path: json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            }

        try:
            state = git_store.publish_cas(
                bridge_root=self.bridge_root,
                state_path=state_path,
                expected=expected,
                already_applied=already,
                payload_builder=payload,
                message=f"bridge: supervisor manual supersede {project_id}",
            )
        except (protocol_core.ProtocolConflict, git_store.CASConflict):
            return PublicationResult(False, "cas_race")
        except (OSError, ValueError, json.JSONDecodeError, protocol_core.ProtocolViolation):
            return PublicationResult(False, "manual_supersede_rejected")
        except Exception:
            return PublicationResult(False, "publication_failed")
        reason = "already_published" if already_applied else "published"
        return PublicationResult(state.get("latest_command") == command_id, reason, state)


def _render_command(
    command_id: int,
    state: Mapping[str, Any],
    plan: CommandPlan,
    *,
    supersedes_command_id: int | None = None,
    expected_generation: int | None = None,
) -> str:
    metadata: dict[str, Any] = {
        "command_id": command_id,
        "source": plan.source,
        "based_on_report": int(state.get("latest_report", 0)),
        "expected_generation": int(state["generation"]) + 1 if expected_generation is None else expected_generation,
        "kind": plan.kind,
    }
    if plan.executor is not None:
        metadata["executor"] = dict(plan.executor)
    if plan.manual_request_id is not None:
        metadata["manual_request_id"] = plan.manual_request_id
    if supersedes_command_id is not None:
        metadata["supersedes_command_id"] = supersedes_command_id
    protocol_core.validate_command_metadata(metadata)
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    return f"<!-- bridge-command: {encoded} -->\n# Command {command_id:03d}\n\n{plan.body.rstrip()}\n"


def _load_state(state_path: Path) -> dict[str, Any]:
    value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("canonical state must be an object")
    return value


def _snapshot_matches_state(state: Mapping[str, Any], snapshot: ProjectSnapshot) -> bool:
    return (
        state.get("status") == snapshot.status
        and state.get("generation") == snapshot.generation
        and state.get("latest_command") == snapshot.latest_command
        and state.get("latest_report") == snapshot.latest_report
        and state.get("active_run") is None
    )


def _canonical_command_path(project_root: Path, state: Mapping[str, Any]) -> Path:
    command_id = state.get("latest_command")
    if isinstance(command_id, bool) or not isinstance(command_id, int) or command_id < 1:
        raise protocol_core.ProtocolViolation("canonical latest_command is invalid")
    return project_root / "commands" / f"command-{command_id:03d}.md"


def _validate_manual_target(
    project_root: Path, state: Mapping[str, Any]
) -> tuple[protocol_core.ManualSupersedeDecision, dict[str, Any]]:
    command_path = _canonical_command_path(project_root, state)
    if not command_path.is_file():
        raise protocol_core.ProtocolViolation("canonical scheduled command file is missing")
    latest_command = state.get("latest_command")
    report_path = project_root / "reports" / f"report-{int(latest_command):03d}.md"
    if report_path.exists():
        raise protocol_core.ProtocolConflict("manual supersede target already has a report")
    meta = protocol_core.parse_command_metadata(command_path.read_text(encoding="utf-8"))
    return protocol_core.manual_supersede_decision(state, meta), meta


def _next_command_id(project_root: Path, state: Mapping[str, Any]) -> int:
    values: list[int] = []
    for key in ("latest_command", "latest_report"):
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    commands_dir = project_root / "commands"
    if commands_dir.exists():
        for path in commands_dir.iterdir():
            match = _COMMAND_FILE_RE.fullmatch(path.name)
            if match:
                values.append(int(match.group(1)))
    return max(values, default=0) + 1


def _manual_priority_already_applied(
    project_root: Path,
    state: Mapping[str, Any],
    *,
    snapshot: ProjectSnapshot,
    plan: CommandPlan,
    command_id: int,
) -> bool:
    if plan.manual_request_id is None:
        return False
    if not (
        state.get("status") == "COMMAND_READY"
        and state.get("active_run") is None
        and state.get("generation") == snapshot.generation + 1
        and state.get("latest_command") == command_id
        and state.get("latest_report") == snapshot.latest_report
    ):
        return False
    command_path = project_root / "commands" / f"command-{command_id:03d}.md"
    if not command_path.is_file():
        return False
    expected = _render_command(
        command_id,
        state,
        plan,
        supersedes_command_id=snapshot.latest_command,
        expected_generation=snapshot.generation + 1,
    )
    try:
        return command_path.read_text(encoding="utf-8") == expected
    except OSError:
        return False


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


__all__ = [
    "CasProjectPublisher",
    "CommandPlan",
    "ContextIsolationError",
    "IsolatedPlanningContextLoader",
    "Level1Facts",
    "PlanningEvidence",
    "PortfolioPassResult",
    "PortfolioPolicy",
    "ProjectPlanningContext",
    "ProjectSnapshot",
    "PublicationResult",
    "SchedulingClass",
    "SelectionDecision",
    "SnapshotError",
    "SupervisorPortfolioPass",
    "classify_snapshot",
    "derive_portfolio_snapshot",
    "priority_band",
    "priority_reason",
    "select_projects",
]
