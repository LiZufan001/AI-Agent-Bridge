"""Typed, built-in capability lookup for the vNext migration.

The registry is intentionally a small in-process dependency container.  It
does not discover, load, replace, or otherwise manage third-party plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar, cast


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CapabilityKey(Generic[T]):
    """A typed name used at a runtime capability boundary."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Capability key name must be a non-empty string.")


class CapabilityError(RuntimeError):
    """Base class for fail-closed capability lookup errors."""


class MissingCapabilityError(CapabilityError):
    """Raised when a required capability has not been registered."""

    def __init__(self, key: CapabilityKey[object]) -> None:
        self.key = key
        super().__init__(f"Required capability is not registered: {key.name}")


class DuplicateCapabilityError(CapabilityError):
    """Raised instead of silently replacing an already registered capability."""

    def __init__(self, key: CapabilityKey[object]) -> None:
        self.key = key
        super().__init__(f"Capability is already registered: {key.name}")


class CapabilityRegistry:
    """A typed registry for explicitly supplied built-in capabilities.

    Capability names are the runtime identity.  Consequently, registering a
    second ``CapabilityKey`` with the same name is rejected even if its static
    type parameter differs.  Values are kept as ``object`` internally and
    narrowed only through the typed key at the public lookup boundary.
    """

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: dict[str, object] = {}

    def register(self, key: CapabilityKey[T], value: T) -> None:
        """Register one capability, rejecting duplicate names."""

        if key.name in self._values:
            raise DuplicateCapabilityError(cast(CapabilityKey[object], key))
        self._values[key.name] = value

    def require(self, key: CapabilityKey[T]) -> T:
        """Return a capability or fail closed when it is absent."""

        try:
            value = self._values[key.name]
        except KeyError as exc:
            raise MissingCapabilityError(cast(CapabilityKey[object], key)) from exc
        return cast(T, value)

    def get(self, key: CapabilityKey[T]) -> T | None:
        """Return an optional capability without weakening ``require``."""

        return cast(T | None, self._values.get(key.name))

    def contains(self, key: CapabilityKey[T]) -> bool:
        """Return whether a capability with this runtime name is registered."""

        return key.name in self._values

    def __len__(self) -> int:
        return len(self._values)
