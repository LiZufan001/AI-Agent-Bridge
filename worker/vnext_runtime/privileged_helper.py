"""Fixed-purpose privileged-helper protocol and owner bootstrap boundary.

Ordinary Worker code can exchange only :class:`HelperRequest` and
:class:`HelperReceipt` values.  The service dispatcher below has a closed
operation vocabulary and an owner-bound policy reference; it exposes no
generic command, firewall, WFP, address, or delete-target input.  The native
Windows implementation is intentionally a separately installed helper.  The
Worker never installs or elevates it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Mapping, Protocol

from .egress import (
    ContainmentIdentity,
    EgressPolicyReference,
    HelperEnforcement,
    HelperOperation,
    HelperProtocolError,
    HelperReceipt,
    HelperReceiptStatus,
    HelperRequest,
    PrivilegedHelperTransport,
    decode_receipt,
)
from .models import RunIdentity


MAX_HELPER_FRAME_BYTES = 64 * 1024
MAX_HELPER_REQUEST_HISTORY = 4096
HELPER_BOOTSTRAP_VERSION = "phase86-c-helper-v1"
HELPER_EXECUTABLE_NAME = "AI-Agent-Bridge.Phase86.Helper.exe"
_HELPER_NAMESPACE = uuid.UUID("9b2a9a26-8d20-4d75-b45d-7d1d36e3fe86")


class PrivilegedHelperError(RuntimeError):
    """The fixed helper cannot prove the requested exact-run transition."""


class PrivilegedHelperUnavailable(PrivilegedHelperError):
    """The owner-installed helper/bootstrap prerequisite is unavailable."""


class FixedWfpLayer(str, Enum):
    """The only WFP layers a production helper may derive."""

    ALE_AUTH_CONNECT_V4 = "ALE_AUTH_CONNECT_V4"
    ALE_AUTH_CONNECT_V6 = "ALE_AUTH_CONNECT_V6"


@dataclass(frozen=True, slots=True)
class FixedWfpPolicySpec:
    """Reviewable fixed semantics reproduced from the accepted B2 proof."""

    deny_layers: tuple[FixedWfpLayer, ...] = (
        FixedWfpLayer.ALE_AUTH_CONNECT_V4,
        FixedWfpLayer.ALE_AUTH_CONNECT_V6,
    )
    allow_layer: FixedWfpLayer = FixedWfpLayer.ALE_AUTH_CONNECT_V4
    allow_host: str = "127.0.0.1"
    allow_protocol: str = "tcp"

    def __post_init__(self) -> None:
        if self.deny_layers != (
            FixedWfpLayer.ALE_AUTH_CONNECT_V4,
            FixedWfpLayer.ALE_AUTH_CONNECT_V6,
        ):
            raise PrivilegedHelperError("fixed helper deny layer set cannot be changed")
        if self.allow_layer is not FixedWfpLayer.ALE_AUTH_CONNECT_V4:
            raise PrivilegedHelperError("fixed helper allow layer cannot be changed")
        if self.allow_host != "127.0.0.1" or self.allow_protocol != "tcp":
            raise PrivilegedHelperError("fixed helper allow semantics cannot be changed")


@dataclass(frozen=True, slots=True)
class HelperResourceKeys:
    """Deterministic owner keys derived inside the helper, never supplied by Worker."""

    provider_key: str
    sublayer_key: str
    deny_v4_key: str
    deny_v6_key: str
    allow_v4_key: str
    profile_name: str

    @classmethod
    def derive(
        cls,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> "HelperResourceKeys":
        if containment != ContainmentIdentity.derive(identity):
            raise HelperProtocolError("helper resource identity is not exact-run bound")
        seed = json.dumps(
            {
                "identity": {
                    "project_id": identity.project_id,
                    "command_id": identity.command_id,
                    "run_id": identity.run_id,
                    "claim_generation": identity.claim_generation,
                },
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "policy_digest": policy.policy_digest,
                "broker_port": policy.broker.port,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        values = tuple(
            str(uuid.uuid5(_HELPER_NAMESPACE, f"{label}:{seed}"))
            for label in ("provider", "sublayer", "deny-v4", "deny-v6", "allow-v4")
        )
        return cls(*values, containment.profile_name)


class FixedHelperBackend(Protocol):
    """Closed backend surface implemented by the owner-installed helper."""

    def inspect_health(self, policy: EgressPolicyReference) -> bool:
        """Return helper readiness without creating run resources."""

    def prepare(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> HelperEnforcement:
        """Install deny first, then exact loopback allow, and verify ACTIVE."""

    def reconcile(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> HelperEnforcement:
        """Inspect only the exact derived resource set."""

    def release(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> HelperEnforcement:
        """Remove only exact resources, retaining deny on uncertainty."""


@dataclass(slots=True)
class _MemoryResource:
    identity: RunIdentity
    policy: EgressPolicyReference
    containment: ContainmentIdentity
    keys: HelperResourceKeys
    enforcement: HelperEnforcement = HelperEnforcement.ACTIVE


class MemoryFixedHelperBackend:
    """Deterministic reference backend for protocol/ownership tests.

    This backend is not the production privileged implementation.  It models
    the helper's exact ownership and fail-closed transitions without touching
    Windows policy or the host.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self._resources: dict[str, _MemoryResource] = {}
        self._lock = threading.RLock()

    def inspect_health(self, policy: EgressPolicyReference) -> bool:
        return self.available and policy.broker.host == "127.0.0.1"

    def prepare(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> HelperEnforcement:
        self._require_available()
        keys = HelperResourceKeys.derive(identity, policy, containment)
        with self._lock:
            existing = self._resources.get(containment.identity_digest)
            if existing is not None and (
                existing.identity != identity or existing.policy != policy
            ):
                raise PrivilegedHelperError("resource identity collision")
            self._resources[containment.identity_digest] = _MemoryResource(
                identity=identity,
                policy=policy,
                containment=containment,
                keys=keys,
            )
        return HelperEnforcement.ACTIVE

    def reconcile(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> HelperEnforcement:
        self._require_available()
        resource = self._exact(identity, policy, containment)
        return resource.enforcement

    def release(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> HelperEnforcement:
        self._require_available()
        with self._lock:
            resource = self._resources.get(containment.identity_digest)
            if resource is None:
                # Missing exact objects cannot justify reopening any broader
                # scope; the restrictive result is the safe receipt.
                return HelperEnforcement.DENY_RETAINED
            self._verify(resource, identity, policy, containment)
            del self._resources[containment.identity_digest]
            return HelperEnforcement.INACTIVE

    def resource_keys(
        self, identity: RunIdentity, policy: EgressPolicyReference, containment: ContainmentIdentity
    ) -> HelperResourceKeys:
        return self._exact(identity, policy, containment).keys

    def _exact(
        self,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> _MemoryResource:
        if containment != ContainmentIdentity.derive(identity):
            raise HelperProtocolError("resource containment is not exact")
        with self._lock:
            resource = self._resources.get(containment.identity_digest)
            if resource is None:
                raise PrivilegedHelperError("exact run resource is absent")
            self._verify(resource, identity, policy, containment)
            return resource

    @staticmethod
    def _verify(
        resource: _MemoryResource,
        identity: RunIdentity,
        policy: EgressPolicyReference,
        containment: ContainmentIdentity,
    ) -> None:
        if (
            resource.identity != identity
            or resource.policy != policy
            or resource.containment != containment
        ):
            raise HelperProtocolError("helper resource is owned by another exact run")

    def _require_available(self) -> None:
        if not self.available:
            raise PrivilegedHelperUnavailable("helper is not ready")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class FixedPurposeHelperService:
    """Dispatch only typed fixed operations for one owner-provisioned policy."""

    __slots__ = ("_policy", "_backend", "_request_lock", "_seen_request_ids")

    def __init__(self, policy: EgressPolicyReference, backend: FixedHelperBackend) -> None:
        if not isinstance(policy, EgressPolicyReference):
            raise TypeError("policy must be an EgressPolicyReference")
        self._policy = policy
        self._backend = backend
        self._request_lock = threading.RLock()
        self._seen_request_ids: set[str] = set()

    def handle(self, request: HelperRequest) -> HelperReceipt:
        if not isinstance(request, HelperRequest):
            raise HelperProtocolError("helper accepts only a typed HelperRequest")
        with self._request_lock:
            if request.request_id in self._seen_request_ids:
                return self._rejected(request, "request_replayed")
            if len(self._seen_request_ids) >= MAX_HELPER_REQUEST_HISTORY:
                return self._rejected(request, "request_history_full")
            # Consume the id before dispatch.  A backend failure must not make
            # retrying the same possibly-partially-applied request ambiguous.
            self._seen_request_ids.add(request.request_id)
        if request.policy != self._policy:
            return self._rejected(request, "policy_mismatch")
        try:
            if request.operation is HelperOperation.INSPECT_HEALTH:
                if not self._backend.inspect_health(request.policy):
                    return self._rejected(request, "helper_unavailable")
                return HelperReceipt.for_request(
                    request,
                    status=HelperReceiptStatus.HEALTHY,
                    enforcement=HelperEnforcement.READY,
                    observed_at=_now_iso(),
                )
            if request.operation is HelperOperation.PREPARE_RUN:
                enforcement = self._backend.prepare(
                    request.identity, request.policy, request.containment
                )
                status = HelperReceiptStatus.PREPARED
            elif request.operation is HelperOperation.RECONCILE_RUN:
                enforcement = self._backend.reconcile(
                    request.identity, request.policy, request.containment
                )
                status = HelperReceiptStatus.RECONCILED
            elif request.operation is HelperOperation.RELEASE_RUN:
                enforcement = self._backend.release(
                    request.identity, request.policy, request.containment
                )
                status = HelperReceiptStatus.RELEASED
            else:
                return self._rejected(request, "unsupported_operation")
            return HelperReceipt.for_request(
                request,
                status=status,
                enforcement=enforcement,
                observed_at=_now_iso(),
            )
        except PrivilegedHelperUnavailable:
            return self._rejected(request, "helper_unavailable")
        except (HelperProtocolError, PrivilegedHelperError):
            return self._rejected(request, "helper_rejected")
        except BaseException:
            # Native helper diagnostics never cross this protocol boundary.
            return self._rejected(request, "helper_backend_error")

    @staticmethod
    def _rejected(request: HelperRequest, error_code: str) -> HelperReceipt:
        return HelperReceipt.for_request(
            request,
            status=HelperReceiptStatus.REJECTED,
            enforcement=HelperEnforcement.UNKNOWN,
            observed_at=_now_iso(),
            error_code=error_code,
        )


class LocalHelperTransport:
    """Bounded loopback frame transport for the owner-installed helper."""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        if host != "127.0.0.1":
            raise PrivilegedHelperError("helper IPC must bind exactly to 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise PrivilegedHelperError("helper IPC port is outside the TCP range")
        if timeout <= 0:
            raise PrivilegedHelperError("helper IPC timeout must be positive")
        self.host = host
        self.port = port
        self.timeout = float(timeout)

    def exchange(self, request: HelperRequest) -> HelperReceipt:
        if not isinstance(request, HelperRequest):
            raise TypeError("request must be a HelperRequest")
        wire = request.encode().encode("ascii") + b"\n"
        if len(wire) > MAX_HELPER_FRAME_BYTES:
            raise PrivilegedHelperError("helper request frame is too large")
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as connection:
                connection.settimeout(self.timeout)
                connection.sendall(wire)
                data = bytearray()
                while len(data) < MAX_HELPER_FRAME_BYTES:
                    chunk = connection.recv(min(4096, MAX_HELPER_FRAME_BYTES - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if b"\n" in data:
                        break
        except OSError as exc:
            raise PrivilegedHelperUnavailable("helper IPC is unavailable") from exc
        if not data or b"\n" not in data:
            raise PrivilegedHelperError("helper receipt frame is incomplete")
        frame = bytes(data).split(b"\n", 1)[0]
        try:
            receipt = decode_receipt(frame.decode("ascii", "strict"))
            receipt.verify_for(request)
        except (UnicodeError, ValueError, HelperProtocolError) as exc:
            raise HelperProtocolError("helper receipt frame is invalid") from exc
        return receipt


class FixedHelperClient:
    """Typed Worker-side client; it cannot issue arbitrary helper actions."""

    __slots__ = ("_transport", "_request_lock", "_seen_request_ids")

    def __init__(self, transport: PrivilegedHelperTransport) -> None:
        if not callable(getattr(transport, "exchange", None)):
            raise TypeError("transport must implement typed exchange")
        self._transport = transport
        self._request_lock = threading.RLock()
        self._seen_request_ids: set[str] = set()

    def exchange(self, request: HelperRequest) -> HelperReceipt:
        if not isinstance(request, HelperRequest):
            raise TypeError("request must be a HelperRequest")
        with self._request_lock:
            if request.request_id in self._seen_request_ids:
                raise HelperProtocolError("helper request was replayed")
            if len(self._seen_request_ids) >= MAX_HELPER_REQUEST_HISTORY:
                raise HelperProtocolError("helper request history is full")
            # A transport failure consumes the request id so the caller cannot
            # turn an uncertain partial helper action into a blind retry.
            self._seen_request_ids.add(request.request_id)
        receipt = self._transport.exchange(request)
        if not isinstance(receipt, HelperReceipt):
            raise HelperProtocolError("helper transport returned a non-typed receipt")
        receipt.verify_for(request)
        return receipt

    def inspect_health(
        self, identity: RunIdentity, policy: EgressPolicyReference, request_id: str
    ) -> HelperReceipt:
        return self.exchange(
            HelperRequest.for_operation(
                HelperOperation.INSPECT_HEALTH, identity, policy, request_id
            )
        )


@dataclass(frozen=True, slots=True)
class HelperBootstrapPlan:
    """Idempotent fixed-task owner plan; rendering is the only Worker action."""

    helper_path: Path
    binary_sha256: str
    install_root: Path = Path(r"C:\ProgramData\AI-Agent-Bridge\Phase86")
    task_name: str = "AI-Agent-Bridge-Phase86-Egress"
    version: str = HELPER_BOOTSTRAP_VERSION

    def __post_init__(self) -> None:
        if self.task_name != "AI-Agent-Bridge-Phase86-Egress":
            raise PrivilegedHelperError("bootstrap task name is not fixed")
        if self.version != HELPER_BOOTSTRAP_VERSION:
            raise PrivilegedHelperError("unsupported helper bootstrap version")
        if not isinstance(self.helper_path, Path) or PureWindowsPath(str(self.helper_path)).name != HELPER_EXECUTABLE_NAME:
            raise PrivilegedHelperError("bootstrap executable name is not fixed")
        if not _is_absolute_windows_path(self.helper_path):
            raise PrivilegedHelperError("bootstrap helper_path must be absolute")
        digest = self.binary_sha256
        if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise PrivilegedHelperError("bootstrap binary_sha256 must be lowercase SHA-256")
        if not isinstance(self.install_root, Path) or not _is_absolute_windows_path(
            self.install_root
        ):
            raise PrivilegedHelperError("bootstrap install_root must be absolute")

    @property
    def manifest_digest(self) -> str:
        payload = f"{self.version}\n{self.task_name}\n{self.helper_path}\n{self.binary_sha256}\n"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def render_install_script(self) -> str:
        """Return reviewable fixed PowerShell; it accepts no Worker arguments."""

        path = _ps_quote(str(self.helper_path))
        root = _ps_quote(str(self.install_root))
        task = _ps_quote(self.task_name)
        digest = _ps_quote(self.binary_sha256)
        version = _ps_quote(self.version)
        installed = _ps_quote(str(self.install_root / HELPER_EXECUTABLE_NAME))
        return "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$root = {root}",
                f"$source = {path}",
                f"$expected = {digest}",
                f"$installed = {installed}",
                f"$version = {version}",
                "$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()",
                "if ($sourceHash -cne $expected) { throw 'Phase86 helper digest mismatch' }",
                "New-Item -ItemType Directory -Force -Path $root | Out-Null",
                "if ($source -ine $installed) { Copy-Item -LiteralPath $source -Destination $installed -Force }",
                "$installedHash = (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash.ToLowerInvariant()",
                "if ($installedHash -cne $expected) { throw 'Phase86 installed helper digest mismatch' }",
                "$action = New-ScheduledTaskAction -Execute $installed -Argument '--serve'",
                "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
                "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
                f"Register-ScheduledTask -TaskName {task} -Action $action -Principal $principal -Settings $settings -Description ('AI-Agent-Bridge Phase86 egress helper ' + $version) -Force | Out-Null",
                "",
            ]
        )

    def render_uninstall_script(self) -> str:
        """Return the fixed reversible owner cleanup script."""

        task = _ps_quote(self.task_name)
        root = _ps_quote(str(self.install_root))
        installed = _ps_quote(str(self.install_root / HELPER_EXECUTABLE_NAME))
        return "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"Unregister-ScheduledTask -TaskName {task} -Confirm:$false -ErrorAction SilentlyContinue",
                f"Remove-Item -LiteralPath {installed} -Force -ErrorAction SilentlyContinue",
                f"Remove-Item -LiteralPath {root} -Force -ErrorAction SilentlyContinue",
                "",
            ]
        )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _is_absolute_windows_path(path: Path) -> bool:
    """Recognize Windows absolute paths even when contracts are tested off-host."""

    value = str(path)
    return path.is_absolute() or (
        len(value) >= 3
        and value[1] == ":"
        and value[2] in {"/", "\\"}
    ) or value.startswith(("\\\\", "/\\"))


class FixedHelperServer:
    """Small server loop intended only for the owner-installed fixed helper."""

    def __init__(self, service: FixedPurposeHelperService, port: int) -> None:
        if not isinstance(service, FixedPurposeHelperService):
            raise TypeError("service must be a FixedPurposeHelperService")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise PrivilegedHelperError("helper server port is outside the TCP range")
        self._service = service
        self._port = port
        self._stop = threading.Event()
        self._socket: socket.socket | None = None

    def serve_forever(self) -> None:
        """Serve one typed request per loopback connection until stopped."""

        if os.name != "nt":
            raise PrivilegedHelperUnavailable("production fixed helper is Windows-only")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self._port))
            sock.listen(8)
            sock.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    connection, _address = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                self._serve_connection(connection)
        finally:
            sock.close()
            self._socket = None

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _serve_connection(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(5.0)
            data = bytearray()
            while len(data) < MAX_HELPER_FRAME_BYTES:
                chunk = connection.recv(min(4096, MAX_HELPER_FRAME_BYTES - len(data)))
                if not chunk:
                    return
                data.extend(chunk)
                if b"\n" in data:
                    break
            if b"\n" not in data:
                return
            try:
                from .egress import decode_request

                request = decode_request(bytes(data).split(b"\n", 1)[0].decode("ascii", "strict"))
                receipt = self._service.handle(request)
                connection.sendall(receipt.encode().encode("ascii") + b"\n")
            except (UnicodeError, ValueError, OSError):
                return


__all__ = [
    "FixedHelperBackend",
    "FixedHelperClient",
    "FixedHelperServer",
    "FixedWfpLayer",
    "FixedWfpPolicySpec",
    "HELPER_BOOTSTRAP_VERSION",
    "HELPER_EXECUTABLE_NAME",
    "HelperBootstrapPlan",
    "HelperResourceKeys",
    "LocalHelperTransport",
    "MAX_HELPER_REQUEST_HISTORY",
    "MemoryFixedHelperBackend",
    "PrivilegedHelperError",
    "PrivilegedHelperUnavailable",
]
