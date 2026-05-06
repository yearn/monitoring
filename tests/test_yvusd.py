import unittest
from unittest.mock import MagicMock, patch

from utils.chains import Chain
from yearn.yvusd import (
    CCTP_REPORT_SKEW_HOURS,
    CCTP_REPORT_STALENESS_HOURS,
    LOOPER_CHAIN_CONFIG,
    YVUSD_VAULT,
    LooperPosition,
    _collect_looper_positions,
    check_flashloan_liquidity,
    check_large_cooldowns,
    check_strategy_staleness,
)


def _make_remote_strategy_mock(last_report: int, total_assets: int) -> MagicMock:
    """Build a mock V3 tokenized strategy contract for the remote side."""
    contract = MagicMock()
    contract.functions.lastReport.return_value.call.return_value = last_report
    contract.functions.totalAssets.return_value.call.return_value = total_assets
    return contract


class TestYvUsdCctpChecks(unittest.TestCase):
    @patch("yearn.yvusd.send_alert")
    @patch("yearn.yvusd.ChainManager.get_client")
    def test_alerts_on_report_skew_between_local_and_remote(
        self, mock_get_client: MagicMock, mock_send_alert: MagicMock
    ):
        now = 1_000_000
        local_last_report = now - 3600
        remote_last_report = now - int((CCTP_REPORT_SKEW_HOURS + 2) * 3600)

        remote_strategy = _make_remote_strategy_mock(remote_last_report, 100_000_000)
        remote_client = MagicMock()
        remote_client.eth.contract.return_value = remote_strategy
        mock_get_client.return_value = remote_client

        mainnet_vault = MagicMock()
        client = MagicMock()
        client.eth.contract.return_value = mainnet_vault
        client.batch_requests.return_value.__enter__.return_value = MagicMock()
        client.batch_requests.return_value.__exit__.return_value = False
        client.execute_batch.return_value = [(1, local_last_report, 100_000_000, 0)]

        api_data = {
            YVUSD_VAULT: {
                "meta": {
                    "strategies": [
                        {
                            "address": "0x1983923e5a3591AFe036d38A8C8011e66Cd76e9E",
                            "meta": {
                                "name": "Arbitrum Yearn Degen Morpho Compounder",
                                "type": "cross-chain",
                                "remote_chain_id": Chain.ARBITRUM.chain_id,
                                "remote_vault": "0x78b7774c4368df8f2c115Abf6210F557753a6aC5",
                            },
                        }
                    ]
                }
            }
        }

        with patch("yearn.yvusd.time.time", return_value=now):
            check_strategy_staleness(client, api_data)

        mock_send_alert.assert_called_once()
        message = mock_send_alert.call_args.args[0].message
        self.assertIn("report skew", message)
        self.assertIn("Arbitrum Yearn Degen Morpho Compounder", message)

    @patch("yearn.yvusd.send_alert")
    @patch("yearn.yvusd.ChainManager.get_client")
    def test_alerts_on_remote_staleness(self, mock_get_client: MagicMock, mock_send_alert: MagicMock):
        now = 1_000_000
        stale_seconds = int((CCTP_REPORT_STALENESS_HOURS + 1) * 3600)

        remote_strategy = _make_remote_strategy_mock(now - stale_seconds, 200_000_000)
        remote_client = MagicMock()
        remote_client.eth.contract.return_value = remote_strategy
        mock_get_client.return_value = remote_client

        client = MagicMock()
        client.eth.contract.return_value = MagicMock()
        client.batch_requests.return_value.__enter__.return_value = MagicMock()
        client.batch_requests.return_value.__exit__.return_value = False
        client.execute_batch.return_value = [(1, now - 3600, 100_000_000, 0)]

        api_data = {
            YVUSD_VAULT: {
                "meta": {
                    "strategies": [
                        {
                            "address": "0x2F56D106C6Df739bdbb777C2feE79FFaED88D179",
                            "meta": {
                                "name": "Arbitrum syrupUSDC/USDC Morpho Looper",
                                "type": "cross-chain",
                                "remote_chain_id": Chain.ARBITRUM.chain_id,
                                "remote_vault": "0xBCf08997C34183d1b7B0f99e13aCeACFBA88E453",
                            },
                        }
                    ]
                }
            }
        }

        with patch("yearn.yvusd.time.time", return_value=now):
            check_strategy_staleness(client, api_data)

        mock_send_alert.assert_called_once()
        self.assertIn("report stale", mock_send_alert.call_args.args[0].message)

    @patch("yearn.yvusd.send_alert")
    @patch("yearn.yvusd.ChainManager.get_client")
    def test_alerts_when_remote_lookup_fails(self, mock_get_client: MagicMock, mock_send_alert: MagicMock):
        """Failure to read remote state must surface an alert, not silently skip."""
        now = 1_000_000

        remote_strategy = MagicMock()
        remote_strategy.functions.lastReport.return_value.call.side_effect = RuntimeError("execution reverted")
        remote_client = MagicMock()
        remote_client.eth.contract.return_value = remote_strategy
        mock_get_client.return_value = remote_client

        client = MagicMock()
        client.eth.contract.return_value = MagicMock()
        client.batch_requests.return_value.__enter__.return_value = MagicMock()
        client.batch_requests.return_value.__exit__.return_value = False
        client.execute_batch.return_value = [(1, now - 3600, 100_000_000, 0)]

        api_data = {
            YVUSD_VAULT: {
                "meta": {
                    "strategies": [
                        {
                            "address": "0x2F56D106C6Df739bdbb777C2feE79FFaED88D179",
                            "meta": {
                                "name": "Arbitrum syrupUSDC/USDC Morpho Looper",
                                "type": "cross-chain",
                                "remote_chain_id": Chain.ARBITRUM.chain_id,
                                "remote_vault": "0xBCf08997C34183d1b7B0f99e13aCeACFBA88E453",
                            },
                        }
                    ]
                }
            }
        }

        with patch("yearn.yvusd.time.time", return_value=now):
            check_strategy_staleness(client, api_data)

        mock_send_alert.assert_called_once()
        message = mock_send_alert.call_args.args[0].message
        self.assertIn("Remote Lookup Failed", message)
        self.assertIn("Arbitrum syrupUSDC/USDC Morpho Looper", message)


class TestYvUsdLooperPositionCollection(unittest.TestCase):
    def test_includes_cross_chain_loopers_with_remote_morpho_market(self):
        """Cross-chain wrappers whose remote side is a looper must be covered."""
        strategies = [
            {
                "address": "0xMainnetLooper",
                "debt": "5000000000000",
                "meta": {
                    "name": "Mainnet Direct Looper",
                    "type": "morpho-looper",
                    "market_id": "0xaaaa",
                },
            },
            {
                "address": "0x2F56D106C6Df739bdbb777C2feE79FFaED88D179",
                "debt": "100404831974",
                "meta": {
                    "name": "Arbitrum syrupUSDC/USDC Morpho Looper",
                    "type": "cross-chain",
                    "remote_chain_id": Chain.ARBITRUM.chain_id,
                    "remote_vault": "0xBCf08997C34183d1b7B0f99e13aCeACFBA88E453",
                    "remote_vault_type": "morpho-looper",
                    "remote_meta": {
                        "type": "morpho-looper",
                        "market_id": "0xf86f3edd6f16cd8211f4d206866dc4ecd41be6211063ac11f8508e1b7112ef40",
                    },
                },
            },
            {
                "address": "0xCrossChainNonLooper",
                "debt": "1000",
                "meta": {
                    "name": "Cross-chain default vault",
                    "type": "cross-chain",
                    "remote_chain_id": Chain.ARBITRUM.chain_id,
                    "remote_vault": "0x000000000000000000000000000000000000dead",
                    "remote_vault_type": "default",
                    "remote_meta": {"type": "default"},
                },
            },
            {
                "address": "0xZeroDebtLooper",
                "debt": "0",
                "meta": {"name": "Zero debt", "type": "morpho-looper", "market_id": "0xbbbb"},
            },
        ]

        positions = _collect_looper_positions(strategies)

        self.assertEqual(len(positions), 2)
        mainnet = next(p for p in positions if p.chain == Chain.MAINNET)
        cross = next(p for p in positions if p.chain == Chain.ARBITRUM)

        self.assertEqual(mainnet.borrower, "0xMainnetLooper")
        self.assertEqual(mainnet.market_id, "0xaaaa")

        # Borrower for cross-chain is the remote tokenized strategy, not the mainnet wrapper.
        self.assertEqual(cross.borrower, "0xBCf08997C34183d1b7B0f99e13aCeACFBA88E453")
        self.assertEqual(cross.mainnet_strategy, "0x2F56D106C6Df739bdbb777C2feE79FFaED88D179")
        self.assertEqual(cross.market_id, "0xf86f3edd6f16cd8211f4d206866dc4ecd41be6211063ac11f8508e1b7112ef40")


class TestYvUsdFlashloanLiquidity(unittest.TestCase):
    @patch("yearn.yvusd.send_alert")
    @patch("yearn.yvusd.ChainManager.get_client")
    def test_alerts_on_insufficient_liquidity_for_cross_chain_looper(
        self, mock_get_client: MagicMock, mock_send_alert: MagicMock
    ):
        # Borrow shares == borrow assets when total_borrow_shares == total_borrow_assets
        # market: total_supply=10M USDC, total_borrow=9M USDC, shares match -> liquidity = 1M
        # position: borrow shares 50M USDC -> way more than 1M market liquidity and 100k Balancer
        market = (
            10_000_000 * 10**6,  # totalSupplyAssets
            10_000_000 * 10**6,  # totalSupplyShares
            9_000_000 * 10**6,  # totalBorrowAssets
            9_000_000 * 10**6,  # totalBorrowShares
            0,  # lastUpdate
            0,  # fee
        )
        position = (0, 50_000_000 * 10**6, 0)  # supplyShares, borrowShares, collateral
        balancer_balance = 100_000 * 10**6

        arb_client = MagicMock()
        arb_client.batch_requests.return_value.__enter__.return_value = MagicMock()
        arb_client.batch_requests.return_value.__exit__.return_value = False
        arb_client.execute_batch.return_value = [market, position, balancer_balance]
        mock_get_client.return_value = arb_client

        # Verify the chain we care about is configured
        self.assertIn(Chain.ARBITRUM, LOOPER_CHAIN_CONFIG)

        api_data = {
            YVUSD_VAULT: {
                "meta": {
                    "strategies": [
                        {
                            "address": "0x2F56D106C6Df739bdbb777C2feE79FFaED88D179",
                            "debt": "50000000000000",
                            "meta": {
                                "name": "Arbitrum syrupUSDC/USDC Morpho Looper",
                                "type": "cross-chain",
                                "remote_chain_id": Chain.ARBITRUM.chain_id,
                                "remote_vault": "0xBCf08997C34183d1b7B0f99e13aCeACFBA88E453",
                                "remote_vault_type": "morpho-looper",
                                "remote_meta": {
                                    "market_id": "0xf86f3edd6f16cd8211f4d206866dc4ecd41be6211063ac11f8508e1b7112ef40",
                                },
                            },
                        }
                    ]
                }
            }
        }

        check_flashloan_liquidity(api_data)

        mock_get_client.assert_called_once_with(Chain.ARBITRUM)
        mock_send_alert.assert_called_once()
        message = mock_send_alert.call_args.args[0].message
        self.assertIn("Flashloan Liquidity Warning", message)
        self.assertIn("arbitrum", message.lower())
        # Both the mainnet strategy link and the remote borrower link should appear
        self.assertIn("0x2F56D106C6Df739bdbb777C2feE79FFaED88D179", message)
        self.assertIn("0xBCf08997C34183d1b7B0f99e13aCeACFBA88E453", message)


class TestYvUsdCooldownScanning(unittest.TestCase):
    @patch("yearn.yvusd.set_cache_value")
    @patch("yearn.yvusd.get_cache_value", return_value=123)
    def test_does_not_advance_cache_when_log_fetch_fails(self, mock_get_cache: MagicMock, mock_set_cache: MagicMock):
        client = MagicMock()
        client.eth.block_number = 200

        locked = MagicMock()
        locked.events.CooldownStarted.get_logs.side_effect = RuntimeError("rpc failure")
        client.eth.contract.return_value = locked

        check_large_cooldowns(client)

        mock_set_cache.assert_not_called()

    @patch("yearn.yvusd.set_cache_value")
    @patch("yearn.yvusd.get_cache_value", return_value=100)
    def test_uses_snake_case_kwargs_for_get_logs(self, mock_get_cache: MagicMock, mock_set_cache: MagicMock):
        """web3 7.x's get_logs takes from_block/to_block, not fromBlock/toBlock."""
        client = MagicMock()
        client.eth.block_number = 250

        locked = MagicMock()
        locked.events.CooldownStarted.get_logs.return_value = []
        client.eth.contract.return_value = locked

        check_large_cooldowns(client)

        # web3 7.x raises TypeError on camelCase kwargs; assert we used snake_case.
        kwargs = locked.events.CooldownStarted.get_logs.call_args.kwargs
        self.assertIn("from_block", kwargs)
        self.assertIn("to_block", kwargs)
        self.assertNotIn("fromBlock", kwargs)
        self.assertNotIn("toBlock", kwargs)


class TestLooperPositionDataclass(unittest.TestCase):
    def test_dataclass_is_hashable_for_dedup(self):
        """LooperPosition is frozen and should be usable as a dict key."""
        a = LooperPosition(Chain.MAINNET, "0xaa", "0xbb", "name", "0xbb")
        b = LooperPosition(Chain.MAINNET, "0xaa", "0xbb", "name", "0xbb")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))


if __name__ == "__main__":
    unittest.main()
