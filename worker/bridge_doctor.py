"""Read-only Bridge Doctor operator surface."""

from __future__ import annotations

import state_roots

import argparse
import json
from pathlib import Path
from typing import Sequence

try:
    from .vnext_runtime.health_aggregator import HealthAggregator, HealthSnapshot, Severity
except ImportError:  # pragma: no cover - direct ``python worker/bridge_doctor.py`` support
    from vnext_runtime.health_aggregator import HealthAggregator, HealthSnapshot, Severity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge doctor", description="Read-only Bridge health diagnosis")
    parser.add_argument("--project", help="show one project plus the required host summary")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit deterministic machine-readable JSON")
    parser.add_argument("--state-root", "--bridge-root", dest="bridge_root", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def _human(snapshot: HealthSnapshot) -> str:
    lines = [f"Bridge Doctor: {snapshot.severity.value}", f"Worker: {snapshot.worker.get('severity', 'Attention')}"]
    coordinator = snapshot.coordinator
    lines.append(f"Coordinator: {coordinator.get('lifecycle', 'unavailable')} ({coordinator.get('active_run_count', 0)}/{coordinator.get('max_parallel_runs', 1)} slots)")
    if snapshot.warnings:
        lines.append("Host warnings: " + "; ".join(snapshot.warnings))
    for project in snapshot.projects:
        state = project.canonical_status or "unavailable"
        suffix = f"; wait={project.wait_reason}" if project.waiting else ""
        if project.active_run:
            run = project.active_run
            suffix += f"; run={run.run_id}; provider={run.provider or 'unavailable'}; duration={run.duration_seconds if run.duration_seconds is not None else 'unavailable'}s"
        if project.warnings:
            suffix += "; warning=" + ", ".join(project.warnings)
        lines.append(f"{project.project_id}: {project.severity.value}; state={state}; generation={project.generation}; command={project.latest_command}; report={project.latest_report}{suffix}")
    return "\n".join(lines) + "\n"


def diagnose(*, bridge_root: Path, project_id: str | None = None, config_path: Path | None = None) -> HealthSnapshot:
    return HealthAggregator(bridge_root, config_path=config_path).collect(project_id)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bridge_root = state_roots.resolve_state_root(args.bridge_root)
    snapshot = diagnose(bridge_root=bridge_root, project_id=args.project, config_path=args.config)
    if args.as_json:
        print(snapshot.to_json(), end="")
    else:
        print(_human(snapshot), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["diagnose", "main"]
