"""Deterministic Phase-8.6-C acceptance and cross-run ownership tests."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from vnext_runtime.egress import (
    BrokerEndpoint,
    ContainmentIdentity,
    EgressPolicy,
    HelperEnforcement,
    HelperOperation,
    HelperReceiptStatus,
    HelperRequest,
    decode_request,
)
from vnext_runtime.egress_broker import (
    BrokerError,
    LocalEgressBroker,
    MAX_BROKER_LEDGER_BYTES,
    UpstreamProxyEndpoint,
)
from vnext_runtime.egress_config import FIXED_HELPER_TASK_NAME, EgressActivationConfig
from vnext_runtime.egress_runtime import RunEgressResources, preclaim_egress
from vnext_runtime.models import RunIdentity
from vnext_runtime.privileged_helper import (
    FixedHelperClient,
    FixedPurposeHelperService,
    FixedWfpLayer,
    FixedWfpPolicySpec,
    HelperBootstrapPlan,
    HelperResourceKeys,
    MemoryFixedHelperBackend,
)
from vnext_runtime.recovery_wal import DurableRunWal, DurableWalError
from vnext_runtime.recovery_evidence import WalEventKind
from vnext_runtime.run_scope import RunScope, RunScopeError


IDENTITY_A = RunIdentity("engine-maintenance", 36, "run-036-c-a", 101)
IDENTITY_B = RunIdentity("engine-maintenance", 36, "run-036-c-b", 102)
IDENTITY_LATER = RunIdentity("engine-maintenance", 36, "run-036-c-later", 103)


def _free_ports(count: int) -> tuple[int, ...]:
    listeners: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return tuple(int(listener.getsockname()[1]) for listener in listeners)
    finally:
        for listener in listeners:
            listener.close()


def _activation_config(
    *, broker_port: int, upstream_port: int, helper_port: int
) -> dict[str, object]:
    return {
        "egress_containment": {
            "enabled": True,
            "mode": "required",
            "policy_id": "phase86-c-acceptance",
            "policy_version": 1,
            "broker_port": broker_port,
            "approved_upstream": "safe-overseas-proxy-v1",
            "logical_destinations": ["codex-provider", "github-read-only"],
            "destination_hosts": {
                "codex-provider": ["api.openai.com"],
                "github-read-only": ["github.com"],
            },
            "upstream": {"host": "127.0.0.1", "port": upstream_port},
            "helper": {
                "ipc_host": "127.0.0.1",
                "ipc_port": helper_port,
                "task_name": FIXED_HELPER_TASK_NAME,
            },
        }
    }


class _RecordingTransport:
    def __init__(self, service: FixedPurposeHelperService) -> None:
        self.service = service
        self.requests: list[HelperRequest] = []

    def exchange(self, request: HelperRequest):
        self.requests.append(request)
        return self.service.handle(request)


class _ContainedLauncher:
    is_contained = True

    def __init__(self) -> None:
        self.bound_binding = None

    def bind_containment(self, binding) -> None:
        self.bound_binding = binding

    def launch(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("acceptance test must not create an executor")


class _FailingDisposable:
    def __init__(self, identity: RunIdentity) -> None:
        self.identity = identity
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True
        raise RuntimeError("synthetic cleanup failure")


class HelperProtocolAcceptanceTests(unittest.TestCase):
    def test_malformed_replayed_and_foreign_requests_fail_closed(self) -> None:
        broker_port, upstream_port, helper_port = _free_ports(3)
        activation = EgressActivationConfig.from_worker_config(
            _activation_config(
                broker_port=broker_port,
                upstream_port=upstream_port,
                helper_port=helper_port,
            )
        )
        backend = MemoryFixedHelperBackend()
        service = FixedPurposeHelperService(activation.policy().reference(), backend)
        request = HelperRequest.for_operation(
            HelperOperation.PREPARE_RUN,
            IDENTITY_A,
            activation.policy().reference(),
            "request-c-prepare",
        )

        first = service.handle(request)
        replay = service.handle(request)
        self.assertEqual(first.status, HelperReceiptStatus.PREPARED)
        self.assertEqual(replay.status, HelperReceiptStatus.REJECTED)
        self.assertEqual(replay.error_code, "request_replayed")

        foreign = HelperRequest.for_operation(
            HelperOperation.RECONCILE_RUN,
            IDENTITY_B,
            activation.policy().reference(),
            "request-c-foreign",
        )
        foreign_receipt = service.handle(foreign)
        self.assertEqual(foreign_receipt.status, HelperReceiptStatus.REJECTED)
        self.assertEqual(foreign_receipt.error_code, "helper_rejected")

        with self.assertRaises(ValueError):
            decode_request("{not-json}")

        malformed = request.to_wire()
        malformed["delete_target"] = "all-filters"
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            HelperRequest.from_wire(malformed)

    def test_client_consumes_request_id_once_and_rejects_foreign_receipt(self) -> None:
        broker_port, upstream_port, helper_port = _free_ports(3)
        activation = EgressActivationConfig.from_worker_config(
            _activation_config(
                broker_port=broker_port,
                upstream_port=upstream_port,
                helper_port=helper_port,
            )
        )
        service = FixedPurposeHelperService(
            activation.policy().reference(), MemoryFixedHelperBackend()
        )
        transport = _RecordingTransport(service)
        client = FixedHelperClient(transport)
        request = HelperRequest.for_operation(
            HelperOperation.INSPECT_HEALTH,
            IDENTITY_A,
            activation.policy().reference(),
            "request-c-health",
        )

        self.assertEqual(
            client.exchange(request).status, HelperReceiptStatus.HEALTHY
        )
        with self.assertRaisesRegex(ValueError, "replayed"):
            client.exchange(request)

        class _ForeignTransport:
            def exchange(self, received: HelperRequest):
                foreign = HelperRequest.for_operation(
                    received.operation,
                    IDENTITY_B,
                    received.policy,
                    "foreign-receipt",
                )
                return service.handle(foreign)

        foreign_client = FixedHelperClient(_ForeignTransport())
        foreign_request = HelperRequest.for_operation(
            HelperOperation.INSPECT_HEALTH,
            IDENTITY_A,
            activation.policy().reference(),
            "request-c-foreign-receipt",
        )
        with self.assertRaises(ValueError):
            foreign_client.exchange(foreign_request)


class BrokerLedgerAcceptanceTests(unittest.TestCase):
    def _request(self, port: int, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall(request)
            chunks: list[bytes] = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def test_open_proxy_block_and_ledger_are_bounded_and_secret_safe(self) -> None:
        broker_port, upstream_port = _free_ports(2)
        policy = EgressPolicy(
            policy_id="phase86-c-ledger",
            policy_version=1,
            broker=BrokerEndpoint("127.0.0.1", broker_port),
            approved_upstream="safe-overseas-proxy-v1",
            logical_destinations=("github-read-only",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "egress-ledger.json"
            broker = LocalEgressBroker(
                IDENTITY_A,
                policy,
                UpstreamProxyEndpoint("127.0.0.1", upstream_port),
                {"github-read-only": ("github.com",)},
                ledger_path=ledger,
            )
            broker.start()
            try:
                health = self._request(
                    broker_port,
                    b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                )
                blocked = self._request(
                    broker_port,
                    b"CONNECT 8.8.8.8:443 HTTP/1.1\r\nHost: 8.8.8.8:443\r\n\r\n",
                )
                self.assertIn(b"200 OK", health)
                self.assertIn(b"403 Forbidden", blocked)
            finally:
                broker.dispose()

            raw = ledger.read_bytes()
            self.assertLessEqual(len(raw), MAX_BROKER_LEDGER_BYTES)
            self.assertNotIn(b"github.com", raw)
            self.assertNotIn(b"authorization", raw.lower())
            self.assertNotIn(b"cookie", raw.lower())
            self.assertNotIn(b"token", raw.lower())
            self.assertEqual(json.loads(raw)["run_id"], IDENTITY_A.run_id)


class CrossRunOwnershipAcceptanceTests(unittest.TestCase):
    def test_two_active_scopes_have_disjoint_resources_and_safe_cleanup(self) -> None:
        ports = _free_ports(6)
        config_a = _activation_config(
            broker_port=ports[0], upstream_port=ports[1], helper_port=ports[2]
        )
        config_b = _activation_config(
            broker_port=ports[3], upstream_port=ports[4], helper_port=ports[5]
        )
        activation_a = EgressActivationConfig.from_worker_config(config_a)
        activation_b = EgressActivationConfig.from_worker_config(config_b)
        backend_a = MemoryFixedHelperBackend()
        backend_b = MemoryFixedHelperBackend()
        transport_a = _RecordingTransport(
            FixedPurposeHelperService(activation_a.policy().reference(), backend_a)
        )
        transport_b = _RecordingTransport(
            FixedPurposeHelperService(activation_b.policy().reference(), backend_b)
        )
        client_a = FixedHelperClient(transport_a)
        client_b = FixedHelperClient(transport_b)
        launcher_a = _ContainedLauncher()
        launcher_b = _ContainedLauncher()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir_a = root / IDENTITY_A.run_id
            run_dir_b = root / IDENTITY_B.run_id
            resources_a = RunEgressResources(
                IDENTITY_A,
                activation_a,
                helper=client_a,
                process_launcher=launcher_a,
                run_dir=run_dir_a,
            )
            resources_b = RunEgressResources(
                IDENTITY_B,
                activation_b,
                helper=client_b,
                process_launcher=launcher_b,
                run_dir=run_dir_b,
            )
            scope_a = RunScope(IDENTITY_A, run_dir=run_dir_a)
            scope_b = RunScope(IDENTITY_B, run_dir=run_dir_b)
            scope_a.activate()
            scope_b.activate()
            scope_a.own_containment(resources_a, process_launcher=launcher_a)
            scope_b.own_containment(resources_b, process_launcher=launcher_b)
            self.assertTrue(
                preclaim_egress(
                    config_a,
                    IDENTITY_A,
                    helper=client_a,
                    process_launcher=launcher_a,
                ).ready
            )
            self.assertTrue(
                preclaim_egress(
                    config_b,
                    IDENTITY_B,
                    helper=client_b,
                    process_launcher=launcher_b,
                ).ready
            )

            try:
                resources_a.prepare()
                resources_b.prepare()
                self.assertEqual(resources_a.broker.endpoint.port, ports[0])
                self.assertEqual(resources_b.broker.endpoint.port, ports[3])
                self.assertTrue(resources_a.process_boundary_ready)
                self.assertTrue(resources_b.process_boundary_ready)
                self.assertEqual(
                    launcher_a.bound_binding.identity, IDENTITY_A
                )
                self.assertEqual(
                    launcher_b.bound_binding.identity, IDENTITY_B
                )
                self.assertNotEqual(
                    ContainmentIdentity.derive(IDENTITY_A),
                    ContainmentIdentity.derive(IDENTITY_B),
                )
                keys_a = HelperResourceKeys.derive(
                    IDENTITY_A,
                    activation_a.policy().reference(),
                    ContainmentIdentity.derive(IDENTITY_A),
                )
                keys_b = HelperResourceKeys.derive(
                    IDENTITY_B,
                    activation_b.policy().reference(),
                    ContainmentIdentity.derive(IDENTITY_B),
                )
                self.assertTrue(
                    all(
                        getattr(keys_a, field.name) != getattr(keys_b, field.name)
                        for field in fields(keys_a)
                    )
                )
                request_ids = [
                    request.request_id
                    for request in (*transport_a.requests, *transport_b.requests)
                ]
                self.assertEqual(len(request_ids), len(set(request_ids)))

                wal_a_path = run_dir_a / "recovery.wal"
                wal_b_path = run_dir_b / "recovery.wal"
                wal_a = DurableRunWal(wal_a_path, IDENTITY_A)
                wal_b = DurableRunWal(wal_b_path, IDENTITY_B)
                wal_a.append(WalEventKind.RUN_CLAIMED)
                wal_b.append(WalEventKind.RUN_CLAIMED)
                wal_b_before = wal_b_path.read_bytes()
                with self.assertRaises(DurableWalError):
                    DurableRunWal.open(wal_b_path, IDENTITY_A)
                self.assertEqual(wal_b_path.read_bytes(), wal_b_before)

                ledger_b_path = run_dir_b / "egress-ledger.json"
                ledger_b_before = ledger_b_path.read_bytes()
                foreign_broker = LocalEgressBroker(
                    IDENTITY_A,
                    activation_a.policy(),
                    UpstreamProxyEndpoint("127.0.0.1", ports[1]),
                    activation_a.destination_host_map(),
                    ledger_path=ledger_b_path,
                )
                with self.assertRaises(BrokerError):
                    foreign_broker.start()
                self.assertEqual(ledger_b_path.read_bytes(), ledger_b_before)

                foreign_reconcile = HelperRequest.for_operation(
                    HelperOperation.RECONCILE_RUN,
                    IDENTITY_B,
                    activation_a.policy().reference(),
                    "request-c-foreign-reconcile",
                )
                self.assertEqual(
                    transport_a.service.handle(foreign_reconcile).status,
                    HelperReceiptStatus.REJECTED,
                )
                stale_release = HelperRequest.for_operation(
                    HelperOperation.RELEASE_RUN,
                    IDENTITY_LATER,
                    activation_a.policy().reference(),
                    "request-c-stale-release",
                )
                stale_receipt = transport_a.service.handle(stale_release)
                self.assertEqual(stale_receipt.status, HelperReceiptStatus.RELEASED)
                self.assertEqual(
                    stale_receipt.enforcement, HelperEnforcement.DENY_RETAINED
                )
                self.assertEqual(
                    backend_a.reconcile(
                        IDENTITY_A,
                        activation_a.policy().reference(),
                        ContainmentIdentity.derive(IDENTITY_A),
                    ),
                    HelperEnforcement.ACTIVE,
                )

                failing = _FailingDisposable(IDENTITY_A)
                scope_a.own(failing)
                errors = scope_a.close()
                self.assertEqual(len(errors), 1)
                self.assertTrue(failing.disposed)
                self.assertTrue(resources_b.broker.health())
                self.assertEqual(resources_b.state.value, "PREPARED")
                self.assertEqual(
                    backend_b.reconcile(
                        IDENTITY_B,
                        activation_b.policy().reference(),
                        ContainmentIdentity.derive(IDENTITY_B),
                    ),
                    HelperEnforcement.ACTIVE,
                )
            finally:
                scope_a.close()
                scope_b.close()

    def test_scope_rejects_foreign_containment_identity(self) -> None:
        class ForeignContainment:
            identity = IDENTITY_B
            process_launcher = _ContainedLauncher()

            def dispose(self) -> None:
                return

        scope = RunScope(IDENTITY_A)
        scope.activate()
        with self.assertRaises(RunScopeError):
            scope.own_containment(
                ForeignContainment(),
                process_launcher=ForeignContainment.process_launcher,
            )
        scope.close()


class BoundaryAcceptanceTests(unittest.TestCase):
    def test_fixed_semantics_and_bootstrap_have_no_generic_admin_surface(self) -> None:
        spec = FixedWfpPolicySpec()
        self.assertEqual(
            spec.deny_layers,
            (
                FixedWfpLayer.ALE_AUTH_CONNECT_V4,
                FixedWfpLayer.ALE_AUTH_CONNECT_V6,
            ),
        )
        plan = HelperBootstrapPlan(
            helper_path=Path(
                r"C:\ProgramData\AI-Agent-Bridge\Phase86\AI-Agent-Bridge.Phase86.Helper.exe"
            ),
            binary_sha256="a" * 64,
        )
        install = plan.render_install_script().lower()
        uninstall = plan.render_uninstall_script().lower()
        self.assertIn("register-scheduledtask", install)
        self.assertIn("get-filehash", install)
        self.assertNotIn("netsh", install)
        self.assertNotIn("invoke-expression", install)
        self.assertIn("unregister-scheduledtask", uninstall)
        self.assertIn("remove-item", uninstall)


if __name__ == "__main__":
    unittest.main()
