from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import worker_health
from vnext_runtime import arbitration


CREATE_NO_WINDOW = 0x08000000


class WindowsHiddenGitProcessTests(unittest.TestCase):
    def test_arbitration_git_identity_sets_create_no_window(self) -> None:
        responses = [
            subprocess.CompletedProcess(["git"], 0, stdout="main\n", stderr=""),
            subprocess.CompletedProcess(
                ["git"],
                0,
                stdout="https://github.com/example/repository.git\n",
                stderr="",
            ),
        ]
        with patch.object(
            arbitration.subprocess,
            "CREATE_NO_WINDOW",
            CREATE_NO_WINDOW,
            create=True,
        ), patch.object(
            arbitration.subprocess,
            "run",
            side_effect=responses,
        ) as run:
            identity = arbitration.git_target_identity(Path("."))

        self.assertIsNotNone(identity)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs.get("creationflags"), CREATE_NO_WINDOW)
            self.assertFalse(call.kwargs.get("shell"))

    def test_heartbeat_git_credential_lookup_sets_create_no_window(self) -> None:
        response = subprocess.CompletedProcess(
            ["git", "credential", "fill"],
            0,
            stdout="protocol=https\nhost=github.com\npassword=test-token\n",
            stderr="",
        )
        with patch.object(
            worker_health.subprocess,
            "CREATE_NO_WINDOW",
            CREATE_NO_WINDOW,
            create=True,
        ), patch.object(
            worker_health.subprocess,
            "run",
            return_value=response,
        ) as run:
            token = worker_health._credential_token_from_git()

        self.assertEqual(token, "test-token")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs.get("creationflags"), CREATE_NO_WINDOW)
        self.assertEqual(run.call_args.kwargs["env"]["GCM_INTERACTIVE"], "Never")
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__":
    unittest.main()
