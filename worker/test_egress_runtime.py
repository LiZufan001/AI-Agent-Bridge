"""Portable runtime, broker, helper, process and WAL regression tests."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from codex_lifecycle import run_codex_process
from vnext_runtime.egress import (
    BrokerEndpoint,
    ContainmentIdentity,
    EgressPolicy,
)
from vnext_runtime.egress_broker import (
    BrokerState,
    LocalEgressBroker,
    UpstreamProxyEndpoint,
)
from vnext_runtime.egress_config import (
    EgressActivationConfig,
    EgressActivationMode,
    EgressConfigurationError,
    FIXED_HELPER_TASK_NAME,
)
from vnext_runtime.egress_runtime import (
    EgressRuntimeState,
    RunEgressResources,
    preclaim_egress,
)
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
from vnext_runtime.process_boundary import SubprocessProcessLauncher


from vnext_runtime.recovery_evidence import WalEventKind
from vnext_runtime.recovery_wal import DurableRunWal, DurableWalError
from vnext_runtime.run_scope import RunScope


IDENTITY = RunIdentity("engine-maintenance", 36, "run-036-runtime-test", 101)
OTHER_IDENTITY = RunIdentity("engine-maintenance", 37, "run-037-runtime-test", 102)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _activation_config(broker_port: int | None = None) -> dict[str, object]:
    return {
        "egress_containment": {
            "enabled": True,
            "mode": "required",
            "policy_id": "phase86-c-runtime-test",
            "policy_version": 1,
            "broker_port": broker_port or _free_port(),
            "approved_upstream": "safe-overseas-proxy-v1",
            "logical_destinations": ["codex-provider", "github-read-only"],
            "destination_hosts": {
                "codex-provider": ["api.openai.com"],
                "github-read-only": ["github.com"],
            },
            "upstream": {"host": "127.0.0.1", "port": _free_port()},
            "helper": {
                "ipc_host": "127.0.0.1",
                "ipc_port": _free_port(),
                "task_name": FIXED_HELPER_TASK_NAME,
            },
        }
    }


class _ServiceTransport:
    def __init__(self, service: FixedPurposeHelperService) -> None:
        self.service = service

    def exchange(self, request):
        return self.service.handle(request)


class _NoopLauncher:
    is_contained = True
    bound_binding = None

    def bind_containment(self, binding):
        self.bound_binding = binding

    def launch(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("the test launcher must not create a process")


class EgressConfigurationTests(unittest.TestCase):
    def test_absent_gate_is_disabled_and_required_mode_is_explicit(self) -> None:
        disabled = EgressActivationConfig.from_worker_config({})
        self.assertIs(disabled.mode, EgressActivationMode.DISABLED)
        self.assertFalse(disabled.required)

        config = _activation_config()
        required = EgressActivationConfig.from_worker_config(config)
        self.assertIs(required.mode, EgressActivationMode.REQUIRED)
        self.assertTrue(required.required)
        self.assertEqual(required.policy().broker.host, "127.0.0.1")

    def test_gate_rejects_address_inputs_and_unknown_helper_controls(self) -> None:
        config = _activation_config()
        config["egress_containment"]["destination_hosts"]["github-read-only"] = [
            "8.8.8.8"
        ]  # type: ignore[index]
        with self.assertRaises(EgressConfigurationError):
            EgressActivationConfig.from_worker_config(config)

        config = _activation_config()
        config["egress_containment"]["helper"]["command"] = "netsh"  # type: ignore[index]
        with self.assertRaisesRegex(EgressConfigurationError, "unsupported fields"):
            EgressActivationConfig.from_worker_config(config)

    def test_preclaim_requires_a_contained_launcher(self) -> None:
        activation = EgressActivationConfig.from_worker_config(_activation_config())
        backend = MemoryFixedHelperBackend()
        service = FixedPurposeHelperService(activation.policy().reference(), backend)
        client = FixedHelperClient(_ServiceTransport(service))
        with self.assertRaisesRegex(RuntimeError, "contained process launcher"):
            preclaim_egress(
                _activation_config(),
                IDENTITY,
                helper=client,
            )


class FixedHelperTests(unittest.TestCase):
    def test_fixed_backend_owns_and_releases_only_exact_run(self) -> None:
        activation = EgressActivationConfig.from_worker_config(_activation_config())
        policy = activation.policy()
        backend = MemoryFixedHelperBackend()
        service = FixedPurposeHelperService(policy.reference(), backend)
        client = FixedHelperClient(_ServiceTransport(service))
        preflight = preclaim_egress(
            _activation_config(broker_port=activation.broker_port),
            IDENTITY,
            helper=client,
            process_launcher=_NoopLauncher(),
        )
        self.assertTrue(preflight.ready)

        self.assertEqual(
            FixedWfpPolicySpec().deny_layers,
            (
                FixedWfpLayer.ALE_AUTH_CONNECT_V4,
                FixedWfpLayer.ALE_AUTH_CONNECT_V6,
            ),
        )
        plan = HelperBootstrapPlan(
            helper_path=Path(r"C:\ProgramData\AI-Agent-Bridge\Phase86\AI-Agent-Bridge.Phase86.Helper.exe"),
            binary_sha256="a" * 64,
        )
        self.assertIn("Register-ScheduledTask", plan.render_install_script())
        self.assertNotIn("netsh", plan.render_install_script().lower())

        events: list[str] = []
        resources = RunEgressResources(
            IDENTITY,
            activation,
            helper=client,
            process_launcher=_NoopLauncher(),
            event_logger=lambda event, _fields: events.append(event),
        )
        scope = RunScope(IDENTITY)
        scope.activate()
        scope.own_containment(resources, process_launcher=resources.process_launcher)
        resources.prepare()
        self.assertIs(resources.state, EgressRuntimeState.PREPARED)
        scope.close()
        self.assertIs(resources.state, EgressRuntimeState.CLOSED)
        self.assertEqual(
            events,
            [
                "broker_started",
                "containment_prepared",
                "broker_stopped",
                "containment_reconciled",
                "containment_released",
            ],
        )

    def test_resource_keys_are_disjoint_for_adjacent_runs(self) -> None:
        activation = EgressActivationConfig.from_worker_config(_activation_config())
        policy = activation.policy().reference()
        key_sets = {
            HelperResourceKeys.derive(
                identity,
                policy,
                ContainmentIdentity.derive(identity),
            )
            for identity in (IDENTITY, OTHER_IDENTITY)
        }
        self.assertEqual(len(key_sets), 2)


class BrokerTests(unittest.TestCase):
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

    def test_broker_is_loopback_only_and_has_no_direct_fallback(self) -> None:
        policy = EgressPolicy(
            policy_id="phase86-c-broker-test",
            policy_version=1,
            broker=BrokerEndpoint("127.0.0.1", _free_port()),
            approved_upstream="safe-overseas-proxy-v1",
            logical_destinations=("github-read-only",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            broker = LocalEgressBroker(
                IDENTITY,
                policy,
                UpstreamProxyEndpoint("127.0.0.1", _free_port()),
                {"github-read-only": ("github.com",)},
                ledger_path=Path(temporary) / "ledger.json",
                connect_timeout=0.2,
            )
            broker.start()
            try:
                health = self._request(
                    policy.broker.port,
                    b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                )
                self.assertIn(b"200 OK", health)
                blocked = self._request(
                    policy.broker.port,
                    b"CONNECT 8.8.8.8:443 HTTP/1.1\r\nHost: 8.8.8.8\r\n\r\n",
                )
                self.assertIn(b"403 Forbidden", blocked)
                failed_upstream = self._request(
                    policy.broker.port,
                    b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com\r\n\r\n",
                )
                self.assertIn(b"502 Bad Gateway", failed_upstream)
                time.sleep(0.05)
                ledger = broker.ledger()
                self.assertGreaterEqual(ledger.health_allowed, 1)
                self.assertGreaterEqual(ledger.upstream_failures, 1)
                self.assertEqual(ledger.connect_allowed, 0)
                self.assertNotIn(b"github.com", json.dumps(ledger.to_wire()).encode())
            finally:
                broker.dispose()
            self.assertIs(broker.state, BrokerState.CLOSED)


class DurableWalTests(unittest.TestCase):
    def test_wal_is_exact_run_bound_integrity_checked_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recovery.wal"
            writer = DurableRunWal(path, IDENTITY)
            writer.append(WalEventKind.RUN_CLAIMED)
            writer.record_lifecycle(
                "process_create_intent", {"command_digest": "a" * 64}
            )
            writer.record_lifecycle(
                "process_created", {"wrapper_pid": 11, "codex_root_pid": 12}
            )
            writer.record_lifecycle("process_exit", {"exit_code": 0})
            report_digest = "b" * 64
            writer.append(
                WalEventKind.REPORT_MATERIALIZED,
                artifact_digest=report_digest,
            )
            writer.append(
                WalEventKind.REPORT_PUBLISH_INTENT,
                artifact_digest=report_digest,
            )
            writer.append(WalEventKind.REPORT_PUBLISHED, artifact_digest=report_digest)

            snapshot = writer.snapshot
            self.assertEqual(snapshot.identity, IDENTITY)
            self.assertEqual(
                snapshot.events[0].kind,
                WalEventKind.RUN_CLAIMED,
            )
            self.assertEqual(snapshot.events[-1].kind, WalEventKind.REPORT_PUBLISHED)
            reopened = DurableRunWal.open(path, IDENTITY)
            self.assertEqual(reopened.events, snapshot.events)

            tampered = Path(temporary) / "tampered.wal"
            records = path.read_text(encoding="utf-8").splitlines()
            event_record = json.loads(records[1])
            event_record["identity"]["run_id"] = "run-other"
            records[1] = json.dumps(event_record, sort_keys=True, separators=(",", ":"))
            tampered.write_text("\n".join(records) + "\n", encoding="utf-8")
            with self.assertRaises(DurableWalError):
                DurableRunWal.read(tampered)

            partial = Path(temporary) / "partial.wal"
            partial.write_bytes(path.read_bytes().rstrip(b"\n"))
            with self.assertRaises(DurableWalError):
                DurableRunWal.read(partial)
            with self.assertRaises(DurableWalError):
                DurableRunWal.read(path, identity=OTHER_IDENTITY)

    def test_wal_rejects_duplicate_lifecycle_and_secret_like_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = DurableRunWal(Path(temporary) / "duplicate.wal", IDENTITY)
            duplicate.append(WalEventKind.RUN_CLAIMED)
            with self.assertRaises(DurableWalError):
                duplicate.append(WalEventKind.RUN_CLAIMED)

            with self.assertRaises(DurableWalError):
                duplicate.append(
                    WalEventKind.PROCESS_CREATE_INTENT,
                    artifact_digest="c" * 64,
                    expected_object="Authorization header",
                )


class ProcessOrderingTests(unittest.TestCase):
    def test_process_create_intent_precedes_exact_root_created_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "fake_codex.py"
            script.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "import time\n"
                "Path(sys.argv[1]).write_text("
                "'BRIDGE_EXECUTION_JSON: {\\\"status\\\":\\\"SUCCESS\\\"}', "
                "encoding='utf-8')\n"
                "time.sleep(0.05)\n",
                encoding="utf-8",
            )
            output = root / "run" / "final-message.txt"
            events: list[str] = []

            class _TestContainedLauncher(SubprocessProcessLauncher):
                is_contained = True

            result = run_codex_process(
                args=["python", str(script), str(output)],
                workdir=root,
                output_file=output,
                stdout_log_path=output.with_name("stdout.log"),
                stderr_log_path=output.with_name("stderr.log"),
                prompt="test",
                execution_timeout_seconds=3,
                final_grace_timeout_seconds=0.3,
                cleanup_timeout_seconds=2,
                marker_stable_seconds=0.04,
                poll_interval_seconds=0.02,
                max_log_bytes=1024 * 1024,
                max_final_message_bytes=1024 * 1024,
                marker_parser=lambda text: (
                    {"status": "SUCCESS"}
                    if 'BRIDGE_EXECUTION_JSON: {"status":"SUCCESS"}' in text
                    else None
                ),
                event_logger=lambda event, _fields: events.append(event),
                process_launcher=_TestContainedLauncher(),
            )
            self.assertEqual(result.marker, {"status": "SUCCESS"})
            self.assertLess(
                events.index("process_create_intent"),
                events.index("process_created"),
            )
            self.assertIn("process_exit", events)


if __name__ == "__main__":
    unittest.main()
