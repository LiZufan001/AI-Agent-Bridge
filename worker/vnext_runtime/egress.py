"""Pure egress policy and privileged-helper contracts.

This module is the narrow, testable boundary selected for the production
egress design.  It deliberately contains no Windows, WFP, AppContainer,
socket, subprocess, filesystem, or canonical-state operations.  A future
transport may activate the fixed helper, but the Worker can exchange only the
typed requests and receipts defined here.

The request carries an exact proposed/claimed Protocol-v2 run identity.  The
helper derives the AppContainer and WFP object identity from that run; the
Worker cannot supply arbitrary executable paths, addresses, layers, filters,
or delete targets.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, TypeVar, cast

from .models import RunIdentity


EGRESS_POLICY_SCHEMA_VERSION = 1
HELPER_PROTOCOL_SCHEMA_VERSION = 1
CONTAINMENT_DERIVATION_VERSION = "phase86-c-containment-v1"
HELPER_PROTOCOL_NAME = "ai-agent-bridge.phase86.egress"
LOOPBACK_HOST = "127.0.0.1"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOGICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_NAMESPACE = uuid.UUID("b6cb0f4d-86c0-4b85-a2a7-55ecb9ed92b1")


class EgressContractError(ValueError):
    """A policy, identity, request, or receipt is unsafe or malformed."""


class HelperProtocolError(EgressContractError):
    """A helper response is not bound to the exact request that created it."""


class EgressContainmentError(RuntimeError):
    """A run-owned containment transition could not be proven successful."""


class HelperOperation(str, Enum):
    """The complete fixed operation vocabulary exposed to the Worker."""

    INSPECT_HEALTH = "INSPECT_HEALTH"
    PREPARE_RUN = "PREPARE_RUN"
    RECONCILE_RUN = "RECONCILE_RUN"
    RELEASE_RUN = "RELEASE_RUN"


class HelperReceiptStatus(str, Enum):
    """Operation-specific bounded helper outcomes."""

    HEALTHY = "HEALTHY"
    PREPARED = "PREPARED"
    RECONCILED = "RECONCILED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"


class HelperEnforcement(str, Enum):
    """What the receipt proves about the exact disposable run identity."""

    READY = "READY"
    ACTIVE = "ACTIVE"
    DENY_RETAINED = "DENY_RETAINED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class ContainmentState(str, Enum):
    """Local lifecycle of one exact-run containment handle."""

    NEW = "NEW"
    PREPARED = "PREPARED"
    RECONCILED = "RECONCILED"
    RELEASED = "RELEASED"
    UNKNOWN = "UNKNOWN"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _require_text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{label} must be non-empty text")
    if len(value) > limit or "\x00" in value or "\r" in value or "\n" in value:
        raise EgressContractError(f"{label} is outside the bounded text contract")
    return value


def _require_token(value: object, label: str) -> str:
    text = _require_text(value, label)
    if _TOKEN_RE.fullmatch(text) is None:
        raise EgressContractError(f"{label} is not a bounded protocol token")
    return text


def _require_logical_name(value: object, label: str) -> str:
    text = _require_text(value, label, limit=64)
    if _LOGICAL_NAME_RE.fullmatch(text) is None:
        raise EgressContractError(
            f"{label} must be a logical destination name, not an address"
        )
    return text


def _require_int(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EgressContractError(f"{label} must be an integer >= {minimum}")
    return value


def _require_digest(value: object, label: str) -> str:
    text = _require_text(value, label, limit=64)
    if _DIGEST_RE.fullmatch(text) is None:
        raise EgressContractError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _enum_value(enum_type: type[_EnumT], value: object, label: str) -> _EnumT:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise EgressContractError(f"{label} has an unsupported value") from exc


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value.keys()
    ):
        raise EgressContractError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _strict_keys(
    value: Mapping[str, object], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if unknown:
        raise EgressContractError(
            f"{label} contains unsupported fields: {', '.join(unknown)}"
        )
    if missing:
        raise EgressContractError(
            f"{label} is missing fields: {', '.join(missing)}"
        )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity_wire(identity: RunIdentity) -> dict[str, object]:
    return {
        "project_id": identity.project_id,
        "command_id": identity.command_id,
        "run_id": identity.run_id,
        "claim_generation": identity.claim_generation,
    }


def _identity_from_wire(value: object) -> RunIdentity:
    data = _mapping(value, "identity")
    _strict_keys(
        data,
        frozenset({"project_id", "command_id", "run_id", "claim_generation"}),
        "identity",
    )
    try:
        return RunIdentity(
            project_id=data["project_id"],
            command_id=data["command_id"],
            run_id=data["run_id"],
            claim_generation=data["claim_generation"],
        )
    except (TypeError, ValueError) as exc:
        raise EgressContractError("identity is not a valid exact run identity") from exc


@dataclass(frozen=True, slots=True)
class BrokerEndpoint:
    """The only endpoint the contained process may receive from the Worker."""

    host: str
    port: int
    transport: str = "tcp"

    def __post_init__(self) -> None:
        if self.host != LOOPBACK_HOST:
            raise EgressContractError(
                "the egress helper contract accepts only 127.0.0.1"
            )
        _require_text(self.host, "broker.host", limit=15)
        if self.transport != "tcp":
            raise EgressContractError("the egress helper contract accepts only TCP")
        _require_int(self.port, "broker.port", minimum=1)
        if self.port > 65535:
            raise EgressContractError("broker.port is outside the TCP port range")

    def to_wire(self) -> dict[str, object]:
        """Return the bounded endpoint representation used on the wire."""

        return {"host": self.host, "port": self.port, "transport": self.transport}

    @classmethod
    def from_wire(cls, value: object) -> "BrokerEndpoint":
        """Parse one strict loopback endpoint from a wire mapping."""

        data = _mapping(value, "broker")
        _strict_keys(data, frozenset({"host", "port", "transport"}), "broker")
        try:
            return cls(
                host=data["host"],
                port=data["port"],
                transport=data["transport"],
            )
        except (TypeError, ValueError) as exc:
            raise EgressContractError("broker endpoint is invalid") from exc


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """An owner-provisioned logical policy, never a raw WFP rule description."""

    policy_id: str
    policy_version: int
    broker: BrokerEndpoint
    approved_upstream: str
    logical_destinations: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_token(self.policy_id, "policy_id")
        _require_int(self.policy_version, "policy_version", minimum=1)
        if not isinstance(self.broker, BrokerEndpoint):
            raise EgressContractError("policy broker must be a BrokerEndpoint")
        _require_logical_name(self.approved_upstream, "approved_upstream")
        if not isinstance(self.logical_destinations, tuple) or not self.logical_destinations:
            raise EgressContractError(
                "logical_destinations must be a non-empty immutable tuple"
            )
        names = tuple(
            _require_logical_name(value, "logical_destinations entry")
            for value in self.logical_destinations
        )
        if len(set(names)) != len(names):
            raise EgressContractError("logical_destinations must not contain duplicates")
        object.__setattr__(self, "logical_destinations", tuple(sorted(names)))

    def to_wire(self) -> dict[str, object]:
        """Return the stable, address-free policy representation."""

        return {
            "schema_version": EGRESS_POLICY_SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "broker": self.broker.to_wire(),
            "approved_upstream": self.approved_upstream,
            "logical_destinations": list(self.logical_destinations),
        }

    @property
    def digest(self) -> str:
        """Return the stable policy digest pinned into every helper request."""

        return _digest(self.to_wire())

    def reference(self) -> "EgressPolicyReference":
        """Return the request-safe reference without raw upstream details."""

        return EgressPolicyReference(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            policy_digest=self.digest,
            broker=self.broker,
        )


@dataclass(frozen=True, slots=True)
class EgressPolicyReference:
    """The policy facts a Worker may bind to a helper request."""

    policy_id: str
    policy_version: int
    policy_digest: str
    broker: BrokerEndpoint

    def __post_init__(self) -> None:
        _require_token(self.policy_id, "policy_id")
        _require_int(self.policy_version, "policy_version", minimum=1)
        _require_digest(self.policy_digest, "policy_digest")
        if not isinstance(self.broker, BrokerEndpoint):
            raise EgressContractError("policy reference broker must be a BrokerEndpoint")

    def to_wire(self) -> dict[str, object]:
        """Return the reference fields used in a request or receipt."""

        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "broker": self.broker.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: object) -> "EgressPolicyReference":
        """Parse one strict policy reference from a wire mapping."""

        data = _mapping(value, "policy")
        _strict_keys(
            data,
            frozenset({"policy_id", "policy_version", "policy_digest", "broker"}),
            "policy",
        )
        try:
            return cls(
                policy_id=data["policy_id"],
                policy_version=data["policy_version"],
                policy_digest=data["policy_digest"],
                broker=BrokerEndpoint.from_wire(data["broker"]),
            )
        except (TypeError, ValueError) as exc:
            raise EgressContractError("policy reference is invalid") from exc


@dataclass(frozen=True, slots=True)
class ContainmentIdentity:
    """Deterministic disposable identity derived from all exact run fields."""

    derivation_version: str
    identity_digest: str
    profile_name: str

    def __post_init__(self) -> None:
        if self.derivation_version != CONTAINMENT_DERIVATION_VERSION:
            raise EgressContractError("unsupported containment derivation version")
        _require_digest(self.identity_digest, "containment.identity_digest")
        _require_token(self.profile_name, "containment.profile_name")
        expected_prefix = "bridge_phase86_c_"
        if not self.profile_name.startswith(expected_prefix):
            raise EgressContractError("containment profile name has an invalid prefix")
        if self.profile_name != expected_prefix + self.identity_digest[:32]:
            raise EgressContractError(
                "containment profile name is not derived from identity_digest"
            )

    @classmethod
    def derive(cls, identity: RunIdentity) -> "ContainmentIdentity":
        """Derive the exact profile identity without accepting caller keys."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        identity_digest = _digest(_identity_wire(identity))
        return cls(
            derivation_version=CONTAINMENT_DERIVATION_VERSION,
            identity_digest=identity_digest,
            profile_name="bridge_phase86_c_" + identity_digest[:32],
        )

    def to_wire(self) -> dict[str, object]:
        """Return the exact identity derivation evidence."""

        return {
            "derivation_version": self.derivation_version,
            "identity_digest": self.identity_digest,
            "profile_name": self.profile_name,
        }

    @classmethod
    def from_wire(cls, value: object) -> "ContainmentIdentity":
        """Parse one strict derived containment identity."""

        data = _mapping(value, "containment")
        _strict_keys(
            data,
            frozenset({"derivation_version", "identity_digest", "profile_name"}),
            "containment",
        )
        try:
            return cls(
                derivation_version=data["derivation_version"],
                identity_digest=data["identity_digest"],
                profile_name=data["profile_name"],
            )
        except (TypeError, ValueError) as exc:
            raise EgressContractError("containment identity is invalid") from exc


@dataclass(frozen=True, slots=True)
class HelperRequest:
    """One schema-versioned, exact-run request to the fixed helper."""

    schema_version: int
    request_id: str
    operation: HelperOperation
    identity: RunIdentity
    policy: EgressPolicyReference
    containment: ContainmentIdentity

    def __post_init__(self) -> None:
        _require_int(self.schema_version, "request.schema_version", minimum=1)
        if self.schema_version != HELPER_PROTOCOL_SCHEMA_VERSION:
            raise EgressContractError("unsupported helper request schema_version")
        _require_token(self.request_id, "request_id")
        object.__setattr__(
            self,
            "operation",
            _enum_value(HelperOperation, self.operation, "operation"),
        )
        if not isinstance(self.identity, RunIdentity):
            raise EgressContractError("request identity must be a RunIdentity")
        if not isinstance(self.policy, EgressPolicyReference):
            raise EgressContractError("request policy must be an EgressPolicyReference")
        if not isinstance(self.containment, ContainmentIdentity):
            raise EgressContractError(
                "request containment must be a ContainmentIdentity"
            )
        if self.containment != ContainmentIdentity.derive(self.identity):
            raise EgressContractError(
                "request containment is not derived from the exact run identity"
            )

    @classmethod
    def for_operation(
        cls,
        operation: HelperOperation,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        request_id: str,
    ) -> "HelperRequest":
        """Construct a request with only the fixed operation vocabulary."""

        return cls(
            schema_version=HELPER_PROTOCOL_SCHEMA_VERSION,
            request_id=request_id,
            operation=operation,
            identity=identity,
            policy=policy,
            containment=ContainmentIdentity.derive(identity),
        )

    def to_wire(self) -> dict[str, object]:
        """Return a strict JSON-compatible request object."""

        return {
            "protocol": HELPER_PROTOCOL_NAME,
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "identity": _identity_wire(self.identity),
            "policy": self.policy.to_wire(),
            "containment": self.containment.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: object) -> "HelperRequest":
        """Parse a request while rejecting unknown fields and identity drift."""

        data = _mapping(value, "helper request")
        _strict_keys(
            data,
            frozenset(
                {
                    "protocol",
                    "schema_version",
                    "request_id",
                    "operation",
                    "identity",
                    "policy",
                    "containment",
                }
            ),
            "helper request",
        )
        if data["protocol"] != HELPER_PROTOCOL_NAME:
            raise EgressContractError("unsupported helper protocol")
        try:
            return cls(
                schema_version=data["schema_version"],
                request_id=data["request_id"],
                operation=data["operation"],
                identity=_identity_from_wire(data["identity"]),
                policy=EgressPolicyReference.from_wire(data["policy"]),
                containment=ContainmentIdentity.from_wire(data["containment"]),
            )
        except (TypeError, ValueError) as exc:
            raise EgressContractError("helper request is invalid") from exc

    def encode(self) -> str:
        """Encode this request as deterministic JSON for a future transport."""

        return encode_request(self)


def encode_request(request: HelperRequest) -> str:
    """Encode one typed request without adding an arbitrary payload channel."""

    if not isinstance(request, HelperRequest):
        raise TypeError("request must be a HelperRequest")
    return _canonical_json(request.to_wire())


def decode_request(value: str) -> HelperRequest:
    """Decode one deterministic JSON request through the strict contract."""

    if not isinstance(value, str):
        raise TypeError("wire request must be text")
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EgressContractError("helper request is not valid JSON") from exc
    return HelperRequest.from_wire(parsed)


@dataclass(frozen=True, slots=True)
class HelperReceipt:
    """One bounded receipt that is cryptographically tied to request facts."""

    schema_version: int
    request_id: str
    operation: HelperOperation
    identity: RunIdentity
    policy: EgressPolicyReference
    containment: ContainmentIdentity
    status: HelperReceiptStatus
    enforcement: HelperEnforcement
    observed_at: str
    resource_digest: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.schema_version, "receipt.schema_version", minimum=1)
        if self.schema_version != HELPER_PROTOCOL_SCHEMA_VERSION:
            raise EgressContractError("unsupported helper receipt schema_version")
        _require_token(self.request_id, "receipt.request_id")
        object.__setattr__(
            self,
            "operation",
            _enum_value(HelperOperation, self.operation, "receipt.operation"),
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(HelperReceiptStatus, self.status, "receipt.status"),
        )
        object.__setattr__(
            self,
            "enforcement",
            _enum_value(
                HelperEnforcement, self.enforcement, "receipt.enforcement"
            ),
        )
        if not isinstance(self.identity, RunIdentity):
            raise EgressContractError("receipt identity must be a RunIdentity")
        if not isinstance(self.policy, EgressPolicyReference):
            raise EgressContractError("receipt policy must be an EgressPolicyReference")
        if not isinstance(self.containment, ContainmentIdentity):
            raise EgressContractError(
                "receipt containment must be a ContainmentIdentity"
            )
        if self.containment != ContainmentIdentity.derive(self.identity):
            raise EgressContractError(
                "receipt containment is not derived from the exact run identity"
            )
        _require_text(self.observed_at, "receipt.observed_at", limit=128)
        _require_digest(self.resource_digest, "receipt.resource_digest")
        expected = _resource_digest(self.policy, self.containment)
        if self.resource_digest != expected:
            raise EgressContractError(
                "receipt resource_digest does not match policy and containment"
            )
        if self.error_code is not None:
            _require_token(self.error_code, "receipt.error_code")

        if self.status is HelperReceiptStatus.HEALTHY:
            if self.operation is not HelperOperation.INSPECT_HEALTH:
                raise EgressContractError("HEALTHY is valid only for INSPECT_HEALTH")
            if self.enforcement is not HelperEnforcement.READY:
                raise EgressContractError("HEALTHY must prove helper readiness")
        elif self.status is HelperReceiptStatus.PREPARED:
            if self.operation is not HelperOperation.PREPARE_RUN:
                raise EgressContractError("PREPARED is valid only for PREPARE_RUN")
            if self.enforcement is not HelperEnforcement.ACTIVE:
                raise EgressContractError("PREPARED must prove active containment")
        elif self.status is HelperReceiptStatus.RECONCILED:
            if self.operation is not HelperOperation.RECONCILE_RUN:
                raise EgressContractError("RECONCILED is valid only for RECONCILE_RUN")
            if self.enforcement not in {
                HelperEnforcement.ACTIVE,
                HelperEnforcement.DENY_RETAINED,
            }:
                raise EgressContractError(
                    "RECONCILED must prove active or restrictive containment"
                )
        elif self.status is HelperReceiptStatus.RELEASED:
            if self.operation is not HelperOperation.RELEASE_RUN:
                raise EgressContractError("RELEASED is valid only for RELEASE_RUN")
            if self.enforcement not in {
                HelperEnforcement.INACTIVE,
                HelperEnforcement.DENY_RETAINED,
            }:
                raise EgressContractError(
                    "RELEASED must prove inactive or deny-retained containment"
                )
        elif self.status is HelperReceiptStatus.REJECTED:
            if self.enforcement is not HelperEnforcement.UNKNOWN:
                raise EgressContractError("REJECTED must leave enforcement UNKNOWN")
            if self.error_code is None:
                raise EgressContractError("REJECTED must include a bounded error_code")
        if self.status is not HelperReceiptStatus.REJECTED and self.error_code is not None:
            raise EgressContractError("successful receipts cannot contain error_code")

    @classmethod
    def for_request(
        cls,
        request: HelperRequest,
        status: HelperReceiptStatus,
        enforcement: HelperEnforcement,
        observed_at: str,
        error_code: str | None = None,
    ) -> "HelperReceipt":
        """Build a receipt whose resource digest matches one request."""

        if not isinstance(request, HelperRequest):
            raise TypeError("request must be a HelperRequest")
        return cls(
            schema_version=HELPER_PROTOCOL_SCHEMA_VERSION,
            request_id=request.request_id,
            operation=request.operation,
            identity=request.identity,
            policy=request.policy,
            containment=request.containment,
            status=status,
            enforcement=enforcement,
            observed_at=observed_at,
            resource_digest=_resource_digest(request.policy, request.containment),
            error_code=error_code,
        )

    def verify_for(self, request: HelperRequest) -> None:
        """Reject a receipt from another request, run, policy, or derivation."""

        if not isinstance(request, HelperRequest):
            raise TypeError("request must be a HelperRequest")
        if (
            self.request_id != request.request_id
            or self.operation is not request.operation
            or self.identity != request.identity
            or self.policy != request.policy
            or self.containment != request.containment
        ):
            raise HelperProtocolError(
                "helper receipt is not bound to the exact request identity"
            )

    def to_wire(self) -> dict[str, object]:
        """Return a strict JSON-compatible receipt object."""

        return {
            "protocol": HELPER_PROTOCOL_NAME,
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "identity": _identity_wire(self.identity),
            "policy": self.policy.to_wire(),
            "containment": self.containment.to_wire(),
            "status": self.status.value,
            "enforcement": self.enforcement.value,
            "observed_at": self.observed_at,
            "resource_digest": self.resource_digest,
            "error_code": self.error_code,
        }

    @classmethod
    def from_wire(cls, value: object) -> "HelperReceipt":
        """Parse a strict receipt without trusting unbound helper output."""

        data = _mapping(value, "helper receipt")
        _strict_keys(
            data,
            frozenset(
                {
                    "protocol",
                    "schema_version",
                    "request_id",
                    "operation",
                    "identity",
                    "policy",
                    "containment",
                    "status",
                    "enforcement",
                    "observed_at",
                    "resource_digest",
                    "error_code",
                }
            ),
            "helper receipt",
        )
        if data["protocol"] != HELPER_PROTOCOL_NAME:
            raise EgressContractError("unsupported helper protocol")
        try:
            return cls(
                schema_version=data["schema_version"],
                request_id=data["request_id"],
                operation=data["operation"],
                identity=_identity_from_wire(data["identity"]),
                policy=EgressPolicyReference.from_wire(data["policy"]),
                containment=ContainmentIdentity.from_wire(data["containment"]),
                status=data["status"],
                enforcement=data["enforcement"],
                observed_at=data["observed_at"],
                resource_digest=data["resource_digest"],
                error_code=data["error_code"],
            )
        except (TypeError, ValueError) as exc:
            raise EgressContractError("helper receipt is invalid") from exc

    def encode(self) -> str:
        """Encode this receipt as deterministic JSON."""

        return encode_receipt(self)


def _resource_digest(
    policy: EgressPolicyReference, containment: ContainmentIdentity
) -> str:
    return _digest(
        {
            "policy": policy.to_wire(),
            "containment": containment.to_wire(),
        }
    )


def encode_receipt(receipt: HelperReceipt) -> str:
    """Encode one typed receipt as deterministic JSON."""

    if not isinstance(receipt, HelperReceipt):
        raise TypeError("receipt must be a HelperReceipt")
    return _canonical_json(receipt.to_wire())


def decode_receipt(value: str) -> HelperReceipt:
    """Decode one strict JSON receipt through the helper contract."""

    if not isinstance(value, str):
        raise TypeError("wire receipt must be text")
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EgressContractError("helper receipt is not valid JSON") from exc
    return HelperReceipt.from_wire(parsed)


class PrivilegedHelperTransport(Protocol):
    """The only future transport surface exposed to ordinary Worker code."""

    def exchange(self, request: HelperRequest) -> HelperReceipt:
        """Exchange one typed request with the fixed-purpose helper."""


class RunContainment:
    """RunScope-compatible owner for one exact helper containment lifecycle."""

    __slots__ = (
        "_identity",
        "_policy",
        "_transport",
        "_state",
        "_prepare_attempted",
        "_last_receipt",
    )

    def __init__(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        transport: PrivilegedHelperTransport,
    ) -> None:
        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not isinstance(policy, EgressPolicyReference):
            raise TypeError("policy must be an EgressPolicyReference")
        self._identity = identity
        self._policy = policy
        self._transport = transport
        self._state = ContainmentState.NEW
        self._prepare_attempted = False
        self._last_receipt: HelperReceipt | None = None

    @property
    def identity(self) -> RunIdentity:
        """Return the immutable exact-run owner."""

        return self._identity

    @property
    def state(self) -> ContainmentState:
        """Return the local containment lifecycle state."""

        return self._state

    @property
    def last_receipt(self) -> HelperReceipt | None:
        """Return the last exact-run receipt, if one was accepted."""

        return self._last_receipt

    def prepare(self) -> HelperReceipt:
        """Prepare and verify active exact-run containment."""

        if self._state is not ContainmentState.NEW:
            raise EgressContainmentError(
                f"containment prepare requires NEW, got {self._state.value}"
            )
        self._prepare_attempted = True
        receipt = self._exchange(HelperOperation.PREPARE_RUN)
        if receipt.status is not HelperReceiptStatus.PREPARED:
            self._state = ContainmentState.UNKNOWN
            raise EgressContainmentError("helper did not prove PREPARED containment")
        self._state = ContainmentState.PREPARED
        return receipt

    def reconcile(self) -> HelperReceipt:
        """Inspect exact-run objects without widening or replacing policy."""

        if self._state not in {
            ContainmentState.PREPARED,
            ContainmentState.RECONCILED,
        }:
            raise EgressContainmentError(
                "containment reconcile requires PREPARED or RECONCILED state"
            )
        receipt = self._exchange(HelperOperation.RECONCILE_RUN)
        if receipt.status is not HelperReceiptStatus.RECONCILED:
            self._state = ContainmentState.UNKNOWN
            raise EgressContainmentError("helper did not prove RECONCILED containment")
        self._state = ContainmentState.RECONCILED
        return receipt

    def dispose(self) -> None:
        """Release only this run; failed release keeps the state uncertain."""

        if self._state is ContainmentState.RELEASED:
            return
        if not self._prepare_attempted:
            self._state = ContainmentState.RELEASED
            return
        try:
            receipt = self._exchange(HelperOperation.RELEASE_RUN)
        except BaseException:
            self._state = ContainmentState.UNKNOWN
            raise
        if receipt.status is not HelperReceiptStatus.RELEASED:
            self._state = ContainmentState.UNKNOWN
            raise EgressContainmentError("helper did not prove RELEASED containment")
        self._state = ContainmentState.RELEASED

    close = dispose

    def __enter__(self) -> "RunContainment":
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        self.dispose()

    def _exchange(self, operation: HelperOperation) -> HelperReceipt:
        request = HelperRequest.for_operation(
            operation,
            self._identity,
            self._policy,
            request_id=str(uuid.uuid4()),
        )
        try:
            receipt = self._transport.exchange(request)
        except BaseException:
            self._state = ContainmentState.UNKNOWN
            raise
        if not isinstance(receipt, HelperReceipt):
            self._state = ContainmentState.UNKNOWN
            raise HelperProtocolError("helper transport returned a non-typed receipt")
        try:
            receipt.verify_for(request)
        except BaseException:
            self._state = ContainmentState.UNKNOWN
            raise
        self._last_receipt = receipt
        if receipt.status is HelperReceiptStatus.REJECTED:
            self._state = ContainmentState.UNKNOWN
            raise EgressContainmentError(
                f"helper rejected {operation.value}: {receipt.error_code}"
            )
        return receipt


__all__ = [
    "BrokerEndpoint",
    "ContainmentIdentity",
    "ContainmentState",
    "EgressContainmentError",
    "EgressContractError",
    "EgressPolicy",
    "EgressPolicyReference",
    "HELPER_PROTOCOL_NAME",
    "HELPER_PROTOCOL_SCHEMA_VERSION",
    "HelperEnforcement",
    "HelperOperation",
    "HelperProtocolError",
    "HelperReceipt",
    "HelperReceiptStatus",
    "HelperRequest",
    "LOOPBACK_HOST",
    "PrivilegedHelperTransport",
    "RunContainment",
    "decode_request",
    "decode_receipt",
    "encode_request",
    "encode_receipt",
]
