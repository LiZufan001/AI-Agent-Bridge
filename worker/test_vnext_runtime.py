import unittest
from pathlib import Path

from vnext_runtime.capabilities import (
    CapabilityKey,
    CapabilityRegistry,
    DuplicateCapabilityError,
    MissingCapabilityError,
)
from vnext_runtime.effects import (
    EffectDisposalError,
    EffectOwnershipError,
    EffectScope,
)
from vnext_runtime.models import (
    ExecutionDiagnostics,
    ExecutionOutcome,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    RunIdentity,
)
from vnext_runtime.run_scope import RunScope, RunScopeError, RunScopeState


class _Disposable:
    def __init__(self, name: str, events: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail = fail

    def dispose(self) -> None:
        self.events.append(self.name)
        if self.fail:
            raise RuntimeError(self.name)


class CapabilityTests(unittest.TestCase):
    def test_typed_registration_and_lookup(self) -> None:
        key = CapabilityKey[list[str]]("test.provider")
        registry = CapabilityRegistry()
        provider = ["synthetic"]

        registry.register(key, provider)

        self.assertIs(registry.require(key), provider)
        self.assertIs(registry.get(key), provider)
        self.assertTrue(registry.contains(key))

    def test_missing_capability_fails_closed(self) -> None:
        key = CapabilityKey[str]("missing")

        with self.assertRaises(MissingCapabilityError):
            CapabilityRegistry().require(key)

    def test_duplicate_capability_name_is_rejected(self) -> None:
        registry = CapabilityRegistry()
        registry.register(CapabilityKey[str]("same"), "first")

        with self.assertRaises(DuplicateCapabilityError):
            registry.register(CapabilityKey[int]("same"), 2)
        self.assertEqual(registry.require(CapabilityKey[str]("same")), "first")


class EffectTests(unittest.TestCase):
    def test_reverse_cleanup_and_idempotent_close(self) -> None:
        events: list[str] = []
        scope = EffectScope("scope-a")
        scope.own(_Disposable("first", events))
        scope.own(_Disposable("second", events))

        scope.close()
        scope.close()

        self.assertEqual(events, ["second", "first"])
        self.assertTrue(scope.closed)

    def test_cleanup_continues_after_one_disposer_fails(self) -> None:
        events: list[str] = []
        scope = EffectScope("scope-a")
        scope.own(_Disposable("first", events))
        scope.own(_Disposable("bad", events, fail=True))
        scope.own(_Disposable("last", events))

        errors = scope.close()

        self.assertEqual(events, ["last", "bad", "first"])
        self.assertEqual(len(errors), 1)
        with self.assertRaises(EffectDisposalError):
            scope.raise_if_disposal_failed()

    def test_effect_handle_cannot_cross_scopes(self) -> None:
        scope_a = EffectScope("scope-a")
        scope_b = EffectScope("scope-b")
        handle = scope_a.own(_Disposable("a", []))

        with self.assertRaises(EffectOwnershipError):
            scope_b.release(handle)


class ModelTests(unittest.TestCase):
    def test_models_are_immutable_and_have_value_identity(self) -> None:
        identity = RunIdentity("project", 3, "run-003", 8)
        request = ExecutionRequest(
            project_id="project",
            command_id=3,
            run_id="run-003",
            workdir=Path("X:/synthetic/c/work"),
            mission="mission",
            command_text="command",
            kind="EXECUTE",
            profile=ExecutionProfile(model="synthetic"),
        )
        result = ExecutionResult(
            provider_id="synthetic",
            outcome="SUCCESS",
            exit_code=0,
            final_message="done",
            launched_at=None,
            completed_at=None,
            diagnostics=ExecutionDiagnostics(),
        )

        self.assertEqual(identity, RunIdentity("project", 3, "run-003", 8))
        self.assertEqual(request, request)
        self.assertEqual(result.outcome, ExecutionOutcome.SUCCESS)
        with self.assertRaises((AttributeError, TypeError)):
            request.project_id = "other"  # type: ignore[misc]


class RunScopeTests(unittest.TestCase):
    def test_scope_derives_exact_identity_from_claimed_state(self) -> None:
        state = {
            "status": "CODEX_RUNNING",
            "project_id": "project",
            "latest_command": 3,
            "generation": 8,
            "active_run": {
                "project_id": "project",
                "command_id": 3,
                "run_id": "run-003",
                "claimed_generation": 8,
            },
        }

        scope = RunScope.from_claimed_state(
            state,
            run_dir=Path("runtime/runs/run-003"),
        )

        self.assertEqual(scope.identity, RunIdentity("project", 3, "run-003", 8))
        self.assertEqual(scope.artifacts.final_message, Path("runtime/runs/run-003/final-message.txt"))
        with self.assertRaises(RunScopeError):
            RunScope.from_claimed_state(
                state,
                run_id="run-other",
                run_dir=Path("runtime/runs/run-other"),
            )

    def test_lifecycle_and_owned_effects(self) -> None:
        events: list[str] = []
        scope = RunScope(RunIdentity("project", 3, "run-003", 8))
        self.assertEqual(scope.state, RunScopeState.CREATED)
        scope.activate()
        self.assertEqual(scope.state, RunScopeState.ACTIVE)
        scope.own(_Disposable("effect", events))
        scope.begin_completion()
        scope.close()

        self.assertEqual(events, ["effect"])
        self.assertEqual(scope.state, RunScopeState.CLOSED)
        with self.assertRaises(AttributeError):
            scope.identity = RunIdentity("other", 4, "run-004", 9)  # type: ignore[misc]

    def test_scope_requires_activation_before_ownership(self) -> None:
        scope = RunScope(RunIdentity("project", 3, "run-003", 8))

        with self.assertRaises(RunScopeError):
            scope.own(_Disposable("effect", []))

    def test_provider_pin_and_single_invocation_are_scope_owned(self) -> None:
        scope = RunScope(RunIdentity("project", 3, "run-003", 8))
        scope.activate()

        scope.begin_provider_execution("codex")
        self.assertEqual(scope.provider_id, "codex")
        self.assertTrue(scope.provider_invoked)
        with self.assertRaises(RunScopeError):
            scope.begin_provider_execution("codex")
        with self.assertRaises(RunScopeError):
            scope.pin_provider("other")


if __name__ == "__main__":
    unittest.main()
