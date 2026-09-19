"""Small dependency-free static type gate for the accepted vNext surfaces.

The repository intentionally does not require a third-party checker.  This
gate checks syntax, public callable annotations, and rejects ``Any`` in the
small immutable core contracts where it would erase a boundary.  Dynamic
canonical evidence remains explicitly bounded to the legacy/projection
modules that already require it.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TypeViolation:
    path: str
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


_CORE_MODULES = {
    "models.py", "capabilities.py", "effects.py", "run_scope.py", "coordinator.py",
    "recovery_evidence.py",
}


def _has_annotation(argument: ast.arg) -> bool:
    return argument.annotation is not None


def _annotation_contains_any(node: ast.AST | None) -> bool:
    return any(isinstance(item, ast.Name) and item.id == "Any" for item in ast.walk(node)) if node is not None else False


def check_type_contracts(root: Path, *, paths: tuple[Path, ...] | None = None) -> tuple[TypeViolation, ...]:
    """Return deterministic violations for the bounded vNext type scope."""
    root = Path(root).resolve()
    runtime = root / "worker" / "vnext_runtime"
    selected = paths or tuple(sorted(runtime.rglob("*.py")))
    violations: list[TypeViolation] = []
    for path in selected:
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            violations.append(TypeViolation(path.as_posix(), 1, "parse", str(exc)))
            continue
        core = path.name in _CORE_MODULES
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg is not None:
                arguments.append(node.args.vararg)
            if node.args.kwarg is not None:
                arguments.append(node.args.kwarg)
            for argument in arguments:
                if argument.arg in {"self", "cls"}:
                    continue
                if not _has_annotation(argument):
                    violations.append(TypeViolation(path.as_posix(), argument.lineno, "public-callable-annotations", f"parameter {argument.arg!r} has no annotation"))
            if node.returns is None:
                violations.append(TypeViolation(path.as_posix(), node.lineno, "public-callable-annotations", f"{node.name} has no return annotation"))
            if core:
                for annotation in [*(argument.annotation for argument in arguments), node.returns]:
                    if _annotation_contains_any(annotation):
                        violations.append(TypeViolation(path.as_posix(), getattr(annotation, "lineno", node.lineno), "core-contract-no-any", f"{node.name} uses Any in a core contract annotation"))
        if core:
            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign) and _annotation_contains_any(node.annotation):
                    violations.append(TypeViolation(path.as_posix(), node.lineno, "core-contract-no-any", "core contract field uses Any"))
    return tuple(sorted(violations, key=lambda item: (item.path, item.line, item.rule, item.detail)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check bounded vNext type contracts")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    violations = check_type_contracts(args.root)
    if violations:
        for item in violations:
            print(item.format())
        return 1
    print("type gate: PASS (dependency-free AST contract scope; Python 3.13)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TypeViolation", "check_type_contracts", "main"]
