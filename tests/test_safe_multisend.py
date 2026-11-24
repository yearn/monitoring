"""Tests for safe/multisend.py."""

import unittest

from eth_abi import encode

from protocols.safe.multisend import (
    build_context_note,
    decode_multisend,
    extract_inner_calls,
    is_multisend_call,
    is_simulatable_multisend,
    safe_utility_label,
)


class TestSafeUtilityLabel(unittest.TestCase):
    def test_known_multisend_call_only(self) -> None:
        self.assertEqual(
            safe_utility_label("0x40A2aCCbd92BCA938b02010E17A5b8929b49130D"),
            "Safe MultiSendCallOnly",
        )

    def test_case_insensitive(self) -> None:
        self.assertEqual(
            safe_utility_label("0x40a2accbd92bca938b02010e17a5b8929b49130d"),
            "Safe MultiSendCallOnly",
        )

    def test_unknown_address(self) -> None:
        self.assertEqual(safe_utility_label("0x000000000000000000000000000000000000dead"), "")


MULTISEND_CALL_ONLY = "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D"

# yChad nonce 2308 from the Safe Queue Service: MultiSendCallOnly batch with one USDC transfer.
YCHAD_2308_DATA = (
    "0x8d80ff0a0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000099"
    "00a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000044"
    "a9059cbb000000000000000000000000b9d026f3f383872922d2c3bebfbfa2affa06ec24"
    "000000000000000000000000000000000000000000000000000000001dcd6500"
    "00000000000000"
)


def pack_multisend(calls: list[tuple[int, str, int, str]]) -> str:
    """Encode ``multiSend(bytes)`` calldata from ``(operation, to, value, data_hex)`` tuples."""
    packed = b"".join(
        bytes([operation])
        + bytes.fromhex(to[2:])
        + value.to_bytes(32, "big")
        + len(bytes.fromhex(data[2:])).to_bytes(32, "big")
        + bytes.fromhex(data[2:])
        for operation, to, value, data in calls
    )
    return "0x8d80ff0a" + encode(["bytes"], [packed]).hex()


def multisend_tx(data: str | None, to: str = MULTISEND_CALL_ONLY, operation: int = 1) -> dict:
    """A Safe Queue Service row for a multisend tx (addresses lowercase, as the service returns them)."""
    return {"to": to.lower(), "operation": operation, "data": data}


class TestDecodeMultisend(unittest.TestCase):
    def test_decodes_real_queue_service_payload(self) -> None:
        self.assertEqual(
            decode_multisend(YCHAD_2308_DATA),
            [
                {
                    "target": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "data": "0xa9059cbb000000000000000000000000b9d026f3f383872922d2c3bebfbfa2affa06ec24"
                    "000000000000000000000000000000000000000000000000000000001dcd6500",
                    "value": "0",
                    "operation": "0",
                }
            ],
        )

    def test_decodes_calls_in_order_with_value_and_operation(self) -> None:
        data = pack_multisend(
            [
                (0, "0x8772e3a2d86b9347a2688f9bc1808a6d8917760c", 0, "0xe318b52b" + "00" * 96),
                (1, "0x000000000000000000000000000000000000abcd", 5, "0x"),
            ]
        )
        calls = decode_multisend(data)
        self.assertEqual(
            [(c["target"], c["value"], c["operation"]) for c in calls],
            [
                ("0x8772E3a2D86B9347A2688f9bc1808A6d8917760C", "0", "0"),
                ("0x000000000000000000000000000000000000ABcD", "5", "1"),
            ],
        )
        self.assertTrue(calls[0]["data"].startswith("0xe318b52b"))
        self.assertEqual(calls[1]["data"], "0x")

    def test_rejects_malformed_payloads(self) -> None:
        call = (0, "0x000000000000000000000000000000000000abcd", 0, "0x12345678")
        valid = pack_multisend([call])
        cases = {
            "wrong selector": "0xdeadbeef" + valid[10:],
            "argument does not decode": valid[:20],
            "invalid operation": pack_multisend([(2, *call[1:])]),
            "empty payload": pack_multisend([]),
        }
        # A packed call whose declared data length runs past the payload.
        header = bytes([0]) + bytes(20) + (0).to_bytes(32, "big") + (64).to_bytes(32, "big")
        cases["overrun"] = "0x8d80ff0a" + encode(["bytes"], [header + b"\x01"]).hex()
        cases["truncated header"] = "0x8d80ff0a" + encode(["bytes"], [header[:40]]).hex()
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                decode_multisend(data)


class TestExtractInnerCalls(unittest.TestCase):
    def test_delegatecall_into_multisend_is_decoded(self) -> None:
        calls = extract_inner_calls(multisend_tx(YCHAD_2308_DATA))
        self.assertEqual([c["target"] for c in calls], ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"])

    def test_official_multisend_deployments_are_decoded(self) -> None:
        # v1.3.0 canonical + EIP-155, v1.4.1, and the MultiSend variants (safe-global/safe-deployments).
        for to in (
            MULTISEND_CALL_ONLY,
            "0xA1dabEF33b3B82c7814B6D82A79e50F4AC44102B",
            "0x9641d764fc13c8B624c04430C7356C1C7C8102e2",
            "0x8D29bE29923b68abfDD21e541b9374737B49cdAD",
            "0xA238CBeb142c10Ef7Ad8442C6D1f9E89e07e7761",
            "0x998739BFdAAdde7C933B942a68053933098f9EDa",
            "0x38869bf66a61cF6bDB996A6aE40D5853Fd43B526",
        ):
            with self.subTest(to=to):
                self.assertEqual(len(extract_inner_calls(multisend_tx(YCHAD_2308_DATA, to=to))), 1)

    def test_sign_message_lib_is_not_a_multisend(self) -> None:
        tx = multisend_tx(YCHAD_2308_DATA, to="0xd53cd0ab83d845ac265be939c57f53ad838012c9")
        self.assertEqual(extract_inner_calls(tx), [])

    def test_multisend_selector_on_unknown_destination_stays_single_call(self) -> None:
        tx = multisend_tx(YCHAD_2308_DATA, to="0x000000000000000000000000000000000000dead")
        self.assertFalse(is_multisend_call(tx))
        self.assertEqual(extract_inner_calls(tx), [])

    def test_plain_call_into_multisend_stays_single_call(self) -> None:
        tx = multisend_tx(YCHAD_2308_DATA, operation=0)
        self.assertFalse(is_multisend_call(tx))
        self.assertEqual(extract_inner_calls(tx), [])

    def test_other_selector_on_multisend_stays_single_call(self) -> None:
        self.assertEqual(extract_inner_calls(multisend_tx("0xa9059cbb" + "00" * 64)), [])

    def test_null_data_returns_empty(self) -> None:
        self.assertEqual(extract_inner_calls(multisend_tx(None)), [])

    def test_malformed_payload_returns_empty(self) -> None:
        self.assertEqual(extract_inner_calls(multisend_tx(YCHAD_2308_DATA[:200])), [])


class TestIsSimulatableMultisend(unittest.TestCase):
    """Only a delegatecall into a multisend utility whose inner txs are all CALLs is modeled."""

    CALLS = [{"target": "0xA", "data": "0x", "value": "0", "operation": "0"}]

    def test_all_calls_via_multisend(self) -> None:
        self.assertTrue(is_simulatable_multisend({"to": MULTISEND_CALL_ONLY, "operation": 1}, self.CALLS))

    def test_inner_delegatecall_is_not(self) -> None:
        calls = [*self.CALLS, {"target": "0xB", "data": "0x", "value": "0", "operation": "1"}]
        self.assertFalse(is_simulatable_multisend({"to": MULTISEND_CALL_ONLY, "operation": 1}, calls))

    def test_unknown_delegate_target_is_not(self) -> None:
        self.assertFalse(is_simulatable_multisend({"to": "0xCustomDelegated", "operation": 1}, self.CALLS))

    def test_sign_message_lib_is_not(self) -> None:
        tx = {"to": "0xd53cd0ab83d845ac265be939c57f53ad838012c9", "operation": 1}
        self.assertFalse(is_simulatable_multisend(tx, self.CALLS))

    def test_empty_batch_is_not(self) -> None:
        self.assertFalse(is_simulatable_multisend({"to": MULTISEND_CALL_ONLY, "operation": 1}, []))


class TestBuildContextNote(unittest.TestCase):
    def test_simulatable_multisend_describes_the_model(self) -> None:
        tx = {"to": MULTISEND_CALL_ONLY, "operation": 1}
        note = build_context_note(tx, "0xSafe", simulate_as_bundle=True)
        self.assertIn("can be simulated as one ordered bundle", note)
        self.assertNotIn("skipped", note)

    def test_delegatecall_into_known_multisend(self) -> None:
        tx = {"to": "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D", "operation": 1}
        note = build_context_note(tx, "0xSafe123")
        self.assertIn("DELEGATECALL", note)
        self.assertIn("0xSafe123", note)
        self.assertIn("Safe MultiSendCallOnly", note)
        self.assertIn("simulation was skipped", note.lower())

    def test_delegatecall_into_unknown_target(self) -> None:
        tx = {"to": "0xCustomDelegated", "operation": 1}
        note = build_context_note(tx, "0xSafe")
        self.assertIn("DELEGATECALL", note)
        self.assertNotIn("MultiSend", note)

    def test_plain_call_returns_empty(self) -> None:
        tx = {"to": "0xFoo", "operation": 0}
        self.assertEqual(build_context_note(tx, "0xSafe"), "")

    def test_missing_operation_returns_empty(self) -> None:
        self.assertEqual(build_context_note({"to": "0xFoo"}, "0xSafe"), "")


if __name__ == "__main__":
    unittest.main()
