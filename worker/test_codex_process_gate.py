import subprocess
import unittest
from unittest.mock import patch

import codex_process_gate as gate


class CodexProcessGateTests(unittest.TestCase):
    def test_windows_child_uses_create_no_window(self):
        with patch.object(gate.os, "name", "nt"):
            options = gate._popen_options()
        self.assertEqual(
            options["creationflags"],
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )

    def test_non_windows_child_has_no_windows_creation_flags(self):
        with patch.object(gate.os, "name", "posix"):
            self.assertEqual(gate._popen_options(), {})

    def test_real_child_launch_preserves_stdio_and_passes_flags(self):
        sentinel = object()
        with (
            patch.object(gate, "_popen_options", return_value={"creationflags": 123}),
            patch.object(gate.subprocess, "Popen", return_value=sentinel) as popen,
        ):
            result = gate._launch_child(["codex", "exec"])
        self.assertIs(result, sentinel)
        popen.assert_called_once_with(
            ["codex", "exec"],
            stdin=gate.sys.stdin,
            stdout=gate.sys.stdout,
            stderr=gate.sys.stderr,
            shell=False,
            creationflags=123,
        )


if __name__ == "__main__":
    unittest.main()
