"""Run-owned loopback broker with a bounded, payload-free evidence ledger.

The broker is deliberately a choke point, not an open proxy.  It accepts only
health requests and HTTPS ``CONNECT`` requests whose exact hostname belongs to
an owner-declared logical destination class.  Every data connection is made to
the configured upstream proxy; an upstream failure returns an error and never
falls back to the target or to another route.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping

from .egress import BrokerEndpoint, EgressContractError, EgressPolicy
from .models import RunIdentity


MAX_BROKER_HEADER_BYTES = 16 * 1024
MAX_BROKER_LEDGER_BYTES = 64 * 1024
MAX_BROKER_THREADS = 32
MAX_RELAY_SECONDS = 300.0
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


class BrokerError(RuntimeError):
    """The run-owned broker cannot establish or retain its bounded boundary."""


class BrokerState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_host(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{label} must be non-empty text")
    text = value.strip().lower().rstrip(".")
    if text == "127.0.0.1":
        return text
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        raise EgressContractError(f"{label} must be 127.0.0.1 or an exact hostname")
    if _HOST_RE.fullmatch(text) is None:
        raise EgressContractError(f"{label} must be an exact hostname")
    return text


def _safe_port(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise EgressContractError(f"{label} must be a TCP port from 1 through 65535")
    return value


@dataclass(frozen=True, slots=True)
class UpstreamProxyEndpoint:
    """A configured proxy endpoint; credentials never belong in this object."""

    host: str
    port: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", _safe_host(self.host, "upstream.host"))
        object.__setattr__(self, "port", _safe_port(self.port, "upstream.port"))


@dataclass(frozen=True, slots=True)
class BrokerLedgerSnapshot:
    """Bounded secret-safe facts emitted by one exact run's broker."""

    identity: RunIdentity
    policy_id: str
    policy_digest: str
    enforcement_active: bool
    sequence: int
    health_allowed: int
    connect_allowed: int
    connect_blocked: int
    connections_observed: int
    upstream_failures: int
    logical_destination_counts: tuple[tuple[str, int], ...]
    first_observed_at: str | None
    last_observed_at: str | None
    last_failure_code: str | None

    def to_wire(self) -> dict[str, object]:
        """Return only stable identity, counters, classes, and time facts."""

        return {
            "schema_version": 1,
            "project_id": self.identity.project_id,
            "command_id": self.identity.command_id,
            "run_id": self.identity.run_id,
            "claim_generation": self.identity.claim_generation,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "enforcement_active": self.enforcement_active,
            "sequence": self.sequence,
            "health_allowed": self.health_allowed,
            "connect_allowed": self.connect_allowed,
            "connect_blocked": self.connect_blocked,
            "connections_observed": self.connections_observed,
            "upstream_failures": self.upstream_failures,
            "logical_destination_counts": [
                {"class": name, "count": count}
                for name, count in self.logical_destination_counts
            ],
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "last_failure_code": self.last_failure_code,
        }


class LocalEgressBroker:
    """A disposable IPv4 loopback CONNECT broker owned by one RunScope."""

    __slots__ = (
        "_identity",
        "_policy",
        "_upstream",
        "_destination_hosts",
        "_ledger_path",
        "_header_limit",
        "_connect_timeout",
        "_relay_seconds",
        "_socket",
        "_state",
        "_stop",
        "_accept_thread",
        "_threads",
        "_thread_slots",
        "_lock",
        "_sequence",
        "_health_allowed",
        "_connect_allowed",
        "_connect_blocked",
        "_connections_observed",
        "_upstream_failures",
        "_destination_counts",
        "_first_observed_at",
        "_last_observed_at",
        "_last_failure_code",
    )

    def __init__(
        self,
        identity: RunIdentity,
        policy: EgressPolicy,
        upstream: UpstreamProxyEndpoint,
        destination_hosts: Mapping[str, tuple[str, ...]],
        *,
        ledger_path: Path | None = None,
        header_limit: int = MAX_BROKER_HEADER_BYTES,
        connect_timeout: float = 5.0,
        relay_seconds: float = MAX_RELAY_SECONDS,
    ) -> None:
        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not isinstance(policy, EgressPolicy):
            raise TypeError("policy must be an EgressPolicy")
        if not isinstance(upstream, UpstreamProxyEndpoint):
            raise TypeError("upstream must be an UpstreamProxyEndpoint")
        if policy.broker.host != "127.0.0.1":
            raise BrokerError("broker must bind exactly to 127.0.0.1")
        normalized: dict[str, tuple[str, ...]] = {}
        for name, hosts in destination_hosts.items():
            if name not in policy.logical_destinations:
                raise BrokerError("destination host map contains an undeclared class")
            if not isinstance(hosts, tuple) or not hosts:
                raise BrokerError("destination host classes must be non-empty tuples")
            normalized[name] = tuple(_safe_host(host, "destination hostname") for host in hosts)
        if set(normalized) != set(policy.logical_destinations):
            raise BrokerError("destination host map must cover every policy class")
        if isinstance(header_limit, bool) or not 512 <= header_limit <= MAX_BROKER_HEADER_BYTES:
            raise BrokerError("header_limit is outside the bounded broker contract")
        if connect_timeout <= 0 or relay_seconds <= 0:
            raise BrokerError("broker timeouts must be positive")
        self._identity = identity
        self._policy = policy
        self._upstream = upstream
        self._destination_hosts = normalized
        self._ledger_path = ledger_path
        self._header_limit = header_limit
        self._connect_timeout = float(connect_timeout)
        self._relay_seconds = min(float(relay_seconds), MAX_RELAY_SECONDS)
        self._socket: socket.socket | None = None
        self._state = BrokerState.NEW
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._thread_slots = threading.BoundedSemaphore(MAX_BROKER_THREADS)
        self._lock = threading.RLock()
        self._sequence = 0
        self._health_allowed = 0
        self._connect_allowed = 0
        self._connect_blocked = 0
        self._connections_observed = 0
        self._upstream_failures = 0
        self._destination_counts: dict[str, int] = {}
        self._first_observed_at: str | None = None
        self._last_observed_at: str | None = None
        self._last_failure_code: str | None = None

    @property
    def identity(self) -> RunIdentity:
        return self._identity

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    @property
    def state(self) -> BrokerState:
        return self._state

    @property
    def endpoint(self) -> BrokerEndpoint:
        """Return the exact loopback endpoint without exposing a raw socket."""

        return self._policy.broker

    def start(self) -> BrokerEndpoint:
        """Bind the configured loopback endpoint and return its typed endpoint."""

        with self._lock:
            if self._state is BrokerState.ACTIVE:
                return self._policy.broker
            if self._state is not BrokerState.NEW:
                raise BrokerError(f"broker cannot start from {self._state.value}")
            self._verify_existing_ledger_locked()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", self._policy.broker.port))
                sock.listen(16)
                sock.settimeout(0.2)
            except BaseException:
                sock.close()
                self._state = BrokerState.UNKNOWN
                raise
            self._socket = sock
            self._state = BrokerState.ACTIVE
            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                name=f"bridge-egress-{self._identity.run_id}",
                daemon=True,
            )
            self._accept_thread.start()
            self._record("broker_started")
            self._write_ledger_locked()
            return self._policy.broker

    def health(self) -> bool:
        """Return whether this exact broker is active and accepting health."""

        with self._lock:
            return self._state is BrokerState.ACTIVE and self._socket is not None

    def ledger(self) -> BrokerLedgerSnapshot:
        """Return a bounded immutable ledger snapshot."""

        with self._lock:
            return BrokerLedgerSnapshot(
                identity=self._identity,
                policy_id=self._policy.policy_id,
                policy_digest=self._policy.digest,
                enforcement_active=self._state is BrokerState.ACTIVE,
                sequence=self._sequence,
                health_allowed=self._health_allowed,
                connect_allowed=self._connect_allowed,
                connect_blocked=self._connect_blocked,
                connections_observed=self._connections_observed,
                upstream_failures=self._upstream_failures,
                logical_destination_counts=tuple(sorted(self._destination_counts.items())),
                first_observed_at=self._first_observed_at,
                last_observed_at=self._last_observed_at,
                last_failure_code=self._last_failure_code,
            )

    def dispose(self) -> None:
        """Stop only this broker's socket and handler threads."""

        with self._lock:
            if self._state is BrokerState.CLOSED:
                return
            self._state = BrokerState.STOPPING
            self._stop.set()
            sock = self._socket
            self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._accept_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        for handler in tuple(self._threads):
            if handler is not threading.current_thread():
                handler.join(timeout=1.0)
        with self._lock:
            self._state = BrokerState.CLOSED
            self._record("broker_stopped")
            self._write_ledger_locked()

    close = dispose

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                sock = self._socket
            if sock is None:
                return
            try:
                connection, _address = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._thread_slots.acquire(blocking=False):
                self._send_status(connection, b"503 Service Unavailable")
                connection.close()
                self._record("blocked", failure_code="handler_capacity")
                continue
            handler = threading.Thread(
                target=self._handle_connection,
                args=(connection,),
                name=f"bridge-egress-conn-{self._identity.run_id}",
                daemon=True,
            )
            with self._lock:
                self._threads.append(handler)
                self._connections_observed += 1
            handler.start()

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(self._connect_timeout)
                request = self._read_headers(connection)
                if not request:
                    self._record("blocked", failure_code="empty_request")
                    return
                first_line = request.split(b"\r\n", 1)[0]
                try:
                    line = first_line.decode("ascii", "strict")
                except UnicodeDecodeError:
                    self._send_status(connection, b"400 Bad Request")
                    self._record("blocked", failure_code="invalid_request")
                    return
                if line == "GET /health HTTP/1.1" or line == "GET /health HTTP/1.0":
                    self._send_health(connection)
                    self._record("health")
                    return
                if not line.startswith("CONNECT "):
                    self._send_status(connection, b"403 Forbidden")
                    self._record("blocked", failure_code="method_not_allowed")
                    return
                target = self._parse_connect_target(line)
                if target is None:
                    self._send_status(connection, b"403 Forbidden")
                    self._record("blocked", failure_code="target_not_allowed")
                    return
                host, logical_class = target
                self._proxy_connect(connection, host, logical_class)
        except (OSError, ValueError):
            self._record("blocked", failure_code="connection_error")
        finally:
            self._thread_slots.release()

    def _read_headers(self, connection: socket.socket) -> bytes | None:
        data = bytearray()
        while len(data) < self._header_limit:
            chunk = connection.recv(min(2048, self._header_limit - len(data)))
            if not chunk:
                return None
            data.extend(chunk)
            if b"\r\n\r\n" in data:
                return bytes(data)
        return None

    def _parse_connect_target(self, line: str) -> tuple[str, str] | None:
        parts = line.split(" ")
        if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] not in {
            "HTTP/1.0",
            "HTTP/1.1",
        }:
            return None
        target = parts[1]
        if target.count(":") != 1:
            return None
        host, port_text = target.rsplit(":", 1)
        try:
            port = int(port_text)
        except ValueError:
            return None
        if port != 443:
            return None
        try:
            normalized = _safe_host(host, "CONNECT target")
        except EgressContractError:
            return None
        for logical_class, hosts in self._destination_hosts.items():
            if normalized in hosts:
                return normalized, logical_class
        return None

    def _proxy_connect(
        self, connection: socket.socket, host: str, logical_class: str
    ) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection(
                (self._upstream.host, self._upstream.port),
                timeout=self._connect_timeout,
            )
            upstream.settimeout(self._connect_timeout)
            request = (
                f"CONNECT {host}:443 HTTP/1.1\r\n"
                f"Host: {host}:443\r\n"
                "Proxy-Connection: Keep-Alive\r\n\r\n"
            ).encode("ascii")
            upstream.sendall(request)
            response = self._read_headers(upstream)
            if not response or not self._is_upstream_success(response):
                self._send_status(connection, b"502 Bad Gateway")
                self._record("upstream_failure", failure_code="upstream_rejected")
                return
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._record("allowed", logical_class=logical_class)
            self._relay(connection, upstream)
        except OSError:
            self._send_status(connection, b"502 Bad Gateway")
            self._record("upstream_failure", failure_code="upstream_unavailable")
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    @staticmethod
    def _is_upstream_success(response: bytes) -> bool:
        first_line = response.split(b"\r\n", 1)[0]
        return first_line.startswith(b"HTTP/1.0 200 ") or first_line.startswith(
            b"HTTP/1.1 200 "
        )

    def _relay(self, left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        left.setblocking(False)
        right.setblocking(False)
        deadline = time.monotonic() + self._relay_seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                readable, _writable, exceptional = select.select(sockets, [], sockets, 0.5)
            except (OSError, ValueError):
                return
            if exceptional:
                return
            for source in readable:
                target = right if source is left else left
                try:
                    data = source.recv(16 * 1024)
                except (BlockingIOError, OSError):
                    return
                if not data:
                    return
                try:
                    target.sendall(data)
                except OSError:
                    return

    @staticmethod
    def _send_status(connection: socket.socket, status: bytes) -> None:
        try:
            connection.sendall(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        except OSError:
            return

    @staticmethod
    def _send_health(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            b"Connection: close\r\n\r\nok"
        )

    def _record(
        self,
        event: str,
        *,
        logical_class: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            observed_at = _now_iso()
            self._first_observed_at = self._first_observed_at or observed_at
            self._last_observed_at = observed_at
            if event == "health":
                self._health_allowed += 1
            elif event == "allowed":
                self._connect_allowed += 1
                if logical_class is not None:
                    self._destination_counts[logical_class] = (
                        self._destination_counts.get(logical_class, 0) + 1
                    )
            elif event == "blocked":
                self._connect_blocked += 1
            elif event == "upstream_failure":
                self._upstream_failures += 1
            if failure_code is not None:
                self._last_failure_code = failure_code
            self._write_ledger_locked()

    def _write_ledger_locked(self) -> None:
        if self._ledger_path is None:
            return
        path = self._ledger_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._verify_existing_ledger_locked()
        snapshot = self.ledger().to_wire()
        rendered = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="ascii", newline="") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _verify_existing_ledger_locked(self) -> None:
        """Never replace a ledger already owned by another exact run."""

        if self._ledger_path is None or not self._ledger_path.exists():
            return
        try:
            raw = self._ledger_path.read_bytes()
        except OSError as exc:
            raise BrokerError("existing broker ledger cannot be read") from exc
        if not raw or len(raw) > MAX_BROKER_LEDGER_BYTES:
            raise BrokerError("existing broker ledger is outside the bounded contract")
        try:
            value = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError("existing broker ledger is malformed") from exc
        if not isinstance(value, dict):
            raise BrokerError("existing broker ledger is not an object")
        expected = {
            "project_id": self._identity.project_id,
            "command_id": self._identity.command_id,
            "run_id": self._identity.run_id,
            "claim_generation": self._identity.claim_generation,
            "policy_id": self._policy.policy_id,
            "policy_digest": self._policy.digest,
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            raise BrokerError("existing broker ledger belongs to another exact run")


__all__ = [
    "BrokerError",
    "BrokerLedgerSnapshot",
    "BrokerState",
    "LocalEgressBroker",
    "MAX_BROKER_HEADER_BYTES",
    "MAX_BROKER_LEDGER_BYTES",
    "MAX_BROKER_THREADS",
    "UpstreamProxyEndpoint",
]
