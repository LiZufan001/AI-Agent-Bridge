"""Deterministic dependency-direction and authority checks for vNext.

This is deliberately a small AST checker rather than a second runtime
framework.  The rules are explicit because the dependency direction is an
architectural contract, not something that can be inferred safely from names
or import order alone.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ArchitectureViolation:
    path: str
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


_PROTOCOL_FORBIDDEN = (
    "git_store", "executor", "codex_lifecycle", "bridge_alerts",
    "bridge_doctor", "bridge_dashboard", "vnext_runtime.health_aggregator",
    "vnext_runtime.arbitration", "vnext_runtime.guards", "subprocess",
    "urllib", "smtplib", "socket", "requests",
)
_STORE_FORBIDDEN = ("executor", "codex_lifecycle", "vnext_runtime.providers")
_COORDINATOR_FORBIDDEN = (
    "executor", "codex_lifecycle", "vnext_runtime.providers.executors.codex",
    "bridge_worker", "bridge_alerts", "bridge_doctor", "bridge_dashboard",
    "vnext_runtime.health_aggregator", "recovery_resolution", "git_store",
    "protocol_core", "subprocess", "urllib", "smtplib",
)
_RECOVERY_FORBIDDEN = (
    "executor", "codex_lifecycle", "vnext_runtime.providers",
    "bridge_worker", "bridge_worker_hardened",
)
_RECOVERY_MODULES = {
    "vnext_runtime.services.recovery",
    "vnext_runtime.recovery_evidence",
}
_RECOVERY_EVIDENCE_FORBIDDEN = (
    "git_store", "pending_report", "recovery_journal", "recovery_resolution",
    "protocol_core", "vnext_runtime.services.recovery", "subprocess", "socket",
    "urllib", "requests", "http", "smtplib",
)
_RECOVERY_EVIDENCE_SIDE_EFFECT_CALLS = {
    "Popen", "run", "check_call", "check_output", "urlopen", "request",
    "open", "read_text", "read_bytes", "write_text", "write_bytes",
    "publish_cas", "execute", "run_codex", "codex_run",
}
_OBSERVABILITY_FORBIDDEN = (
    "git_store", "bridge_worker", "bridge_worker_hardened", "executor",
    "codex_lifecycle", "recovery_resolution", "pending_report", "bridge_manual",
    "bridge_cli", "vnext_runtime.services.recovery", "vnext_runtime.arbitration",
)
_OBSERVABILITY_MUTATORS = {
    "publish_cas", "claim_command", "resolve_recovery", "rerun", "execute",
    "finalize", "transition", "acquire_lease", "release_lease",
}
_RECOVERY_EXECUTOR_CALLS = {"execute", "run_codex", "codex_run", "Popen"}
_SCHEDULER_SYMBOLS = {
    "HostResourceArbiter", "paths_conflict", "canonicalize_path", "git_target_identity",
}
_STATE_WRITER_MODULES = {
    "git_store", "bridge_worker", "bridge_manual", "pending_report",
    "recovery_resolution", "bridge_worker_hardened",
}


def _module_name(source_root: Path, path: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    return ".".join(relative.parts)


def _matches(module: str, family: str) -> bool:
    return module == family or module.startswith(family + ".")


def _resolve_import(module: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = module.split(".")[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level <= len(package) + 1 else []
    return ".".join((*base, *(node.module or "").split("."))).strip(".")


def _imports(tree: ast.AST, module: str) -> Iterable[tuple[str, ast.AST]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node
        elif isinstance(node, ast.ImportFrom):
            yield _resolve_import(module, node), node


def _path_label(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _violation(root: Path, path: Path, node: ast.AST, rule: str, detail: str) -> ArchitectureViolation:
    return ArchitectureViolation(_path_label(root, path), getattr(node, "lineno", 1), rule, detail)


def _check_module_rules(root: Path, path: Path, module: str, tree: ast.AST) -> list[ArchitectureViolation]:
    violations: list[ArchitectureViolation] = []
    families: tuple[str, tuple[str, ...], str] | None = None
    if module == "protocol_core":
        families = (module, _PROTOCOL_FORBIDDEN, "protocol-kernel-purity")
    elif module == "git_store":
        families = (module, _STORE_FORBIDDEN, "durable-store-purity")
    elif module == "vnext_runtime.coordinator":
        families = (module, _COORDINATOR_FORBIDDEN, "coordinator-boundary")
    elif module in _RECOVERY_MODULES:
        families = (module, _RECOVERY_FORBIDDEN, "recovery-no-executor")
    elif module in {"vnext_runtime.health_aggregator", "bridge_doctor", "bridge_dashboard"}:
        families = (module, _OBSERVABILITY_FORBIDDEN, "observability-read-only")

    if families is not None:
        _, forbidden, rule = families
        for imported, node in _imports(tree, module):
            for family in forbidden:
                if _matches(imported, family):
                    violations.append(_violation(root, path, node, rule, f"forbidden dependency {imported!r}"))
                    break

    if module == "vnext_runtime.recovery_evidence":
        for imported, node in _imports(tree, module):
            for family in _RECOVERY_EVIDENCE_FORBIDDEN:
                if _matches(imported, family):
                    violations.append(
                        _violation(
                            root,
                            path,
                            node,
                            "recovery-classifier-purity",
                            f"pure classifier cannot depend on {imported!r}",
                        )
                    )
                    break

    if module == "vnext_runtime.providers.executors.codex":
        for imported, node in _imports(tree, module):
            if _matches(imported, "git_store") or _matches(imported, "bridge_worker"):
                violations.append(_violation(root, path, node, "provider-boundary", f"provider cannot own canonical publication: {imported!r}"))

    if module in _RECOVERY_MODULES:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or _resolve_import(module, node) != "vnext_runtime.models":
                continue
            if any(alias.name == "ExecutorProvider" for alias in node.names):
                violations.append(_violation(root, path, node, "recovery-no-executor", "Recovery code cannot depend on ExecutorProvider"))

    call_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    attr_names = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    if module == "vnext_runtime.coordinator":
        for name in sorted(call_names | attr_names):
            if name.casefold() in {"codex", "codex_args", "codex_argv", "marker", "publish_cas", "send_mail"}:
                violations.append(_violation(root, path, tree, "coordinator-authority", f"Codex/provider or canonical authority symbol {name!r}"))
    if module in {"vnext_runtime.health_aggregator", "bridge_doctor", "bridge_dashboard"}:
        for name in sorted(call_names | attr_names):
            if name in _OBSERVABILITY_MUTATORS:
                violations.append(_violation(root, path, tree, "observability-read-only", f"mutation authority call {name!r}"))
    if module in _RECOVERY_MODULES:
        for name in sorted(call_names | attr_names):
            if name in _RECOVERY_EXECUTOR_CALLS:
                violations.append(_violation(root, path, tree, "recovery-no-executor", f"executor invocation {name!r}"))
    if module == "vnext_runtime.recovery_evidence":
        for name in sorted(call_names | attr_names):
            if name in _RECOVERY_EVIDENCE_SIDE_EFFECT_CALLS:
                violations.append(
                    _violation(
                        root,
                        path,
                        tree,
                        "recovery-classifier-purity",
                        f"pure classifier side-effect call {name!r}",
                    )
                )

    if module not in {"vnext_runtime.arbitration"}:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in _SCHEDULER_SYMBOLS:
                violations.append(_violation(root, path, node, "single-scheduler-authority", f"scheduler symbol {node.name!r} must remain in vnext_runtime.arbitration"))

    if module not in _STATE_WRITER_MODULES:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"write_text", "write_bytes"}:
                continue
            literals = [item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)]
            if any("state.json" in value.casefold() for value in literals):
                violations.append(_violation(root, path, node, "single-state-writer", "direct state.json write is outside the allowlisted protocol-aware writers"))
    return violations


def check_architecture(root: Path) -> tuple[ArchitectureViolation, ...]:
    """Check the repository's source architecture without importing it."""
    root = Path(root).resolve()
    source_root = root / "worker"
    violations: list[ArchitectureViolation] = []
    capability_names: dict[str, Path] = {}
    for path in sorted(source_root.rglob("*.py")):
        if path.name.startswith("test_") or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            violations.append(ArchitectureViolation(_path_label(root, path), 1, "parse", str(exc)))
            continue
        module = _module_name(source_root, path)
        violations.extend(_check_module_rules(root, path, module, tree))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "CapabilityKey":
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                name = node.args[0].value
                previous = capability_names.get(name)
                if previous is not None:
                    violations.append(_violation(root, path, node, "unique-capability-keys", f"{name!r} is also defined in {_path_label(root, previous)}"))
                else:
                    capability_names[name] = path
    return tuple(sorted(violations, key=lambda item: (item.path, item.line, item.rule, item.detail)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check AI-Agent-Bridge vNext architecture boundaries")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    violations = check_architecture(args.root)
    if violations:
        for item in violations:
            print(item.format())
        return 1
    print("architecture gate: PASS (explicit AST dependency and authority rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ArchitectureViolation", "check_architecture", "main"]
