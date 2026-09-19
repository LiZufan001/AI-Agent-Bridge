"""Phase-8.6 architecture fixtures for the pure recovery classifier."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from architecture_gate import check_architecture


class Phase86ClassifierArchitectureTests(unittest.TestCase):
    def fixture(self, source: str) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory(prefix="bridge-phase86-architecture-")
        root = Path(temporary.name)
        path = root / "worker" / "vnext_runtime" / "recovery_evidence.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return root, temporary

    def test_classifier_cannot_import_git_or_recovery_io_services(self) -> None:
        for imported in (
            "git_store",
            "pending_report",
            "recovery_journal",
            "vnext_runtime.services.recovery",
            "subprocess",
            "socket",
            "urllib.request",
        ):
            with self.subTest(imported=imported):
                root, temporary = self.fixture(f"import {imported}\n")
                try:
                    violations = check_architecture(root)
                    self.assertTrue(
                        any(
                            item.rule == "recovery-classifier-purity"
                            for item in violations
                        ),
                        violations,
                    )
                finally:
                    temporary.cleanup()

    def test_classifier_cannot_perform_io_or_canonical_publication(self) -> None:
        fixtures = (
            "def bad(path) -> None:\n    path.read_text()\n",
            "def bad(path) -> None:\n    path.write_text('x')\n",
            "def bad(store) -> None:\n    store.publish_cas()\n",
            "def bad() -> None:\n    open('x')\n",
        )
        for source in fixtures:
            with self.subTest(source=source):
                root, temporary = self.fixture(source)
                try:
                    violations = check_architecture(root)
                    self.assertTrue(
                        any(
                            item.rule == "recovery-classifier-purity"
                            for item in violations
                        ),
                        violations,
                    )
                finally:
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
