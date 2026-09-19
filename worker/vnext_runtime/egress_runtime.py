"""Runtime composition for the broker/helper boundary.

This module is the only small composition layer between the pure contracts and
the mature Worker lifecycle.  It performs no canonical writes.  Required mode
always verifies helper readiness before claim, prepares broker and containment
before process creation, and releases only the exact RunScope-owned resources.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .egress import (
    ContainmentState,
    ContainmentIdentity,
    EgressPolicy,
    HelperEnforcement,
    HelperReceiptStatus,
    PrivilegedHelperTransport,
    RunContainment,
)
from .egress_broker import (
    BrokerLedgerSnapshot,
    BrokerState,
    LocalEgressBroker,
    UpstreamProxyEndpoint,
)
from .egress_config import EgressActivationConfig, EgressConfigurationError
from .models import RunIdentity
from .privileged_helper import FixedHelperClient, LocalHelperTransport
from .process_boundary import ProcessBoundaryBinding


WalEventLogger = Callable[[str, dict[str, object]], None]


class EgressRuntimeError(RuntimeError):
    """A required exact-run egress boundary cannot be proven."""


class EgressRuntimeState(str, Enum):
    NEW = "NEW"
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    RELEASING = "RELEASING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EgressPreflight:
    """Read-only pre-claim result; it contains no broker or process handle."""

    activation: EgressActivationConfig
    policy: EgressPolicy | None
    helper: FixedHelperClient | None
    ready: bool
    reason: str


def helper_client_from_config(
    activation: EgressActivationConfig,
    *,
    timeout: float = 5.0,
) -> FixedHelperClient:
    """Construct the fixed loopback helper client from safe config facts."""

    if not activation.required:
        raise EgressConfigurationError("disabled egress mode has no helper client")
    if activation.helper_ipc_port is None:
        raise EgressConfigurationError("helper IPC port is missing")
    return FixedHelperClient(
        LocalHelperTransport(
            activation.helper_ipc_host,
            activation.helper_ipc_port,
            timeout=timeout,
        )
    )


def preclaim_egress(
    config: Mapping[str, object],
    identity: RunIdentity,
    *,
    helper: FixedHelperClient | None = None,
    process_launcher: object | None = None,
    request_id: str | None = None,
) -> EgressPreflight:
    """Check required helper readiness without creating run-owned resources."""

    activation = EgressActivationConfig.from_worker_config(config)
    if not activation.required:
        return EgressPreflight(activation, None, None, True, "disabled")
    if (
        process_launcher is None
        or getattr(process_launcher, "is_contained", False) is not True
        or not callable(getattr(process_launcher, "bind_containment", None))
    ):
        raise EgressRuntimeError("contained process launcher is not installed")
    client = helper or helper_client_from_config(activation)
    policy = activation.policy()
    try:
        receipt = client.inspect_health(
            identity,
            policy.reference(),
            request_id or str(uuid.uuid4()),
        )
    except BaseException as exc:
        raise EgressRuntimeError("privileged helper readiness check failed") from exc
    if (
        receipt.status is not HelperReceiptStatus.HEALTHY
        or receipt.enforcement is not HelperEnforcement.READY
    ):
        raise EgressRuntimeError("privileged helper did not prove readiness")
    return EgressPreflight(activation, policy, client, True, "helper_ready")


class RunEgressResources:
    """One broker and one helper containment lifecycle owned by one RunScope."""

    __slots__ = (
        "identity",
        "activation",
        "policy",
        "helper",
        "process_launcher",
        "run_dir",
        "event_logger",
        "state",
        "broker",
        "containment",
        "_containment_attempted",
        "_binding_verified",
        "_binding",
    )

    def __init__(
        self,
        identity: RunIdentity,
        activation: EgressActivationConfig,
        *,
        helper: PrivilegedHelperTransport,
        process_launcher: object,
        run_dir: Path | None = None,
        event_logger: WalEventLogger | None = None,
    ) -> None:
        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not activation.required:
            raise EgressRuntimeError("RunEgressResources requires required mode")
        if not hasattr(helper, "exchange"):
            raise TypeError("helper must implement typed exchange")
        if (
            process_launcher is None
            or getattr(process_launcher, "is_contained", False) is not True
            or not callable(getattr(process_launcher, "launch", None))
            or not callable(getattr(process_launcher, "bind_containment", None))
        ):
            raise EgressRuntimeError("contained process launcher is not installed")
        if run_dir is not None and not isinstance(run_dir, Path):
            raise TypeError("run_dir must be a pathlib.Path")
        self.identity = identity
        self.activation = activation
        self.policy = activation.policy()
        self.helper = helper
        self.process_launcher = process_launcher
        self.run_dir = run_dir
        self.event_logger = event_logger
        self.state = EgressRuntimeState.NEW
        self.broker: LocalEgressBroker | None = None
        self.containment: RunContainment | None = None
        self._containment_attempted = False
        self._binding_verified = False
        self._binding: ProcessBoundaryBinding | None = None

    @property
    def ledger(self) -> BrokerLedgerSnapshot | None:
        if self.broker is None:
            return None
        return self.broker.ledger()

    @property
    def process_boundary_ready(self) -> bool:
        """Whether the exact helper identity is pinned into the launcher."""

        return self._binding_verified and self._binding is not None and getattr(
            self.process_launcher, "bound_binding", None
        ) == self._binding

    def prepare(self) -> None:
        """Start broker, then prove exact containment before process creation."""

        if self.state is not EgressRuntimeState.NEW:
            raise EgressRuntimeError(f"egress prepare requires NEW, got {self.state.value}")
        self.state = EgressRuntimeState.PREPARING
        ledger_path = self.run_dir / "egress-ledger.json" if self.run_dir else None
        try:
            self.broker = LocalEgressBroker(
                self.identity,
                self.policy,
                UpstreamProxyEndpoint(
                    str(self.activation.upstream_host),
                    int(self.activation.upstream_port),
                ),
                self.activation.destination_host_map(),
                ledger_path=ledger_path,
            )
            self.broker.start()
            self._emit("broker_started", {"artifact_digest": self.policy.digest})
            self.containment = RunContainment(
                self.identity,
                self.policy.reference(),
                self.helper,
            )
            self._containment_attempted = True
            receipt = self.containment.prepare()
            if receipt.enforcement is not HelperEnforcement.ACTIVE:
                raise EgressRuntimeError("helper did not prove active containment")
            binding = ProcessBoundaryBinding(
                self.identity,
                ContainmentIdentity.derive(self.identity),
                receipt.resource_digest,
            )
            binder = getattr(self.process_launcher, "bind_containment", None)
            if not callable(binder):
                raise EgressRuntimeError(
                    "contained launcher cannot bind the exact helper identity"
                )
            binder(binding)
            if getattr(self.process_launcher, "bound_binding", None) != binding:
                raise EgressRuntimeError(
                    "contained launcher did not retain the exact helper identity"
                )
            self._binding = binding
            self._binding_verified = True
            self._emit(
                "containment_prepared",
                {"artifact_digest": receipt.resource_digest},
            )
            self.state = EgressRuntimeState.PREPARED
        except BaseException:
            self.state = EgressRuntimeState.UNKNOWN
            raise

    def dispose(self) -> None:
        """Stop broker first, reconcile, then release only exact helper state."""

        if self.state is EgressRuntimeState.CLOSED:
            return
        self.state = EgressRuntimeState.RELEASING
        errors: list[BaseException] = []
        final_ledger: BrokerLedgerSnapshot | None = None
        ledger_digest: str | None = None
        if self.broker is not None:
            broker_was_active = self.broker.state is BrokerState.ACTIVE
            try:
                self.broker.dispose()
                if broker_was_active:
                    self._emit("broker_stopped", {"artifact_digest": self.policy.digest})
                final_ledger = self.broker.ledger()
                ledger_digest = hashlib.sha256(
                    json.dumps(
                        final_ledger.to_wire(),
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            except BaseException as exc:
                errors.append(exc)
        if self.containment is not None and self._containment_attempted:
            containment_release_expected = self.containment.state in {
                ContainmentState.PREPARED,
                ContainmentState.RECONCILED,
            }
            try:
                if self.containment.state in {
                    ContainmentState.PREPARED,
                    ContainmentState.RECONCILED,
                }:
                    receipt = self.containment.reconcile()
                    self._emit(
                        "containment_reconciled",
                        {
                            "artifact_digest": receipt.resource_digest,
                            "project_egress_observed": (
                                "YES"
                                if final_ledger is not None
                                and final_ledger.connections_observed > 0
                                else "NO"
                                if final_ledger is not None
                                else None
                            ),
                            "project_egress_artifact_digest": ledger_digest,
                        },
                    )
            except BaseException as exc:
                errors.append(exc)
            try:
                self.containment.dispose()
                receipt = self.containment.last_receipt
                if containment_release_expected and self.containment.state is ContainmentState.RELEASED:
                    self._emit(
                        "containment_released",
                        {
                            "artifact_digest": (
                                receipt.resource_digest
                                if receipt is not None
                                else self.policy.digest
                            )
                        },
                    )
            except BaseException as exc:
                errors.append(exc)
        self.state = EgressRuntimeState.UNKNOWN if errors else EgressRuntimeState.CLOSED
        if errors:
            raise EgressRuntimeError("exact egress cleanup could not be proven") from errors[0]

    close = dispose

    def _emit(self, event: str, fields: dict[str, object]) -> None:
        if self.event_logger is None:
            return
        self.event_logger(event, fields)


__all__ = [
    "EgressPreflight",
    "EgressRuntimeError",
    "EgressRuntimeState",
    "RunEgressResources",
    "helper_client_from_config",
    "preclaim_egress",
]
