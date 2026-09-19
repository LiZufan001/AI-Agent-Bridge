"""Safe, isolated Phase-5.5 scheduler-boundary smoke tests.

This module deliberately uses only temporary roots, a deterministic fake
executor, and local lock files.  It does not create Bridge commands/reports or
invoke a real Codex process.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import bridge_worker
import git_store
from vnext_runtime.arbitration import HostResourceArbiter


class Phase55IsolatedIntegrationSmokeTests(unittest.TestCase):
    def test_scheduler_overlap_wait_release_and_drain_boundary(self):
        with tempfile.TemporaryDirectory(prefix="bridge-phase55-isolated-smoke-") as temp:
            root = Path(temp)
            runtime = root / "isolated-runtime"
            alpha = root / "synthetic-alpha"
            beta = root / "synthetic-beta"
            alpha_child = alpha / "child"
            duplicate_path = root / "synthetic-duplicate"
            drain_path = root / "synthetic-drain"
            alpha_child.mkdir(parents=True)
            beta.mkdir()
            duplicate_path.mkdir()
            drain_path.mkdir()

            arbiter = HostResourceArbiter(
                root,
                max_parallel_runs=3,
                registry_path=runtime / "resource-registry.json",
                lock_path=runtime / "resource-registry.lock",
                worker_host="isolated-smoke-host",
                worker_pid=os.getpid(),
            )
            state_lock = threading.Lock()
            active_executor_count = 0
            active_executor_peak = 0
            started_projects: list[str] = []
            gate_attempts: list[str] = []
            gate_entries: list[str] = []
            report_events: list[str] = []
            both_executors_started = threading.Event()
            second_gate_attempted = threading.Event()
            first_gate_entered = threading.Event()
            second_gate_entered = threading.Event()
            first_gate_release = threading.Event()

            def fake_executor(
                project_id: str,
                workdir: Path,
            ) -> bool:
                nonlocal active_executor_count, active_executor_peak
                decision = arbiter.check_and_reserve(
                    project_id,
                    workdir=workdir,
                    run_id=f"run-{project_id}",
                    admission_id=f"admission-{project_id}",
                )
                reservation = decision.reservation
                if reservation is None:
                    return False
                try:
                    with state_lock:
                        active_executor_count += 1
                        active_executor_peak = max(
                            active_executor_peak,
                            active_executor_count,
                        )
                        started_projects.append(project_id)
                        if active_executor_count == 2:
                            both_executors_started.set()
                    self.assertTrue(both_executors_started.wait(timeout=2))

                    with state_lock:
                        gate_attempts.append(project_id)
                        if len(gate_attempts) == 2:
                            second_gate_attempted.set()
                    with git_store.git_mutation_gate(root):
                        with state_lock:
                            gate_entries.append(project_id)
                            is_first_gate_entry = len(gate_entries) == 1
                            if is_first_gate_entry:
                                first_gate_entered.set()
                            else:
                                second_gate_entered.set()
                        if is_first_gate_entry:
                            self.assertTrue(first_gate_release.wait(timeout=2))
                    return True
                finally:
                    reservation.close()
                    with state_lock:
                        active_executor_count -= 1
                        report_events.append(project_id)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(fake_executor, "alpha", alpha)
                second = pool.submit(fake_executor, "beta", beta)

                self.assertTrue(both_executors_started.wait(timeout=2))
                with state_lock:
                    self.assertEqual(active_executor_count, 2)
                    self.assertCountEqual(started_projects, ["alpha", "beta"])
                self.assertEqual(len(arbiter.active_reservations()), 2)

                duplicate = arbiter.check_and_reserve(
                    "alpha",
                    workdir=duplicate_path,
                    run_id="run-alpha-duplicate",
                )
                self.assertIsNone(duplicate.reservation)
                self.assertEqual(duplicate.reason, "project_already_reserved")

                waiting = arbiter.check_and_reserve(
                    "conflicting",
                    workdir=alpha_child,
                    run_id="run-conflicting",
                )
                self.assertIsNone(waiting.reservation)
                self.assertEqual(waiting.reason, "exclusive_path_conflict")

                self.assertTrue(first_gate_entered.wait(timeout=2))
                self.assertTrue(second_gate_attempted.wait(timeout=2))
                self.assertFalse(second_gate_entered.is_set())
                first_gate_release.set()

                self.assertTrue(first.result(timeout=2))
                self.assertTrue(second.result(timeout=2))

            with state_lock:
                self.assertEqual(active_executor_count, 0)
                self.assertEqual(active_executor_peak, 2)
                self.assertCountEqual(gate_entries, ["alpha", "beta"])
                self.assertEqual(len(report_events), 2)
            self.assertEqual(arbiter.active_reservations(), ())

            released = arbiter.check_and_reserve(
                "conflicting",
                workdir=alpha_child,
                run_id="run-conflicting-after-release",
            )
            self.assertIsNotNone(released.reservation)
            self.assertTrue(released.reservation.close())
            with state_lock:
                report_events.append("conflicting")

            drain_started = threading.Event()
            drain_finish = threading.Event()
            drain_returned = threading.Event()
            drain_results: list[bool] = []

            def fake_process(
                _bridge_root: Path,
                project_id: str,
                _project_cfg: dict,
                _config: dict,
                *,
                arbiter: HostResourceArbiter | None = None,
            ) -> bool:
                self.assertIsNotNone(arbiter)
                reservation = arbiter.reserve(
                    project_id,
                    workdir=drain_path,
                    run_id="run-drain",
                )
                self.assertIsNotNone(reservation)
                try:
                    drain_started.set()
                    self.assertTrue(drain_finish.wait(timeout=2))
                    return True
                finally:
                    reservation.close()
                    with state_lock:
                        report_events.append(project_id)

            coordinator = None
            with patch.object(bridge_worker, "process_project", fake_process):
                coordinator = bridge_worker.WorkerCoordinator(
                    root,
                    {"runtime": {"max_parallel_runs": 1}},
                    runtime_root=runtime / "coordinator",
                )
                self.assertTrue(
                    coordinator.submit(
                        "drain",
                        {"workdir": str(drain_path)},
                        {},
                    )
                )
                self.assertFalse(
                    coordinator.submit(
                        "drain",
                        {"workdir": str(drain_path)},
                        {},
                    )
                )
                self.assertTrue(drain_started.wait(timeout=2))

                def drain() -> None:
                    drain_results.append(coordinator.begin_draining())
                    drain_returned.set()

                drain_thread = threading.Thread(target=drain)
                drain_thread.start()
                self.assertEqual(coordinator.lifecycle, coordinator.DRAINING)
                self.assertFalse(drain_returned.wait(timeout=0.05))
                self.assertTrue(drain_thread.is_alive())
                drain_finish.set()
                self.assertTrue(drain_returned.wait(timeout=2))
                drain_thread.join(timeout=2)
                self.assertEqual(drain_results, [True])
                coordinator.close()

            with state_lock:
                self.assertCountEqual(
                    report_events,
                    ["alpha", "beta", "conflicting", "drain"],
                )
                self.assertEqual(len(report_events), 4)
            self.assertEqual(arbiter.active_reservations(), ())


if __name__ == "__main__":
    unittest.main()
