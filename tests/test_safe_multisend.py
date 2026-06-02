"""Tests for safe/multisend.py."""

import unittest

from safe.multisend import build_context_note, extract_inner_calls, safe_utility_label


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


class TestExtractInnerCalls(unittest.TestCase):
    def test_decodes_two_inner_swap_owner_calls(self) -> None:
        # Shape mirrors the Safe Transaction Service API response for the
        # Lido nonce-10 swapOwner batch.
        tx = {
            "to": "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D",
            "operation": 1,
            "dataDecoded": {
                "method": "multiSend",
                "parameters": [
                    {
                        "name": "transactions",
                        "valueDecoded": [
                            {
                                "operation": 0,
                                "to": "0x8772E3a2D86B9347A2688f9bc1808A6d8917760C",
                                "value": "0",
                                "data": "0xe318b52b" + "00" * 96,
                            },
                            {
                                "operation": 0,
                                "to": "0x8772E3a2D86B9347A2688f9bc1808A6d8917760C",
                                "value": "0",
                                "data": "0xe318b52b" + "11" * 96,
                            },
                        ],
                    }
                ],
            },
        }
        calls = extract_inner_calls(tx)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["target"], "0x8772E3a2D86B9347A2688f9bc1808A6d8917760C")
        self.assertTrue(calls[0]["data"].startswith("0xe318b52b"))
        self.assertEqual(calls[0]["value"], "0")

    def test_non_multisend_returns_empty(self) -> None:
        tx = {
            "to": "0xSomeContract",
            "operation": 0,
            "dataDecoded": {"method": "swapOwner", "parameters": []},
        }
        self.assertEqual(extract_inner_calls(tx), [])

    def test_missing_data_decoded_returns_empty(self) -> None:
        self.assertEqual(extract_inner_calls({"to": "0xFoo", "operation": 1}), [])

    def test_value_decoded_not_a_list_returns_empty(self) -> None:
        tx = {
            "dataDecoded": {
                "method": "multiSend",
                "parameters": [{"valueDecoded": None}],
            }
        }
        self.assertEqual(extract_inner_calls(tx), [])

    def test_skips_inner_calls_without_target(self) -> None:
        tx = {
            "dataDecoded": {
                "method": "multiSend",
                "parameters": [
                    {
                        "valueDecoded": [
                            {"operation": 0, "to": None, "value": "0", "data": "0x"},
                            {"operation": 0, "to": "0xABC", "value": "0", "data": "0x"},
                        ]
                    }
                ],
            }
        }
        calls = extract_inner_calls(tx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target"], "0xABC")


class TestBuildContextNote(unittest.TestCase):
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
