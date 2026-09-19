import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker"
LEGACY_OWNER = WORKER / "vnext_runtime" / "adoption.py"
LEGACY_LAUNCHER_OWNER = WORKER / "vnext_runtime" / "launcher_actions.py"
PACKAGE_INIT = WORKER / "vnext_runtime" / "__init__.py"

RETIRED_NAMES = {
    "ReconciliationConflict",
    "ReconciliationConflictError",
    "ReconciliationDecision",
    "ReconciliationPlan",
    "ReconciliationPolicy",
    "ReconciliationResolution",
    "plan_reconciliation",
    "Phase85FaultInjection",
}


class RetiredAdoptionSurfaceTests(unittest.TestCase):
    def test_retired_names_are_not_package_level_runtime_api(self) -> None:
        tree = ast.parse(PACKAGE_INIT.read_text(encoding="utf-8"))
        exported: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
                continue
            if isinstance(node.value, (ast.List, ast.Tuple)):
                for item in node.value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        exported.add(item.value)
        self.assertTrue(RETIRED_NAMES.isdisjoint(exported))

    def test_production_modules_do_not_depend_on_retired_reconciliation(self) -> None:
        allowed_owners = {LEGACY_OWNER.resolve(), LEGACY_LAUNCHER_OWNER.resolve()}
        offenders: list[str] = []
        for path in WORKER.rglob("*.py"):
            resolved = path.resolve()
            if path.name.startswith("test_") or resolved in allowed_owners:
                continue
            text = path.read_text(encoding="utf-8")
            for name in RETIRED_NAMES:
                if name in text:
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{name}")
        self.assertEqual(offenders, [])

    def test_legacy_reconciliation_has_no_runtime_entrypoint(self) -> None:
        # The historical helper may remain readable for old test/evidence
        # compatibility, but current execution code must not turn it into an
        # executable service/CLI surface.
        text = LEGACY_OWNER.read_text(encoding="utf-8")
        self.assertIn("def plan_reconciliation", text)
        self.assertNotIn('if __name__ == "__main__"', text)
        self.assertNotIn("argparse.ArgumentParser", text)


if __name__ == "__main__":
    unittest.main()
