#!/usr/bin/env python3
"""Static closure gate for designated Bridge operator/maintenance sources."""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path


DESIGNATED_PYTHON = (
    "worker/self_maintenance.py",
)
DESIGNATED_POWERSHELL_GLOBS = (
    "worker/*maintenance*.ps1",
    "worker/*rollout*.ps1",
    "worker/*inspection*.ps1",
)
INVENTORY = "docs/OPERATOR_SAFETY_SCOPE.json"


@dataclass(frozen=True, slots=True)
class SafetyViolation:
    path: str
    line: int
    rule: str


def _python_violations(path: Path, relative: str) -> list[SafetyViolation]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=relative)
    violations: list[SafetyViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Name)
            and owner.id == "subprocess"
            and node.func.attr in {"run", "Popen", "check_call", "check_output"}
        ):
            lines = text.splitlines()
            context = "\n".join(lines[max(0, node.lineno - 4) : node.lineno])
            if (
                node.func.attr == "Popen"
                and "operator-safety: owned-long-lived-controller" in context
            ):
                continue
            violations.append(
                SafetyViolation(relative, node.lineno, "direct-subprocess-outside-boundary")
            )
    return violations + _powershell_text_violations(text, relative)


def _powershell_text_violations(text: str, relative: str) -> list[SafetyViolation]:
    rules = (
        (re.compile(r"(?im)Get-ChildItem[^\r\n]*\s-Recurse\b"), "recursive-enumeration"),
        (re.compile(r"(?im)Get-Content[^\r\n]*\s-Raw\b"), "unbounded-raw-read"),
        (re.compile(r"(?im)Start-Process[^\r\n]*\s-Verb\s+RunAs\b"), "adhoc-elevation"),
        (re.compile(r"(?im)Start-Transcript\b"), "unbounded-transcript"),
        (re.compile(r"(?im)(taskkill(?:\.exe)?\s+/IM|Stop-Process\s+-Name)"), "image-name-cleanup"),
        (
            re.compile(r"(?im)Get-CimInstance\s+Win32_Process(?![^\r\n]*\s-Filter\b)"),
            "unfiltered-process-snapshot",
        ),
        (re.compile(r"(?m)^\s*\$[A-Za-z_][A-Za-z0-9_]*\s*\+="), "powershell-array-append"),
    )
    violations: list[SafetyViolation] = []
    for pattern, rule in rules:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(SafetyViolation(relative, line, rule))
    return violations


def audit(root: Path) -> tuple[SafetyViolation, ...]:
    violations: list[SafetyViolation] = []
    for relative in DESIGNATED_PYTHON:
        path = root / relative
        if not path.is_file():
            violations.append(SafetyViolation(relative, 0, "designated-source-missing"))
            continue
        violations.extend(_python_violations(path, relative))
    for pattern in DESIGNATED_POWERSHELL_GLOBS:
        for path in root.glob(pattern):
            relative = path.relative_to(root).as_posix()
            violations.extend(
                _powershell_text_violations(path.read_text(encoding="utf-8"), relative)
            )
    inventory_path = root / INVENTORY
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        counts = inventory["counts"]
        calls = inventory["operator_callsites"]
        if counts["unresolved_high_risk_operator_calls"] != 0:
            violations.append(SafetyViolation(INVENTORY, 0, "unresolved-high-risk-call"))
        if counts["operator_callsites_reviewed"] != len(calls):
            violations.append(SafetyViolation(INVENTORY, 0, "inventory-count-mismatch"))
        if any(
            item.get("classification_after")
            not in {"SAFE_BOUNDED", "SAFE_STREAMING", "RETIRED"}
            for item in calls
        ):
            violations.append(SafetyViolation(INVENTORY, 0, "inventory-classification-invalid"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        violations.append(SafetyViolation(INVENTORY, 0, "inventory-invalid"))
    return tuple(sorted(violations, key=lambda item: (item.path, item.line, item.rule)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    violations = audit(args.root.resolve())
    if violations:
        for item in violations:
            print(f"{item.path}:{item.line}: {item.rule}")
        return 1
    print("operator execution safety gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
