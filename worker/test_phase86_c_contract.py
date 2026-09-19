"""Pure Phase-8.6-C policy, helper-protocol, and ownership tests."""

from __future__ import annotations

import copy
import unittest

from vnext_runtime.egress import (
    BrokerEndpoint,
    ContainmentIdentity,
    ContainmentState,
    EgressContractError,
    EgressPolicy,
    EgressPolicyReference,
    HelperEnforcement,
    HelperOperation,
    HelperProtocolError,
    HelperReceipt,
    HelperReceiptStatus,
    HelperRequest,
    RunContainment,
    decode_receipt,
    decode_request,
    encode_receipt,
    encode_request,
)
from vnext_runtime.models import RunIdentity
from vnext_runtime.run_scope import RunScope


IDENTITY = RunIdentity("engine-maintenance", 36, "run-036-test", 101)
OTHER_IDENTITY = RunIdentity("engine-maintenance", 37, "run-037-test", 102)
POLICY = EgressPolicy(
    policy_id="phase86-c-default",
    policy_version=1,
    broker=BrokerEndpoint("127.0.0.1", 43127),
    approved_upstream="safe-overseas-proxy-v1",
    logical_destinations=("codex-provider", "github-read-only"),
)


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[HelperRequest] = []

    def exchange(self, request: HelperRequest) -> HelperReceipt:
        self.requests.append(request)
        status = {
            HelperOperation.INSPECT_HEALTH: (
                HelperReceiptStatus.HEALTHY,
                HelperEnforcement.READY,
            ),
            HelperOperation.PREPARE_RUN: (
                HelperReceiptStatus.PREPARED,
                HelperEnforcement.ACTIVE,
            ),
            HelperOperation.RECONCILE_RUN: (
                HelperReceiptStatus.RECONCILED,
                HelperEnforcement.ACTIVE,
            ),
            HelperOperation.RELEASE_RUN: (
                HelperReceiptStatus.RELEASED,
                HelperEnforcement.DENY_RETAINED,
            ),
        }[request.operation]
        return HelperReceipt.for_request(
            request,
            status=status[0],
            enforcement=status[1],
            observed_at="2001-01-15T02:00:00+08:00",
        )


class EgressPolicyTests(unittest.TestCase):
    def test_policy_digest_is_stable_and_reference_has_no_raw_upstream(self) -> None:
        equivalent = EgressPolicy(
            policy_id="phase86-c-default",
            policy_version=1,
            broker=BrokerEndpoint("127.0.0.1", 43127),
            approved_upstream="safe-overseas-proxy-v1",
            logical_destinations=("github-read-only", "codex-provider"),
        )

        self.assertEqual(POLICY.digest, equivalent.digest)
        reference = POLICY.reference()
        self.assertEqual(reference.policy_digest, POLICY.digest)
        self.assertNotIn("approved_upstream", reference.to_wire())
        self.assertNotIn("https://", str(reference.to_wire()))

        with self.assertRaisesRegex(EgressContractError, "lowercase SHA-256"):
            EgressPolicyReference(
                policy_id=reference.policy_id,
                policy_version=reference.policy_version,
                policy_digest=reference.policy_digest.upper(),
                broker=reference.broker,
            )

    def test_policy_rejects_non_loopback_or_address_like_destinations(self) -> None:
        with self.assertRaisesRegex(EgressContractError, "127.0.0.1"):
            BrokerEndpoint("127.0.0.2", 43127)
        with self.assertRaisesRegex(EgressContractError, "logical destination"):
            EgressPolicy(
                policy_id="phase86-c-default",
                policy_version=1,
                broker=BrokerEndpoint("127.0.0.1", 43127),
                approved_upstream="https://proxy.example",
                logical_destinations=("github-read-only",),
            )
        with self.assertRaisesRegex(EgressContractError, "logical destination"):
            EgressPolicy(
                policy_id="phase86-c-default",
                policy_version=1,
                broker=BrokerEndpoint("127.0.0.1", 43127),
                approved_upstream="safe-overseas-proxy-v1",
                logical_destinations=("8.8.8.8",),
            )

    def test_three_run_identities_are_disjoint(self) -> None:
        identities = (
            IDENTITY,
            OTHER_IDENTITY,
            RunIdentity("engine-maintenance", 38, "run-038-test", 103),
        )
        containments = tuple(ContainmentIdentity.derive(item) for item in identities)

        self.assertEqual(len({item.identity_digest for item in containments}), 3)
        self.assertEqual(len({item.profile_name for item in containments}), 3)


class HelperProtocolTests(unittest.TestCase):
    def request(self, operation: HelperOperation = HelperOperation.PREPARE_RUN) -> HelperRequest:
        return HelperRequest.for_operation(
            operation,
            IDENTITY,
            POLICY.reference(),
            request_id="request-036-prepare",
        )

    def test_request_round_trip_is_strict_and_deterministic(self) -> None:
        request = self.request()
        encoded = encode_request(request)

        self.assertEqual(encoded, request.encode())
        self.assertEqual(decode_request(encoded), request)
        self.assertEqual(list(request.to_wire()), [
            "protocol",
            "schema_version",
            "request_id",
            "operation",
            "identity",
            "policy",
            "containment",
        ])

    def test_request_rejects_identity_endpoint_and_unknown_field_tampering(self) -> None:
        identity_tampered = copy.deepcopy(self.request().to_wire())
        identity_tampered["identity"]["run_id"] = "run-other"  # type: ignore[index]
        with self.assertRaises(EgressContractError):
            HelperRequest.from_wire(identity_tampered)

        endpoint_tampered = copy.deepcopy(self.request().to_wire())
        endpoint_tampered["policy"]["broker"]["host"] = "10.0.0.1"  # type: ignore[index]
        with self.assertRaises(EgressContractError):
            HelperRequest.from_wire(endpoint_tampered)

        unknown = self.request().to_wire()
        unknown["arbitrary_command"] = "whoami"
        with self.assertRaisesRegex(EgressContractError, "unsupported fields"):
            HelperRequest.from_wire(unknown)

    def test_receipt_is_bound_to_exact_request_and_round_trips(self) -> None:
        request = self.request()
        receipt = HelperReceipt.for_request(
            request,
            status=HelperReceiptStatus.PREPARED,
            enforcement=HelperEnforcement.ACTIVE,
            observed_at="2001-01-15T02:00:00+08:00",
        )

        receipt.verify_for(request)
        self.assertEqual(decode_receipt(encode_receipt(receipt)), receipt)
        self.assertEqual(decode_receipt(receipt.encode()), receipt)

        other_request = HelperRequest.for_operation(
            HelperOperation.PREPARE_RUN,
            OTHER_IDENTITY,
            POLICY.reference(),
            request_id="request-037-prepare",
        )
        with self.assertRaises(HelperProtocolError):
            receipt.verify_for(other_request)

    def test_preclaim_health_receipt_is_readiness_only(self) -> None:
        request = self.request(HelperOperation.INSPECT_HEALTH)
        receipt = HelperReceipt.for_request(
            request,
            status=HelperReceiptStatus.HEALTHY,
            enforcement=HelperEnforcement.READY,
            observed_at="2001-01-15T02:00:00+08:00",
        )

        self.assertEqual(receipt.operation, HelperOperation.INSPECT_HEALTH)
        self.assertEqual(receipt.enforcement, HelperEnforcement.READY)
        self.assertEqual(receipt.identity, IDENTITY)

    def test_receipt_states_are_fail_closed(self) -> None:
        request = self.request()
        with self.assertRaisesRegex(EgressContractError, "PREPARED"):
            HelperReceipt.for_request(
                request,
                status=HelperReceiptStatus.PREPARED,
                enforcement=HelperEnforcement.READY,
                observed_at="2001-01-15T02:00:00+08:00",
            )

        rejected_request = self.request(HelperOperation.PREPARE_RUN)
        rejected = HelperReceipt.for_request(
            rejected_request,
            status=HelperReceiptStatus.REJECTED,
            enforcement=HelperEnforcement.UNKNOWN,
            observed_at="2001-01-15T02:00:00+08:00",
            error_code="helper_unavailable",
        )
        self.assertEqual(rejected.status, HelperReceiptStatus.REJECTED)


class RunContainmentOwnershipTests(unittest.TestCase):
    def test_run_scope_disposes_only_its_exact_containment(self) -> None:
        transport = _FakeTransport()
        scope = RunScope(IDENTITY)
        scope.activate()
        containment = RunContainment(IDENTITY, POLICY.reference(), transport)
        scope.own(containment)

        containment.prepare()
        containment.reconcile()
        scope.close()

        self.assertEqual(containment.state, ContainmentState.RELEASED)
        self.assertEqual(
            [request.operation for request in transport.requests],
            [
                HelperOperation.PREPARE_RUN,
                HelperOperation.RECONCILE_RUN,
                HelperOperation.RELEASE_RUN,
            ],
        )
        self.assertTrue(all(request.identity == IDENTITY for request in transport.requests))

    def test_cross_run_receipt_cannot_release_or_prepare_this_run(self) -> None:
        class CrossRunTransport(_FakeTransport):
            def exchange(self, request: HelperRequest) -> HelperReceipt:
                other = HelperRequest.for_operation(
                    request.operation,
                    OTHER_IDENTITY,
                    request.policy,
                    request_id="request-037-cross-run",
                )
                return HelperReceipt.for_request(
                    other,
                    status=(
                        HelperReceiptStatus.PREPARED
                        if request.operation is HelperOperation.PREPARE_RUN
                        else HelperReceiptStatus.RELEASED
                    ),
                    enforcement=(
                        HelperEnforcement.ACTIVE
                        if request.operation is HelperOperation.PREPARE_RUN
                        else HelperEnforcement.DENY_RETAINED
                    ),
                    observed_at="2001-01-15T02:00:00+08:00",
                )

        containment = RunContainment(
            IDENTITY,
            POLICY.reference(),
            CrossRunTransport(),
        )
        with self.assertRaises(HelperProtocolError):
            containment.prepare()
        self.assertEqual(containment.state, ContainmentState.UNKNOWN)

    def test_unprepared_handle_does_not_send_release(self) -> None:
        transport = _FakeTransport()
        containment = RunContainment(IDENTITY, POLICY.reference(), transport)

        containment.dispose()

        self.assertEqual(containment.state, ContainmentState.RELEASED)
        self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
