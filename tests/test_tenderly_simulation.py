"""Tests for utils/tenderly/simulation.py."""

import unittest
from unittest.mock import patch

from utils.tenderly.simulation import (
    BundleCall,
    _merge_balance_override,
    _parse_asset_changes,
    _parse_state_changes,
    simulate_bundle,
    simulate_transaction,
)


class TestParseAssetChanges(unittest.TestCase):
    """Tests for _parse_asset_changes."""

    def test_empty_list(self) -> None:
        self.assertEqual(_parse_asset_changes([]), [])

    def test_single_transfer(self) -> None:
        raw = [
            {
                "token_info": {
                    "contract_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "decimals": "6",
                },
                "from": "0xSender",
                "to": "0xReceiver",
                "amount": "1000",
                "raw_amount": "1000000000",
            }
        ]
        result = _parse_asset_changes(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].token_symbol, "USDC")
        self.assertEqual(result[0].token_name, "USD Coin")
        self.assertEqual(result[0].from_address, "0xSender")
        self.assertEqual(result[0].to_address, "0xReceiver")
        self.assertEqual(result[0].decimals, 6)

    def test_missing_token_info(self) -> None:
        raw = [{"from": "0xA", "to": "0xB", "amount": "100", "raw_amount": "100"}]
        result = _parse_asset_changes(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].token_symbol, "???")
        self.assertEqual(result[0].token_name, "Unknown")


class TestParseStateChanges(unittest.TestCase):
    """Tests for _parse_state_changes."""

    def test_empty_list(self) -> None:
        self.assertEqual(_parse_state_changes([]), [])

    def test_single_state_change(self) -> None:
        raw = [
            {
                "address": "0xContract",
                "storage_changes": [
                    {"key": "0x01", "original": "0x00", "dirty": "0x01"},
                ],
            }
        ]
        result = _parse_state_changes(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].contract_address, "0xContract")
        self.assertEqual(result[0].key, "0x01")
        self.assertEqual(result[0].original, "0x00")
        self.assertEqual(result[0].dirty, "0x01")

    def test_multiple_storage_changes_same_contract(self) -> None:
        raw = [
            {
                "address": "0xContract",
                "storage_changes": [
                    {"key": "0x01", "original": "0x00", "dirty": "0x01"},
                    {"key": "0x02", "original": "0x10", "dirty": "0x20"},
                ],
            }
        ]
        result = _parse_state_changes(raw)
        self.assertEqual(len(result), 2)

    def test_no_storage_changes(self) -> None:
        raw = [{"address": "0xContract"}]
        result = _parse_state_changes(raw)
        self.assertEqual(len(result), 0)


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


class TestSimulateTransaction(unittest.TestCase):
    """Tests for simulate_transaction."""

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_value_injects_state_objects(self, mock_fetch: unittest.mock.MagicMock) -> None:
        mock_fetch.return_value = {"transaction": {"status": True, "transaction_info": {"gas_used": 1}}}
        simulate_transaction(
            target="0xTarget",
            calldata="0x12345678",
            chain_id=1,
            value=10**18,
            from_address="0xExec",
        )
        body = mock_fetch.call_args.kwargs["json"]
        self.assertEqual(body["state_objects"], {"0xExec": {"balance": hex(10**18)}})

    @patch.dict("os.environ", {"TENDERLY_API_KEY": ""}, clear=False)
    def test_no_api_key_returns_none(self) -> None:
        result = simulate_transaction(
            target="0xTarget",
            calldata="0x12345678",
            chain_id=1,
        )
        self.assertIsNone(result)

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_successful_simulation(self, mock_fetch: unittest.mock.MagicMock) -> None:
        mock_fetch.return_value = {
            "transaction": {
                "status": True,
                "transaction_info": {
                    "gas_used": 50000,
                    "asset_changes": [
                        {
                            "token_info": {
                                "contract_address": "0xToken",
                                "name": "Test Token",
                                "symbol": "TST",
                                "decimals": "18",
                            },
                            "from": "0xA",
                            "to": "0xB",
                            "amount": "100",
                            "raw_amount": "100000000000000000000",
                        }
                    ],
                    "state_diff": [],
                    "logs": [],
                },
            }
        }

        result = simulate_transaction(
            target="0xTarget",
            calldata="0x12345678",
            chain_id=1,
        )

        self.assertIsNotNone(result)
        assert result is not None  # for type narrowing
        self.assertTrue(result.success)
        self.assertEqual(result.gas_used, 50000)
        self.assertEqual(len(result.asset_changes), 1)
        self.assertEqual(result.asset_changes[0].token_symbol, "TST")

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_failed_simulation(self, mock_fetch: unittest.mock.MagicMock) -> None:
        mock_fetch.return_value = {
            "transaction": {
                "status": False,
                "transaction_info": {
                    "gas_used": 21000,
                    "asset_changes": [],
                    "state_diff": [],
                    "logs": [],
                    "stack_trace": [{"error_reason": "execution reverted"}],
                },
            }
        }

        result = simulate_transaction(
            target="0xTarget",
            calldata="0x12345678",
            chain_id=1,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "execution reverted")

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_fetch_returns_none(self, mock_fetch: unittest.mock.MagicMock) -> None:
        mock_fetch.return_value = None
        result = simulate_transaction(target="0xTarget", calldata="0x12345678", chain_id=1)
        self.assertIsNone(result)


class TestSimulateBundle(unittest.TestCase):
    """Tests for simulate_bundle."""

    CALLS = [
        BundleCall(target="0xOracleRegistry", calldata="0x5c38eb3a"),
        BundleCall(target="0xFarm", calldata="0x6817031b", value=5),
        BundleCall(target="0xRegistry", calldata="0x56d2ed05"),
    ]

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_posts_calls_in_order_from_the_executor(self, mock_fetch: unittest.mock.MagicMock) -> None:
        mock_fetch.return_value = {"simulation_results": [{"transaction": {"status": True}}] * 3}
        simulate_bundle(self.CALLS, chain_id=1, from_address="0xTimelock")

        url = mock_fetch.call_args.args[0]
        self.assertTrue(url.endswith("/simulate-bundle"))
        simulations = mock_fetch.call_args.kwargs["json"]["simulations"]
        self.assertEqual([sim["to"] for sim in simulations], ["0xOracleRegistry", "0xFarm", "0xRegistry"])
        self.assertTrue(all(sim["from"] == "0xTimelock" for sim in simulations))
        # The executor is funded once, for the whole batch, before the first call.
        self.assertEqual(simulations[0]["state_objects"], {"0xTimelock": {"balance": hex(5)}})
        self.assertNotIn("state_objects", simulations[1])

    @patch("utils.tenderly.simulation.fetch_json")
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_results_align_with_calls_after_early_stop(self, mock_fetch: unittest.mock.MagicMock) -> None:
        # Tenderly stops at the first revert, returning only the calls it ran.
        mock_fetch.return_value = {
            "simulation_results": [
                {"transaction": {"status": True, "gas_used": 21000, "transaction_info": {"logs": [{"name": "Set"}]}}},
                {
                    "transaction": {
                        "status": False,
                        "transaction_info": {"stack_trace": [{"error_reason": "InvalidOracle"}]},
                    }
                },
            ]
        }
        results = simulate_bundle(self.CALLS, chain_id=1, from_address="0xTimelock")

        assert results is not None
        self.assertEqual(len(results), 3)
        first, second, third = results
        assert first is not None and second is not None
        self.assertTrue(first.success)
        # Bundle results carry gas on the transaction, not inside transaction_info.
        self.assertEqual(first.gas_used, 21000)
        self.assertEqual(first.logs, [{"name": "Set"}])
        self.assertFalse(second.success)
        self.assertEqual(second.error_message, "InvalidOracle")
        self.assertIsNone(third)

    @patch("utils.tenderly.simulation.fetch_json", return_value=None)
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_request_failure_returns_none(self, _mock_fetch: unittest.mock.MagicMock) -> None:
        self.assertIsNone(simulate_bundle(self.CALLS, chain_id=1, from_address="0xTimelock"))

    @patch("utils.tenderly.simulation.fetch_json", return_value={"simulation_results": []})
    @patch.dict("os.environ", {"TENDERLY_API_KEY": "test-key"}, clear=False)
    def test_empty_results_return_none(self, _mock_fetch: unittest.mock.MagicMock) -> None:
        self.assertIsNone(simulate_bundle(self.CALLS, chain_id=1, from_address="0xTimelock"))

    @patch.dict("os.environ", {"TENDERLY_API_KEY": ""}, clear=False)
    def test_no_api_key_returns_none(self) -> None:
        self.assertIsNone(simulate_bundle(self.CALLS, chain_id=1, from_address="0xTimelock"))


if __name__ == "__main__":
    unittest.main()
