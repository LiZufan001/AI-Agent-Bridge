"""Focused deterministic coverage for the generic Coordinator boundary."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from vnext_runtime.coordinator import Coordinator, CoordinatorConfig


class CoordinatorBoundaryTests(unittest.TestCase):
    def test_config_keeps_compatibility_default(self):
        self.assertEqual(CoordinatorConfig.from_worker_config({}).max_parallel_runs, 1)

    def test_independent_runs_use_one_typed_runner_and_can_overlap(self):
        with tempfile.TemporaryDirectory(prefix="bridge-coordinator-") as temp:
            root = Path(temp)
            paths = {name: root / name for name in ("a", "b")}
            for path in paths.values():
                path.mkdir()
            started = threading.Barrier(2)
            active = 0
            peak = 0
            lock = threading.Lock()

            def runner(_root, project_id, project_config, _config, *, arbiter):
                nonlocal active, peak
                reservation = arbiter.reserve(
                    project_id,
                    workdir=project_config["workdir"],
                    run_id=f"run-{project_id}",
                )
                self.assertIsNotNone(reservation)
                with reservation:
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    started.wait(timeout=2)
                    time.sleep(0.02)
                    with lock:
                        active -= 1
                return True

            coordinator = Coordinator(
                root,
                {"runtime": {"max_parallel_runs": 2}},
                runner=runner,
                runtime_root=root / "runtime",
            )
            try:
                self.assertTrue(coordinator.submit("a", {"workdir": paths["a"]}, {}))
                self.assertTrue(coordinator.submit("b", {"workdir": paths["b"]}, {}))
                self.assertTrue(coordinator.reap(wait=True))
                self.assertEqual(peak, 2)
                self.assertEqual(coordinator.active_run_count, 0)
            finally:
                coordinator.close()

    def test_draining_rejects_new_admission_and_waits(self):
        with tempfile.TemporaryDirectory(prefix="bridge-coordinator-drain-") as temp:
            root = Path(temp)
            path = root / "project"
            path.mkdir()
            started = threading.Event()
            release = threading.Event()

            def runner(_root, _project_id, project_config, _config, *, arbiter):
                reservation = arbiter.reserve("p", workdir=project_config["workdir"])
                self.assertIsNotNone(reservation)
                with reservation:
                    started.set()
                    release.wait(timeout=2)
                return True

            coordinator = Coordinator(
                root,
                {},
                runner=runner,
                runtime_root=root / "runtime",
            )
            try:
                self.assertTrue(coordinator.submit("p", {"workdir": path}, {}))
                self.assertTrue(started.wait(timeout=2))
                waiter = threading.Thread(target=coordinator.begin_draining)
                waiter.start()
                time.sleep(0.02)
                self.assertEqual(coordinator.lifecycle, coordinator.DRAINING)
                self.assertFalse(
                    coordinator.submit("later", {"workdir": root / "later"}, {})
                )
                self.assertTrue(waiter.is_alive())
                release.set()
                waiter.join(timeout=2)
                self.assertFalse(waiter.is_alive())
            finally:
                release.set()
                coordinator.close()


if __name__ == "__main__":
    unittest.main()
