"""Fail-closed configuration for the optional executor-egress path.

The live Worker has no egress-containment configuration by default.  When the
mode is explicitly ``required``, this module accepts only an owner-provided
logical policy and the fixed helper connection facts needed for a pre-claim
readiness check.  It intentionally does not accept credentials, arbitrary
commands, WFP selectors, or direct destination addresses as Worker inputs.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, cast

from .egress import BrokerEndpoint, EgressContractError, EgressPolicy


FIXED_HELPER_TASK_NAME = "AI-Agent-Bridge-Phase86-Egress"
DEFAULT_HELPER_IPC_HOST = "127.0.0.1"
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class EgressConfigurationError(EgressContractError):
    """The optional required egress configuration is malformed or unsafe."""


class EgressActivationMode(str, Enum):
    """Explicit activation state; absence remains the safe disabled mode."""

    DISABLED = "disabled"
    REQUIRED = "required"


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise EgressConfigurationError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _strict_keys(value: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EgressConfigurationError(
            f"{label} contains unsupported fields: {', '.join(unknown)}"
        )


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EgressConfigurationError(f"{label} must be non-empty text")
    if len(value) > limit or any(character in value for character in "\x00\r\n"):
        raise EgressConfigurationError(f"{label} is outside the bounded text contract")
    return value


def _token(value: object, label: str) -> str:
    text = _text(value, label)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", text) is None:
        raise EgressConfigurationError(f"{label} is not a bounded token")
    return text


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 65535:
        raise EgressConfigurationError(f"{label} must be a TCP port from 1 through 65535")
    return value


def _logical_name(value: object, label: str) -> str:
    text = _text(value, label, limit=64).lower()
    if re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", text) is None:
        raise EgressConfigurationError(
            f"{label} must be a logical destination name, not an address"
        )
    return text


def _host_name(value: object, label: str) -> str:
    text = _text(value, label, limit=253).strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        raise EgressConfigurationError(f"{label} must be a hostname, not an IP address")
    if _HOST_RE.fullmatch(text) is None:
        raise EgressConfigurationError(f"{label} must be an exact hostname")
    return text


def _upstream_host(value: object, label: str) -> str:
    """Accept the existing local proxy endpoint or an exact configured name."""

    text = _text(value, label, limit=253).strip().lower().rstrip(".")
    if text == "127.0.0.1":
        return text
    return _host_name(text, label)


def _digest(value: object, label: str) -> str:
    text = _text(value, label, limit=64)
    if _DIGEST_RE.fullmatch(text) is None:
        raise EgressConfigurationError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise EgressConfigurationError(f"{label} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class EgressActivationConfig:
    """Owner-provided fixed policy facts for the required broker path."""

    mode: EgressActivationMode = EgressActivationMode.DISABLED
    policy_id: str | None = None
    policy_version: int | None = None
    broker_port: int | None = None
    approved_upstream: str | None = None
    logical_destinations: tuple[str, ...] = ()
    destination_hosts: tuple[tuple[str, tuple[str, ...]], ...] = ()
    upstream_host: str | None = None
    upstream_port: int | None = None
    helper_ipc_host: str = DEFAULT_HELPER_IPC_HOST
    helper_ipc_port: int | None = None
    helper_task_name: str = FIXED_HELPER_TASK_NAME
    bootstrap_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, EgressActivationMode):
            try:
                object.__setattr__(self, "mode", EgressActivationMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise EgressConfigurationError("unsupported egress activation mode") from exc
        if self.mode is EgressActivationMode.DISABLED:
            return
        if self.policy_id is None or self.policy_version is None:
            raise EgressConfigurationError("required egress policy identity is missing")
        if self.broker_port is None:
            raise EgressConfigurationError("required egress broker_port is missing")
        if self.approved_upstream is None or not self.logical_destinations:
            raise EgressConfigurationError("required logical egress policy is incomplete")
        if self.upstream_host is None or self.upstream_port is None:
            raise EgressConfigurationError("safe upstream proxy facts are missing")
        if self.helper_ipc_port is None:
            raise EgressConfigurationError("privileged helper IPC port is missing")
        if self.helper_task_name != FIXED_HELPER_TASK_NAME:
            raise EgressConfigurationError("only the fixed Phase-8.6 helper task is allowed")
        _token(self.policy_id, "policy_id")
        if isinstance(self.policy_version, bool) or not isinstance(self.policy_version, int) or self.policy_version < 1:
            raise EgressConfigurationError("policy_version must be an integer >= 1")
        _positive_int(self.broker_port, "broker_port")
        _logical_name(self.approved_upstream, "approved_upstream")
        names = tuple(_logical_name(item, "logical_destinations entry") for item in self.logical_destinations)
        if len(set(names)) != len(names):
            raise EgressConfigurationError("logical_destinations must be unique")
        object.__setattr__(self, "logical_destinations", tuple(sorted(names)))
        host_map: list[tuple[str, tuple[str, ...]]] = []
        for raw_name, raw_hosts in self.destination_hosts:
            name = _logical_name(raw_name, "destination_hosts name")
            if not isinstance(raw_hosts, tuple) or not raw_hosts:
                raise EgressConfigurationError("destination_hosts entries must be non-empty tuples")
            hosts = tuple(_host_name(item, "destination_hosts hostname") for item in raw_hosts)
            if len(set(hosts)) != len(hosts):
                raise EgressConfigurationError("destination_hosts hostnames must be unique")
            host_map.append((name, tuple(sorted(hosts))))
        if len({name for name, _ in host_map}) != len(host_map):
            raise EgressConfigurationError("destination_hosts logical names must be unique")
        if {name for name, _ in host_map} != set(names):
            raise EgressConfigurationError("every logical destination needs an exact hostname mapping")
        object.__setattr__(self, "destination_hosts", tuple(sorted(host_map)))
        if self.helper_ipc_host != DEFAULT_HELPER_IPC_HOST:
            raise EgressConfigurationError("helper IPC must use 127.0.0.1")
        _positive_int(self.helper_ipc_port, "helper_ipc_port")
        _upstream_host(self.upstream_host, "upstream_host")
        _positive_int(self.upstream_port, "upstream_port")
        if self.bootstrap_digest is not None:
            _digest(self.bootstrap_digest, "bootstrap_digest")

    @property
    def required(self) -> bool:
        return self.mode is EgressActivationMode.REQUIRED

    @classmethod
    def disabled(cls) -> "EgressActivationConfig":
        return cls()

    @classmethod
    def from_worker_config(cls, config: Mapping[str, object]) -> "EgressActivationConfig":
        """Read the optional gate without making disabled config observable."""

        if not isinstance(config, Mapping):
            raise EgressConfigurationError("Worker config must be a mapping")
        raw = config.get("egress_containment")
        if raw is None:
            return cls.disabled()
        data = _mapping(raw, "egress_containment")
        allowed = frozenset(
            {
                "enabled",
                "mode",
                "policy_id",
                "policy_version",
                "broker_port",
                "approved_upstream",
                "logical_destinations",
                "destination_hosts",
                "upstream",
                "helper",
                "bootstrap_digest",
            }
        )
        _strict_keys(data, allowed, "egress_containment")
        raw_mode = data.get("mode")
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise EgressConfigurationError("egress_containment.enabled must be boolean")
        if raw_mode is None:
            mode = EgressActivationMode.REQUIRED if enabled else EgressActivationMode.DISABLED
        else:
            try:
                mode = EgressActivationMode(str(raw_mode).strip().lower())
            except ValueError as exc:
                raise EgressConfigurationError("egress_containment.mode is unsupported") from exc
        if mode is EgressActivationMode.DISABLED:
            return cls.disabled()
        if not enabled and raw_mode is not None:
            raise EgressConfigurationError("required egress mode must also set enabled=true")

        destinations_raw = data.get("logical_destinations")
        if not isinstance(destinations_raw, (list, tuple)):
            raise EgressConfigurationError("logical_destinations must be a list")
        hosts_raw = _mapping(data.get("destination_hosts"), "destination_hosts")
        destination_hosts: list[tuple[str, tuple[str, ...]]] = []
        for name, values in hosts_raw.items():
            if not isinstance(values, (list, tuple)):
                raise EgressConfigurationError("destination_hosts values must be lists")
            destination_hosts.append((name, tuple(values)))
        upstream = _mapping(data.get("upstream"), "upstream")
        _strict_keys(upstream, frozenset({"host", "port"}), "upstream")
        helper = _mapping(data.get("helper"), "helper")
        _strict_keys(
            helper,
            frozenset({"ipc_host", "ipc_port", "task_name"}),
            "helper",
        )
        return cls(
            mode=mode,
            policy_id=_token(data.get("policy_id"), "policy_id"),
            policy_version=data.get("policy_version"),
            broker_port=data.get("broker_port"),
            approved_upstream=_logical_name(data.get("approved_upstream"), "approved_upstream"),
            logical_destinations=tuple(destinations_raw),
            destination_hosts=tuple(destination_hosts),
            upstream_host=_upstream_host(upstream.get("host"), "upstream.host"),
            upstream_port=upstream.get("port"),
            helper_ipc_host=_text(
                helper.get("ipc_host", DEFAULT_HELPER_IPC_HOST),
                "helper.ipc_host",
                limit=15,
            ),
            helper_ipc_port=helper.get("ipc_port"),
            helper_task_name=_text(
                helper.get("task_name", FIXED_HELPER_TASK_NAME),
                "helper.task_name",
                limit=128,
            ),
            bootstrap_digest=(
                _digest(data["bootstrap_digest"], "bootstrap_digest")
                if data.get("bootstrap_digest") is not None
                else None
            ),
        )

    def policy(self, broker: BrokerEndpoint | None = None) -> EgressPolicy:
        """Build the fixed policy only for explicitly required mode."""

        if not self.required:
            raise EgressConfigurationError("disabled egress containment has no policy")
        endpoint = broker or BrokerEndpoint("127.0.0.1", cast(int, self.broker_port))
        return EgressPolicy(
            policy_id=cast(str, self.policy_id),
            policy_version=cast(int, self.policy_version),
            broker=endpoint,
            approved_upstream=cast(str, self.approved_upstream),
            logical_destinations=cast(tuple[str, ...], self.logical_destinations),
        )

    def destination_host_map(self) -> dict[str, tuple[str, ...]]:
        """Return a copy of the exact logical-to-host allowlist."""

        if not self.required:
            return {}
        return {name: tuple(hosts) for name, hosts in self.destination_hosts}


__all__ = [
    "DEFAULT_HELPER_IPC_HOST",
    "EgressActivationConfig",
    "EgressActivationMode",
    "EgressConfigurationError",
    "FIXED_HELPER_TASK_NAME",
]
