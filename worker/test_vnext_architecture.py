"""Phase-8 deterministic architecture and type gate tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from architecture_gate import check_architecture
from type_gate import check_type_contracts


class Phase8ArchitectureGateTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_current_tree_passes_architecture_gate(self) -> None:
        self.assertEqual(check_architecture(self.ROOT), ())

    def _fixture(self, relative: str, source: str) -> Path:
        directory = Path(self._temp.name) / "worker"
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="bridge-phase8-gate-")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_forbidden_coordinator_codex_edge_fails_actionably(self) -> None:
        self._fixture("vnext_runtime/coordinator.py", "import executor\n")
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "coordinator-boundary" and "executor" in item.detail for item in violations))

    def test_observability_canonical_mutation_edge_fails(self) -> None:
        self._fixture("vnext_runtime/health_aggregator.py", "import git_store\n")
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "observability-read-only" for item in violations))

    def test_protocol_side_effect_edge_fails(self) -> None:
        self._fixture("protocol_core.py", "import subprocess\n")
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "protocol-kernel-purity" for item in violations))

    def test_recovery_provider_edge_fails(self) -> None:
        self._fixture("vnext_runtime/services/recovery.py", "from vnext_runtime.providers.executors import codex\n")
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "recovery-no-executor" for item in violations))

    def test_recovery_contract_edge_fails(self) -> None:
        self._fixture("vnext_runtime/services/recovery.py", "from vnext_runtime.models import ExecutorProvider\n")
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "recovery-no-executor" and "ExecutorProvider" in item.detail for item in violations))

    def test_recovery_evidence_is_covered_by_no_executor_boundary(self) -> None:
        self._fixture(
            "vnext_runtime/recovery_evidence.py",
            "from vnext_runtime.providers.executors import codex\n\ndef inspect() -> None:\n    codex.execute()\n",
        )
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "recovery-no-executor" for item in violations))

    def test_valid_provider_adapter_edge_is_allowed(self) -> None:
        self._fixture(
            "vnext_runtime/providers/executors/codex.py",
            "import executor\n",
        )
        self.assertFalse(any(item.rule == "provider-boundary" for item in check_architecture(Path(self._temp.name))))

    def test_observability_surfaces_are_read_only(self) -> None:
        self.assertFalse(any(item.rule == "observability-read-only" for item in check_architecture(self.ROOT)))

    def test_direct_state_writer_outside_authority_fails(self) -> None:
        self._fixture(
            "vnext_runtime/health_aggregator.py",
            'path.write_text("state.json")\n',
        )
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "single-state-writer" for item in violations))

    def test_duplicate_scheduler_algorithm_fails(self) -> None:
        self._fixture(
            "vnext_runtime/health_aggregator.py",
            "def paths_conflict(left, right):\n    return left == right\n",
        )
        violations = check_architecture(Path(self._temp.name))
        self.assertTrue(any(item.rule == "single-scheduler-authority" for item in violations))

    def test_type_gate_accepts_current_public_contracts(self) -> None:
        self.assertEqual(check_type_contracts(self.ROOT), ())

    def test_type_gate_catches_untyped_contract_fixture(self) -> None:
        path = self._fixture("vnext_runtime/models.py", "class BadProvider:\n    def execute(self, request):\n        return request\n")
        violations = check_type_contracts(Path(self._temp.name), paths=(path,))
        self.assertTrue(any(item.rule == "public-callable-annotations" for item in violations))

    def test_type_gate_catches_any_in_core_contract_fixture(self) -> None:
        path = self._fixture(
            "vnext_runtime/models.py",
            "from typing import Any\n\ndef public(value: Any) -> str:\n    return str(value)\n",
        )
        violations = check_type_contracts(Path(self._temp.name), paths=(path,))
        self.assertTrue(any(item.rule == "core-contract-no-any" for item in violations))

    def test_type_gate_treats_recovery_evidence_as_core_contract(self) -> None:
        path = self._fixture(
            "vnext_runtime/recovery_evidence.py",
            "from typing import Any\n\ndef classify(value: Any) -> str:\n    return str(value)\n",
        )
        violations = check_type_contracts(Path(self._temp.name), paths=(path,))
        self.assertTrue(any(item.rule == "core-contract-no-any" for item in violations))

    def test_compatibility_shell_reaches_generic_coordinator(self) -> None:
        source = (self.ROOT / "worker" / "bridge_worker.py").read_text(encoding="utf-8")
        self.assertIn("class WorkerCoordinator(Coordinator)", source)
        hardened = (self.ROOT / "worker" / "bridge_worker_hardened.py").read_text(encoding="utf-8")
        self.assertIn("bw.WorkerCoordinator(", hardened)


if __name__ == "__main__":
    unittest.main()
