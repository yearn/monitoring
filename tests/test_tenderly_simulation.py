"""Tests for utils/tenderly/simulation.py.

Tests cover balance overrides and status interpretation at the API boundary.
"""

import unittest
from unittest.mock import MagicMock, patch

from utils.tenderly.simulation import BundleCall, _merge_balance_override, _parse_transaction, simulate_bundle


class TestSimulationStatus(unittest.TestCase):
    """Only explicit success statuses may mark a simulation successful."""

    def test_status_values(self) -> None:
        cases = [
            (True, True),
            (False, False),
            ("success", True),
            ("failed", False),
            ("false", False),
            ("pending", False),
            ("", False),
            (None, False),
            (1, False),
            (["success"], False),
            ({"status": "success"}, False),
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
                self.assertIs(result.success, expected)
                self.assertEqual(result.error_message, "" if expected else "execution reverted")

    def test_missing_status_is_not_success(self) -> None:
        result = _parse_transaction({}, raw_response={})
        self.assertIs(result.success, False)

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
