import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from vnext_runtime.arbitration import (
    GitTargetIdentity,
    HostResourceArbiter,
    canonicalize_path,
    path_identity,
    paths_conflict,
)
import bridge_worker
import git_store


class Phase55ArbitrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-phase55-")
        self.root = Path(self.temp.name)
        self.a = self.root / "a"
        self.b = self.root / "b"
        self.child = self.a / "child"
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def arbiter(self, maximum=2):
        return HostResourceArbiter(self.root, max_parallel_runs=maximum)

    def test_same_project_duplicate_admission_rejected(self):
        arbiter = self.arbiter()
        first = arbiter.reserve("p", workdir=self.a, run_id="run-a")
        second = arbiter.reserve("p", workdir=self.b, run_id="run-b")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(arbiter.last_reason, "project_already_reserved")

    def test_two_different_non_overlapping_projects_can_execute_concurrently(self):
        arbiter = self.arbiter()
        barrier = threading.Barrier(2)

        def admit(project, path):
            reservation = arbiter.reserve(project, workdir=path, run_id=f"run-{project}")
            barrier.wait(timeout=2)
            self.assertIsNotNone(reservation)
            return reservation

        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.map(admit, ("a", "b"), (self.a, self.b))
        self.assertEqual(len(arbiter.active_reservations()), 2)
        one.close()
        two.close()

    def test_equal_paths_conflict(self):
        self.assertTrue(paths_conflict(self.a, self.a))

    def test_parent_child_paths_conflict(self):
        self.child.mkdir()
        self.assertTrue(paths_conflict(self.a, self.child))

    def test_explicit_external_exclusive_path_conflicts(self):
        arbiter = self.arbiter()
        first = arbiter.reserve("a", workdir=self.a, exclusive_paths=[self.a, self.b])
        second = arbiter.reserve("b", workdir=self.root / "other", exclusive_paths=[self.root / "other", self.b])
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        first.close()

    def test_same_remote_and_branch_conflict_by_default(self):
        arbiter = self.arbiter()
        target = GitTargetIdentity("https://example.test/org/repo", "feature/x")
        one = arbiter.reserve("a", workdir=self.a, git_target=target)
        two = arbiter.reserve("b", workdir=self.b, git_target=target)
        self.assertIsNotNone(one)
        self.assertIsNone(two)
        one.close()

    def test_different_branch_can_be_admitted_when_paths_are_safe(self):
        arbiter = self.arbiter()
        one = arbiter.reserve("a", workdir=self.a, git_target=GitTargetIdentity("https://example.test/org/repo", "feature/a"))
        two = arbiter.reserve("b", workdir=self.b, git_target=GitTargetIdentity("https://example.test/org/repo", "feature/b"))
        self.assertIsNotNone(one)
        self.assertIsNotNone(two)
        one.close()
        two.close()

    def test_windows_case_normalization_and_symlink_are_conservative(self):
        self.assertEqual(path_identity(r"X:\synthetic\c\Work\Repo", windows=True), path_identity(r"X:/synthetic/c/work/repo", windows=True))
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.a, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        self.assertTrue(paths_conflict(self.a, alias))
        self.assertEqual(canonicalize_path(alias), canonicalize_path(self.a))

    def test_simultaneous_check_and_reserve_is_atomic(self):
        arbiter = self.arbiter(maximum=1)
        barrier = threading.Barrier(2)

        def attempt(project):
            barrier.wait(timeout=2)
            return arbiter.reserve(project, workdir=self.root / project, run_id=f"run-{project}")

        (self.root / "one").mkdir()
        (self.root / "two").mkdir()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("one", "two")))
        self.assertEqual(sum(result is not None for result in results), 1)
        for result in results:
            if result:
                result.close()

    def test_lost_canonical_claim_release_has_no_reservation(self):
        arbiter = self.arbiter()
        reservation = arbiter.reserve("p", workdir=self.a, run_id="run-p")
        self.assertIsNotNone(reservation)
        self.assertTrue(reservation.close())
        self.assertEqual(arbiter.active_reservations(), ())

    def test_process_lost_protocol_cas_releases_reservation(self):
        project = self.root / "projects" / "p"
        (project / "commands").mkdir(parents=True)
        (project / "reports").mkdir()
        (project / "MISSION.md").write_text("mission", encoding="utf-8")
        state = {
            "protocol_version": 2,
            "status": "COMMAND_READY",
            "generation": 7,
            "latest_command": 4,
            "latest_report": 3,
            "active_run": None,
        }
        (project / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (project / "commands" / "command-004.md").write_text(
            '<!-- bridge-command: {"command_id":4,"source":"scheduled_chatgpt",'
            '"based_on_report":3,"expected_generation":7,"kind":"EXECUTE"} -->\n',
            encoding="utf-8",
        )
        config = {
            "codex_command": "python",
            "codex_execution_mode": "full_access",
            "codex_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
            "network_guard": {"enabled": False},
        }
        with patch.object(bridge_worker, "network_guard_check", return_value=bridge_worker.NetworkGuardResult(True, "disabled", None)), patch.object(
            bridge_worker, "publish_cas", side_effect=bridge_worker.CASConflict("lost")
        ), patch.object(bridge_worker, "codex_run"):
            with self.assertRaises(bridge_worker.CASConflict):
                bridge_worker.process_project(
                    self.root,
                    "p",
                    {"workdir": str(self.a)},
                    config,
                )
        self.assertEqual(HostResourceArbiter(self.root).active_reservations(), ())

    def test_run_a_disposal_cannot_release_run_b(self):
        arbiter = self.arbiter(maximum=2)
        one = arbiter.reserve("a", workdir=self.a, run_id="run-a")
        two = arbiter.reserve("b", workdir=self.b, run_id="run-b")
        self.assertIsNotNone(one)
        self.assertIsNotNone(two)
        forged = type(two)("forged", "b", "other-admission", "run-b", two.worker_host, two.worker_pid, two.paths, two.git_target, two.acquired_at, arbiter)
        self.assertFalse(forged.close())
        self.assertEqual(len(arbiter.active_reservations()), 2)
        one.close()
        two.close()

    def test_slot_exhaustion_leaves_later_admission_unclaimed(self):
        arbiter = self.arbiter(maximum=1)
        first = arbiter.reserve("a", workdir=self.a)
        second = arbiter.check_and_reserve("b", workdir=self.b)
        self.assertIsNotNone(first)
        self.assertFalse(second.admitted)
        self.assertEqual(second.reason, "slot_exhausted")
        first.close()

    def test_stale_reservation_requires_safe_evidence(self):
        arbiter = self.arbiter()
        arbiter.registry_path.parent.mkdir(parents=True, exist_ok=True)
        arbiter.registry_path.write_text(json.dumps([{
            "reservation_id": "old",
            "project_id": "old",
            "admission_id": "old",
            "run_id": "old",
            "worker_host": "foreign-host",
            "worker_pid": 999999,
            "paths": [str(self.a)],
            "acquired_at": "old",
        }]), encoding="utf-8")
        self.assertIsNone(arbiter.reserve("new", workdir=self.a))

    def test_git_store_mutations_serialize_without_serializing_agent_execution(self):
        order = []
        entered = threading.Event()
        release = threading.Event()

        def first():
            with git_store.git_mutation_gate(self.root):
                order.append("first-enter")
                entered.set()
                release.wait(timeout=2)
                order.append("first-exit")

        def second():
            entered.wait(timeout=2)
            order.append("agent-parallel")
            with git_store.git_mutation_gate(self.root):
                order.append("second-enter")

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first)
            two = pool.submit(second)
            entered.wait(timeout=2)
            time.sleep(0.02)
            release.set()
            one.result(timeout=2)
            two.result(timeout=2)
        self.assertLess(order.index("agent-parallel"), order.index("second-enter"))
        self.assertLess(order.index("first-exit"), order.index("second-enter"))

    def test_drain_waits_for_active_runs(self):
        started = threading.Event()
        finish = threading.Event()

        def fake_process(*_args, **_kwargs):
            started.set()
            finish.wait(timeout=2)
            return True

        with patch.object(bridge_worker, "process_project", fake_process):
            coordinator = bridge_worker.WorkerCoordinator(self.root, {"runtime": {"max_parallel_runs": 2}})
            coordinator.submit("p", {"workdir": str(self.a)}, {})
            self.assertTrue(started.wait(timeout=2))
            result = []
            waiter = threading.Thread(target=lambda: result.append(coordinator.begin_draining()))
            waiter.start()
            time.sleep(0.02)
            self.assertEqual(coordinator.lifecycle, coordinator.DRAINING)
            self.assertTrue(waiter.is_alive())
            finish.set()
            waiter.join(timeout=2)
            self.assertEqual(result, [True])
            coordinator.close()




# Existing arbitration cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
