"""Explicit AUTO fixture for pre-control unit tests, never production defaults."""
import unittest
from unittest.mock import patch
from worker_execution_control import ExecutionControlDecision


def install_auto_fixture() -> None:
    replacement = patch("worker_execution_control.read_execution_control", return_value=ExecutionControlDecision("auto", "owner_resume", "owner", "2026-01-01T00:00:00Z", True, "fixture"))
    replacement.start()
    unittest.addModuleCleanup(replacement.stop)
