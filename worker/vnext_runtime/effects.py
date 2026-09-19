"""Deterministic ownership and cleanup primitives for future run scopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Disposable(Protocol):
    """A resource with an idempotent, synchronous cleanup operation."""

    def dispose(self) -> None:
        """Release the resource owned by its enclosing effect scope."""


class EffectOwnershipError(RuntimeError):
    """Raised when an effect handle is used by a different scope."""


class EffectScopeClosedError(RuntimeError):
    """Raised when a new effect is registered after scope disposal begins."""


class EffectDisposalError(RuntimeError):
    """Reports cleanup failures after every owned effect was attempted."""

    def __init__(self, errors: tuple[BaseException, ...]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} effect disposer(s) failed.")


@dataclass(frozen=True, slots=True)
class EffectHandle:
    """Opaque ownership evidence returned by one ``EffectScope``."""

    scope_id: str
    registration_id: int
    _owner_token: object


@dataclass(slots=True)
class _Registration:
    handle: EffectHandle
    disposable: Disposable
    active: bool = True


class EffectScope:
    """Own effects and dispose them in reverse registration order.

    The scope marks each registration inactive before invoking its disposer.
    This makes repeated ``close`` calls safe and lets cleanup continue after a
    disposer raises.  Cleanup failures are retained as diagnostics and do not
    erase an already-valid business result; callers may explicitly promote
    them with ``raise_if_disposal_failed``.
    """

    __slots__ = (
        "_scope_id",
        "_owner_token",
        "_registrations",
        "_next_id",
        "_closed",
        "_disposal_errors",
    )

    def __init__(self, scope_id: str = "effect-scope") -> None:
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("Effect scope id must be a non-empty string.")
        self._scope_id = scope_id
        self._owner_token = object()
        self._registrations: list[_Registration] = []
        self._next_id = 1
        self._closed = False
        self._disposal_errors: tuple[BaseException, ...] = ()

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def closed(self) -> bool:
        return self._closed

    def own(self, disposable: Disposable) -> EffectHandle:
        """Register a disposable and return a handle owned by this scope."""

        if self._closed:
            raise EffectScopeClosedError(
                f"Effect scope is already closed: {self._scope_id}"
            )
        handle = EffectHandle(self._scope_id, self._next_id, self._owner_token)
        self._next_id += 1
        self._registrations.append(_Registration(handle, disposable))
        return handle

    register = own

    def release(self, handle: EffectHandle) -> None:
        """Dispose one owned effect, rejecting handles from other scopes."""

        registration = self._find(handle)
        if registration is None or not registration.active:
            return
        registration.active = False
        try:
            handle_disposable(registration.disposable)
        except BaseException as exc:  # cleanup must continue after failure
            self._disposal_errors = (*self._disposal_errors, exc)

    @property
    def disposal_errors(self) -> tuple[BaseException, ...]:
        return self._disposal_errors

    def close(self) -> tuple[BaseException, ...]:
        """Dispose all remaining effects in reverse order."""

        if self._closed:
            return self._disposal_errors
        self._closed = True
        errors: list[BaseException] = []
        for registration in reversed(self._registrations):
            if not registration.active:
                continue
            registration.active = False
            try:
                handle_disposable(registration.disposable)
            except BaseException as exc:  # cleanup must continue after failure
                errors.append(exc)
        self._disposal_errors = (*self._disposal_errors, *errors)
        return self._disposal_errors

    dispose = close

    def raise_if_disposal_failed(self) -> None:
        """Optionally promote recorded cleanup diagnostics at a boundary."""

        if self._disposal_errors:
            raise EffectDisposalError(self._disposal_errors)

    def __enter__(self) -> "EffectScope":
        if self._closed:
            raise EffectScopeClosedError(
                f"Effect scope is already closed: {self._scope_id}"
            )
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _find(self, handle: EffectHandle) -> _Registration | None:
        if handle._owner_token is not self._owner_token:
            raise EffectOwnershipError(
                f"Effect handle belongs to scope {handle.scope_id!r}, "
                f"not {self._scope_id!r}."
            )
        for registration in self._registrations:
            if registration.handle.registration_id == handle.registration_id:
                return registration
        return None


def handle_disposable(disposable: Disposable) -> None:
    """Call a disposable through the narrow protocol boundary."""

    disposable.dispose()
