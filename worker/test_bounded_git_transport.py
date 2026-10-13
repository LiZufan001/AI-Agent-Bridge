"""Real owned process timeout and cooperating lock release; no network calls."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import git_store
import operator_maintenance as owned


class BoundedGitTransportTests(unittest.TestCase):
    def test_timeout_releases_gate_and_does_not_kill_unrelated_process(self):
        with tempfile.TemporaryDirectory(prefix='bounded-git-fixture-') as tmp:
            root = Path(tmp).resolve()
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            actual_runner = owned.run_maintenance_command
            observations = []
            def delayed_transport(argv, **kwargs):
                result = actual_runner([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
                observations.append(result)
                return result
            try:
                with patch.dict(os.environ, {'BRIDGE_GIT_NETWORK_TIMEOUT_SECONDS': '2'}), \
                     patch.object(owned, 'run_maintenance_command', side_effect=delayed_transport):
                    with self.assertRaises(git_store.GitTransportError):
                        with git_store.git_mutation_gate(root):
                            git_store.git(root, 'fetch', 'origin')
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]['result'], 'TIMEOUT')
                self.assertEqual(observations[0]['operator_owned_active_children'], 0)
                self.assertIsNone(unrelated.poll())
                acquired = threading.Event()
                def acquire_after_failure():
                    with git_store.git_mutation_gate(root):
                        acquired.set()
                waiter = threading.Thread(target=acquire_after_failure, daemon=True)
                waiter.start(); waiter.join(5)
                self.assertTrue(acquired.is_set())
                self.assertFalse(waiter.is_alive())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_actual_local_git_remote_uses_the_same_bounded_transport(self):
        with tempfile.TemporaryDirectory(prefix='bounded-git-remote-') as tmp:
            root = Path(tmp).resolve()
            remote = root / 'remote.git'
            subprocess.run(['git', 'init', '--bare', str(remote)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            result = git_store.git(root, 'ls-remote', str(remote))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
