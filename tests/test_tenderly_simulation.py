"""Tests for utils/tenderly/simulation.py.

Tests cover balance overrides and status interpretation at the API boundary.
"""

import unittest
from unittest.mock import MagicMock, patch

from utils.tenderly.simulation import (
    BundleCall,
    _merge_balance_override,
    _parse_transaction,
    simulate_bundle,
    simulate_transaction,
)


class TestSimulationStatus(unittest.TestCase):
    """Only recognized terminal statuses establish a simulation outcome."""

    def test_status_values(self) -> None:
        cases = [
            (True, True),
            (False, False),
            ("success", True),
            ("failed", False),
            ("false", None),
            ("pending", None),
            ("", None),
            (None, None),
            (1, None),
            (["success"], None),
            ({"status": "success"}, None),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                result = _parse_transaction(
                    {
                        "status": status,
                        "transaction_info": {"stack_trace": [{"error_reason": "execution reverted"}]},
                    },
                    raw_response={},
                )
                if expected is None:
                    self.assertIsNone(result)
                else:
                    assert result is not None
                    self.assertIs(result.success, expected)
                    self.assertEqual(result.error_message, "" if expected else "execution reverted")

    def test_failed_call_keeps_its_raw_revert_data(self) -> None:
        """Tenderly names require strings but leaves custom errors as raw output; keep it for decoding."""
        output = "0x1f9360170000000000000000000000005086bf358635b81d8c47c66d1c8b9e567db70c72"
        tx = {"status": False, "transaction_info": {"call_trace": {"output": output}, "stack_trace": [{}]}}
        result = _parse_transaction(tx, raw_response={})
        assert result is not None
        self.assertEqual(result.revert_data, output)
        self.assertEqual(result.error_message, "")

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"})
    def test_single_response_without_terminal_status_is_unavailable(self, mock_fetch: MagicMock) -> None:
        for response in (
            {"error": {"message": "request failed"}},
            {"transaction": {}},
            {"transaction": {"status": "pending"}},
            {"transaction": {"status": ""}},
        ):
            with self.subTest(response=response):
                mock_fetch.return_value = response
                self.assertIsNone(simulate_transaction("0xTarget", "0x", chain_id=1))

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"})
    def test_single_explicit_failure_status_preserves_revert_reason(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = {
            "transaction": {
                "status": False,
                "transaction_info": {"stack_trace": [{"error_reason": "Change not initiated"}]},
            }
        }
        result = simulate_transaction("0xTarget", "0x", chain_id=1)
        assert result is not None
        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "Change not initiated")

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"})
    def test_failed_bundle_preserves_error_and_skipped_calls(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = {
            "simulation_results": [
                {"transaction": {"status": "success"}},
                {
                    "transaction": {
                        "status": "failed",
                        "transaction_info": {"stack_trace": [{"error_reason": "execution reverted"}]},
                    }
                },
                {"transaction": None},
            ]
        }
        results = simulate_bundle([BundleCall("0xTarget", "0x")] * 3, chain_id=1, from_address="0xExec")
        assert results is not None
        self.assertEqual(len(results), 3)
        first, failed, skipped = results
        assert first is not None and failed is not None
        self.assertIs(first.success, True)
        self.assertIs(failed.success, False)
        self.assertEqual(failed.error_message, "execution reverted")
        self.assertIsNone(skipped)

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"})
    def test_incomplete_bundle_without_a_revert_is_unavailable(self, mock_fetch: MagicMock) -> None:
        for transactions in (
            [{"status": "success"}],
            [{"status": "success"}, {"status": "pending"}],
        ):
            with self.subTest(transactions=transactions):
                mock_fetch.return_value = {"simulation_results": [{"transaction": tx} for tx in transactions]}
                self.assertIsNone(
                    simulate_bundle([BundleCall("0xTarget", "0x")] * 2, chain_id=1, from_address="0xExec")
                )


class TestMergeBalanceOverride(unittest.TestCase):
    """Tests for _merge_balance_override."""

    def test_value_adds_balance_for_sender(self) -> None:
        out = _merge_balance_override(None, "0xExec", 10**18)
        self.assertEqual(out, {"0xExec": {"balance": hex(10**18)}})

    def test_caller_override_wins(self) -> None:
        existing = {"0xExec": {"balance": "0xdead", "storage": {"0x1": "0x2"}}}
        out = _merge_balance_override(existing, "0xExec", 10**18)
        # Caller-supplied balance is preserved; storage carried through.
        self.assertEqual(out["0xExec"]["balance"], "0xdead")
        self.assertEqual(out["0xExec"]["storage"], {"0x1": "0x2"})


if __name__ == "__main__":
    unittest.main()
