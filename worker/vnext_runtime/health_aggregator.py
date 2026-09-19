"""Read-only, bounded health projections for Doctor and Dashboard.

The aggregator deliberately has no dependency on the Worker command,
publication, claim, or recovery-resolution paths.  It reads canonical state
and local evidence, tags observations with their source, and can always be
rebuilt from those inputs.  Its output is diagnostic evidence, never an
execution authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class Severity(str, Enum):
    HEALTHY = "Healthy"
    ATTENTION = "Attention"
    ACTION_REQUIRED = "Action Required"

    @classmethod
    def worst(cls, values: list["Severity"]) -> "Severity":
        return max(values or [cls.HEALTHY], key=(
            lambda value: {cls.HEALTHY: 0, cls.ATTENTION: 1, cls.ACTION_REQUIRED: 2}[value]
        ))


class SourceKind(str, Enum):
    CANONICAL_STATE = "canonical_state"
    COMMAND_HISTORY = "command_history"
    REPORT_HISTORY = "report_history"
    WORKER_HEALTH = "worker_health"
    LAUNCHER_HEALTH = "launcher_health"
    COORDINATOR = "coordinator_runtime"
    RECOVERY = "recovery_runtime"
    PENDING_REPORT = "pending_report_runtime"
    SUPERVISOR = "supervisor_diagnostics"
    CI = "ci_diagnostics"
    SELF_MAINTENANCE = "self_maintenance_evidence"


@dataclass(frozen=True, slots=True)
class Provenance:
    source: SourceKind
    location: str
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveRunHealth:
    project_id: str
    run_id: str
    command_id: int | None
    provider: str | None
    model: str | None
    started_at: str | None
    duration_seconds: int | None
    lease_expires_at: str | None
    lease_consistent: bool
    source: Provenance
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryHealth:
    status: str
    run_id: str | None
    command_id: int | None
    reason: str | None
    unsafe_compensation: bool
    source: Provenance


@dataclass(frozen=True, slots=True)
class ProjectHealth:
    project_id: str
    enabled: bool
    severity: Severity
    canonical_status: str | None
    generation: int | None
    latest_command: int | None
    latest_report: int | None
    last_reviewed_report: int | None
    active_run: ActiveRunHealth | None
    waiting: bool
    wait_reason: str | None
    recovery: tuple[RecoveryHealth, ...] = ()
    warnings: tuple[str, ...] = ()
    sources: tuple[Provenance, ...] = ()
    owner_selected: bool = False
    owner_paused: bool = False
    last_execution_error: str | None = None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    generated_at: str
    severity: Severity
    worker: Mapping[str, Any]
    coordinator: Mapping[str, Any]
    supervisor: Mapping[str, Any]
    ci: Mapping[str, Any]
    projects: tuple[ProjectHealth, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


_SECRET_RE = re.compile(
    r"(?i)(?:appsecret|openid|https?://|authorization\s*:\s*(?:bearer\s+)?|cookie\s*:\s*|"
    r"(?:token|secret|password|api[_-]?key)\s*[=:]\s*|"
    r"(?:ghp_|github_pat_|sk-(?:proj-)?|xox[baprs]-)[^\s,;]+)[^\s,;]*"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_PROJECTS = 128
_MAX_ACTIVE_RUNS = 32
_MAX_RECOVERY = 32


def _safe_text(value: Any, limit: int = 256) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\x00", " ")).strip()
    if _SECRET_RE.search(text):
        return "[REDACTED]"
    return text[:limit] or None


def _safe_id(value: Any) -> str | None:
    text = _safe_text(value)
    return text if text and _SAFE_ID.fullmatch(text) else (text[:128] if text else None)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        return {str(key)[:64]: _json_safe(item) for key, item in list(value.items())[:32]}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value[:128]]
    return value


def _bounded_text(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise OSError("evidence_too_large")
    return raw.decode("utf-8")


def _read_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(_bounded_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "missing_or_corrupt"
    if not isinstance(value, dict):
        return None, "invalid_shape"
    return value, None


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(_bounded_text(path)), None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "missing_or_corrupt"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _duration(now: datetime, started: Any) -> int | None:
    parsed = _parse_time(started)
    if parsed is None:
        return None
    return max(0, min(2_592_000, int((now - parsed).total_seconds())))


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _location(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "external"


class HealthAggregator:
    """Build a bounded snapshot without changing any input or workflow state."""

    def __init__(
        self,
        bridge_root: Path,
        *,
        runtime_root: Path | None = None,
        config: Mapping[str, Any] | None = None,
        config_path: Path | None = None,
        now: datetime | None = None,
        heartbeat_timeout_seconds: int = 120,
    ) -> None:
        self.bridge_root = Path(bridge_root).resolve()
        self.runtime_root = Path(runtime_root or self.bridge_root / "worker" / "runtime").resolve()
        self.config_error = None
        self.config = dict(config) if config is not None else self._load_config(config_path)
        self.now = (now or datetime.now(timezone.utc)).astimezone()
        self.heartbeat_timeout_seconds = max(1, int(heartbeat_timeout_seconds))

    def _load_config(self, config_path: Path | None) -> dict[str, Any]:
        candidates = [config_path] if config_path else [
            self.bridge_root / "worker" / "config.local.json",
            self.bridge_root / "worker" / "config.example.json",
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            value, _ = _read_object(Path(candidate))
            if value is None and (config_path is not None or Path(candidate).exists()):
                self.config_error = "runtime configuration unavailable"
                return {}
            if value is not None:
                if Path(candidate).name == "config.example.json":
                    self.config_error = "example configuration in use"
                try:
                    # Exactly the same host overlay as the Worker, with no network IO.
                    import remote_project_registry
                    return remote_project_registry.load_runtime_config(Path(candidate), self.bridge_root)
                except Exception:
                    self.config_error = "runtime configuration unavailable"
                    return {}

        return {}

    def collect(self, project_id: str | None = None) -> HealthSnapshot:
        worker_path = self.runtime_root / "worker-health.json"
        launcher_path = self.runtime_root / "launcher-health.json"
        worker, worker_error = _read_object(worker_path)
        launcher, launcher_error = _read_object(launcher_path)
        worker = worker or {}
        launcher = launcher or {}
        projects_cfg = self.config.get("projects", {})
        if not isinstance(projects_cfg, Mapping):
            projects_cfg = {}
        ids = sorted(
            str(key) for key, value in projects_cfg.items()
            if _PROJECT_ID.fullmatch(str(key)) and isinstance(value, Mapping) and bool(value.get("enabled", True))
        )
        portfolio, portfolio_error = _read_object(self.bridge_root / "supervisor" / "portfolio.json")
        entries = (portfolio or {}).get("projects", [])
        self.portfolio = {v["project_id"]: v for v in entries[:128]
                          if isinstance(v, dict) and _PROJECT_ID.fullmatch(str(v.get("project_id", "")))} if isinstance(entries, list) else {}
        ids = sorted(set(ids) | {k for k,v in self.portfolio.items() if v.get("owner_selected") is True})
        if not ids and not projects_cfg and not self.portfolio and not self.config_error:
            ids = sorted(path.parent.name for path in (self.bridge_root / "projects").glob("*/state.json"))
        if project_id is not None:
            ids = [project_id] if _PROJECT_ID.fullmatch(project_id) and (project_id in ids or (self.bridge_root / "projects" / project_id / "state.json").exists()) else []
        registry, registry_error = _read_json(self.runtime_root / "resource-registry.json")
        reservations = registry if isinstance(registry, list) else []
        if not reservations and isinstance(worker.get("active_runs"), list):
            reservations = [item for item in worker["active_runs"] if isinstance(item, Mapping)]
        max_parallel = self._max_parallel()
        bounded_reservations = [item for item in reservations[:max_parallel] if isinstance(item, Mapping)]
        coordinator = {
            "lifecycle": _safe_id(worker.get("coordinator_lifecycle")) or "unavailable",
            "max_parallel_runs": max_parallel,
            "active_run_count": min(len(bounded_reservations), max_parallel),
            "active_runs": [self._reservation(item) for item in bounded_reservations],
            "source": _json_safe(Provenance(SourceKind.COORDINATOR, _location(self.bridge_root, self.runtime_root / "resource-registry.json"), _safe_text(worker.get("updated_at")))),
        }
        host_warnings: list[str] = []
        if self.config_error:
            host_warnings.append(self.config_error)
        worker_severity = self._heartbeat_severity(worker, worker_error, "worker")
        launcher_severity = self._heartbeat_severity(launcher, launcher_error, "launcher")
        if worker_error:
            host_warnings.append("worker health evidence unavailable or corrupt")
        if launcher_error:
            host_warnings.append("launcher health evidence unavailable or corrupt")
        if registry_error and self._max_parallel() > 1:
            host_warnings.append("coordinator resource registry unavailable")
        coordinator["heartbeat"] = self._heartbeat(worker, worker_error)
        coordinator["severity"] = Severity.worst([worker_severity, Severity.ATTENTION if coordinator["lifecycle"] != "RUNNING" or (registry_error and max_parallel > 1) else Severity.HEALTHY])
        if not coordinator["heartbeat"]["fresh"]:
            host_warnings.append("worker heartbeat is stale or unavailable")
        if not self._heartbeat(launcher, launcher_error)["fresh"]:
            host_warnings.append("launcher heartbeat is stale or unavailable")
        supervisor = self._optional_diagnostics(
            (self.runtime_root / "supervisor-diagnostics.json", self.runtime_root / "supervisor-health.json"),
            SourceKind.SUPERVISOR,
            "no bounded diagnostic source configured",
        )
        ci = self._optional_diagnostics(
            (self.runtime_root / "ci-status.json", self.runtime_root / "ci-health.json"),
            SourceKind.CI,
            "no bounded CI diagnostic source configured",
        )
        projects = tuple(self._project(str(pid), worker, projects_cfg.get(pid, {})) for pid in ids[:_MAX_PROJECTS])
        if (worker_severity is Severity.ATTENTION or launcher_severity is Severity.ATTENTION) and any(item.active_run for item in projects):
            # A stale host heartbeat while a lease is active is materially
            # unsafe: an operator must reconcile liveness before proceeding.
            if worker_severity is Severity.ATTENTION:
                worker_severity = Severity.ACTION_REQUIRED
            if launcher_severity is Severity.ATTENTION:
                launcher_severity = Severity.ACTION_REQUIRED
        coordinator["severity"] = Severity.worst([coordinator["severity"], worker_severity])
        if worker.get("last_self_maintenance_guard_status") == "rejected":
            host_warnings.append("self-maintenance protected-path guard rejected the last run")
        severity = Severity.worst([Severity.ATTENTION if host_warnings else Severity.HEALTHY, worker_severity, launcher_severity, coordinator["severity"], *(item.severity for item in projects)])
        worker_out = {
            "severity": worker_severity,
            "uptime_seconds": _duration(self.now, worker.get("worker_started_at")),
            "control_version": worker.get("owner_console_control_version") if type(worker.get("owner_console_control_version")) is int else None,
            "execution_mode": _safe_id(worker.get("execution_mode")),
            "execution_reason": _safe_id(worker.get("execution_reason")),
            "execution_control_status": _safe_id(worker.get("execution_control_status")),
            "heartbeat": self._heartbeat(worker, worker_error),
            "process": {key: (_safe_text(worker.get(key)) if key in {"host", "updated_at"} else worker.get(key)) for key in ("host", "pid", "worker_pid", "updated_at") if key in worker},
            "launcher": {
                "severity": launcher_severity,
                "heartbeat": self._heartbeat(launcher, launcher_error),
                "process": {key: (_safe_text(launcher.get(key)) if key in {"host", "updated_at"} else launcher.get(key)) for key in ("host", "pid", "launcher_pid", "worker_pid", "updated_at") if key in launcher},
                "source": _json_safe(Provenance(SourceKind.LAUNCHER_HEALTH, _location(self.bridge_root, launcher_path), _safe_text(launcher.get("updated_at")))),
            },
            "warnings": host_warnings,
            "self_maintenance": {key: worker[key] for key in (
                "last_self_maintenance_preflight_at", "last_self_maintenance_preflight_project",
                "last_self_maintenance_preflight_command_id", "last_self_maintenance_preflight_status",
                "last_self_maintenance_preflight_reason", "last_self_maintenance_guard_at",
                "last_self_maintenance_guard_status", "last_self_maintenance_guard_reason",
                "last_self_maintenance_guard_violations") if key in worker},
            "source": _json_safe(Provenance(SourceKind.WORKER_HEALTH, _location(self.bridge_root, worker_path), _safe_text(worker.get("updated_at")))),
        }
        return HealthSnapshot(
            generated_at=self.now.isoformat(timespec="seconds"),
            severity=severity,
            worker=worker_out,
            coordinator=coordinator,
            supervisor=supervisor,
            ci=ci,
            projects=projects,
            warnings=tuple(host_warnings),
        )

    def _max_parallel(self) -> int:
        runtime = self.config.get("runtime", {})
        value = self.config.get("max_parallel_runs", runtime.get("max_parallel_runs", 1) if isinstance(runtime, Mapping) else 1)
        parsed = _int(value)
        return max(1, min(parsed or 1, _MAX_ACTIVE_RUNS))

    def _heartbeat_severity(self, data: Mapping[str, Any], error: str | None, label: str) -> Severity:
        if error or not data:
            return Severity.ATTENTION
        heartbeat = _parse_time(data.get("last_poll_at") or data.get("updated_at") or data.get("worker_started_at"))
        if heartbeat is None:
            return Severity.ATTENTION
        age = (self.now - heartbeat).total_seconds()
        if age < -60 or age > self.heartbeat_timeout_seconds:
            return Severity.ATTENTION
        return Severity.HEALTHY

    def _heartbeat(self, data: Mapping[str, Any], error: str | None) -> dict[str, Any]:
        timestamp = data.get("last_poll_at") or data.get("updated_at")
        parsed = _parse_time(timestamp)
        age = None if parsed is None else max(0, int((self.now - parsed).total_seconds()))
        return {"available": not bool(error) and parsed is not None, "last_seen_at": _safe_text(timestamp), "age_seconds": age,
                "fresh": not error and parsed is not None and -60 <= (self.now - parsed).total_seconds() <= self.heartbeat_timeout_seconds,
                "timeout_seconds": self.heartbeat_timeout_seconds}

    def _reservation(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "project_id": _safe_id(item.get("project_id")),
            "run_id": _safe_id(item.get("run_id")),
            "provider": "codex",
            "started_at": _safe_text(item.get("acquired_at")),
            "source": _json_safe(Provenance(SourceKind.COORDINATOR, "worker/runtime/resource-registry.json")),
        }

    def _optional_diagnostics(self, candidates: tuple[Path, ...], source: SourceKind, unavailable_reason: str) -> dict[str, Any]:
        allowed = {
            "last_pass_at", "last_pass_id", "last_result", "selected_projects", "deferred_projects",
            "noop_reason", "no_op_reason", "defer_reason", "cas_race", "status", "updated_at",
        }
        corrupt = False
        for path in candidates:
            data, error = _read_object(path)
            if data is None:
                corrupt = corrupt or path.exists()
                continue
            projected = {key: _json_safe(data[key]) for key in sorted(set(data) & allowed)}
            projected["available"] = True
            projected["source"] = _json_safe(Provenance(source, _location(self.bridge_root, path), _safe_text(data.get("updated_at") or data.get("last_pass_at"))))
            return projected
        return {"available": False, "reason": "diagnostic source is corrupt" if corrupt else unavailable_reason}

    def _project(self, project_id: str, worker: Mapping[str, Any], cfg: Any) -> ProjectHealth:
        state_path = self.bridge_root / "projects" / project_id / "state.json"
        state, state_error = _read_object(state_path)
        state = state or {}
        sources = [Provenance(SourceKind.CANONICAL_STATE, _location(self.bridge_root, state_path), _safe_text(state.get("updated_at")))]
        warnings: list[str] = []
        severities: list[Severity] = []
        enabled = bool(cfg.get("enabled", True)) if isinstance(cfg, Mapping) else True
        if state_error:
            return ProjectHealth(project_id, enabled, Severity.ACTION_REQUIRED, None, None, None, None, None, None, False, None, warnings=("canonical state is missing or corrupt",), sources=tuple(sources))
        status = _safe_id(state.get("status"))
        generation = _int(state.get("generation"))
        latest_command = _int(state.get("latest_command"))
        latest_report = _int(state.get("latest_report"))
        reviewed = _int(state.get("last_reviewed_report"))
        if status not in {"COMMAND_READY", "CODEX_RUNNING", "REPORT_READY", "FINALIZING", "FINAL_REPORT_READY", "HUMAN_REQUIRED", "RECOVERY_REQUIRED", "FAILED", "DONE"}:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append("canonical status is unknown")
        if status in {"HUMAN_REQUIRED", "RECOVERY_REQUIRED", "FAILED"}:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append(status)
        active = self._active_run(project_id, state, worker, sources)
        if status == "CODEX_RUNNING" and active is None:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append("CODEX_RUNNING has no valid active lease")
        if active is not None and not active.lease_consistent:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append("active lease identity or expiry is inconsistent")
        if status != "CODEX_RUNNING" and state.get("active_run") is not None:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append("active lease exists outside CODEX_RUNNING")
        recovery = self._recovery(project_id, sources)
        if recovery:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.extend("recovery evidence: " + (item.reason or item.status) for item in recovery)
        wait_project = worker.get("resource_wait_project")
        waiting = wait_project == project_id
        wait_reason = _safe_text(worker.get("resource_wait_reason")) if waiting else None
        if waiting:
            severities.append(Severity.ATTENTION)
            warnings.append("waiting" + (f": {wait_reason}" if wait_reason else ""))
        portfolio_entry = self.portfolio.get(project_id, {})
        error = _safe_text(state.get("last_execution_error"))
        if error and active is None:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append(error)
        if active and not self._heartbeat(worker, None)["fresh"]:
            severities.append(Severity.ACTION_REQUIRED)
            warnings.append("active run liveness unconfirmed: worker heartbeat stale")
        return ProjectHealth(project_id, enabled, Severity.worst(severities), status, generation, latest_command, latest_report, reviewed, active, waiting, wait_reason, tuple(recovery), tuple(dict.fromkeys(warnings)), tuple(sources),
                             owner_selected=portfolio_entry.get("owner_selected") is True,
                             owner_paused=portfolio_entry.get("owner_paused") is True,
                             last_execution_error=error)

    def _active_run(self, project_id: str, state: Mapping[str, Any], worker: Mapping[str, Any], sources: list[Provenance]) -> ActiveRunHealth | None:
        raw = state.get("active_run")
        if not isinstance(raw, Mapping):
            return None
        run_id = _safe_id(raw.get("run_id"))
        if not run_id:
            return None
        claimed = _int(raw.get("claimed_generation"))
        consistent = raw.get("project_id", project_id) == project_id and raw.get("command_id") is not None
        lease = _parse_time(raw.get("lease_expires_at"))
        if raw.get("lease_expires_at") is not None and lease is None:
            consistent = False
        if lease is not None and lease <= self.now:
            consistent = False
        if claimed is not None and _int(state.get("generation")) != claimed:
            consistent = False
        if _int(raw.get("command_id")) != _int(state.get("latest_command")):
            consistent = False
        source = Provenance(SourceKind.CANONICAL_STATE, sources[0].location, sources[0].observed_at)
        executor = raw.get("executor") if isinstance(raw.get("executor"), Mapping) else {}
        return ActiveRunHealth(project_id, run_id, _int(raw.get("command_id")), _safe_id(raw.get("provider")) or "codex", _safe_id(raw.get("model") or executor.get("model")), _safe_text(raw.get("claimed_at") or raw.get("started_at") or raw.get("start_time")), _duration(self.now, raw.get("claimed_at") or raw.get("started_at") or raw.get("start_time")), _safe_text(raw.get("lease_expires_at")), consistent, source, _safe_id(raw.get("reasoning_effort") or executor.get("reasoning_effort")))

    def _recovery(self, project_id: str, sources: list[Provenance]) -> tuple[RecoveryHealth, ...]:
        root = self.runtime_root / project_id / "recovery"
        result: list[RecoveryHealth] = []
        if root.is_dir():
            paths = sorted(root.glob("*.json"), key=lambda path: path.name.casefold())[:_MAX_RECOVERY]
            for path in paths:
                data, error = _read_object(path)
                provenance = Provenance(SourceKind.RECOVERY, _location(self.bridge_root, path), _safe_text((data or {}).get("interrupted_at")))
                if error:
                    result.append(RecoveryHealth("corrupt", None, None, "recovery evidence is corrupt", True, provenance))
                    continue
                status = _safe_id(data.get("journal_status")) or "unknown"
                if status in {"reconciled", "superseded"}:
                    continue
                unsafe = status in {"pending", "conflict"} or bool(data.get("external_side_effects_unknown"))
                result.append(RecoveryHealth(status, _safe_id(data.get("run_id")), _int(data.get("command_id")), _safe_text(data.get("interruption_reason_safe") or data.get("reconciliation_reason")), unsafe, provenance))
        for path in sorted((self.runtime_root / project_id).glob("pending-report-*.meta.json"), key=lambda item: item.name.casefold())[:_MAX_RECOVERY]:
            data, error = _read_object(path)
            if error or not isinstance(data, Mapping):
                result.append(RecoveryHealth("pending_report_corrupt", None, None, "pending report evidence is corrupt", True, Provenance(SourceKind.PENDING_REPORT, _location(self.bridge_root, path))))
            elif data.get("journal_status") == "pending" or data.get("remote_publish_pending") is True:
                result.append(RecoveryHealth("pending_report", _safe_id(data.get("run_id")), _int(data.get("command_id")), "pending report publication evidence", True, Provenance(SourceKind.PENDING_REPORT, _location(self.bridge_root, path), _safe_text(data.get("created_at")))))
        return tuple(result[:_MAX_RECOVERY])


def aggregate_health(bridge_root: Path, **kwargs: Any) -> HealthSnapshot:
    """Convenience entry point used by Doctor, Dashboard, and tests."""
    return HealthAggregator(bridge_root, **kwargs).collect()


__all__ = [
    "ActiveRunHealth", "HealthAggregator", "HealthSnapshot", "ProjectHealth",
    "Provenance", "RecoveryHealth", "Severity", "SourceKind", "aggregate_health",
]
