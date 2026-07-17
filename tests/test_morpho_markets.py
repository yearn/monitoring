"""Behavior tests for Morpho v1/v2 market and liquidity monitoring."""

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from protocols.morpho._shared import (
    VAULTS_V2_BY_CHAIN,
    Asset,
    BadDebt,
    MarketMetrics,
    MarketState,
    MorphoMonitoringError,
    MorphoV2MonitoringError,
)
from protocols.morpho.markets import (
    VAULTS_V2_WITH_YV_COLLATERAL_BY_ASSET,
    YV_COLLATERAL_AT_RISK_POINTS,
    YV_COLLATERAL_STABLE_PRICE_SHOCK,
    calculate_combined_metrics,
    collect_yv_collateral_markets,
    fetch_configured_vaults,
    get_markets_collateral_at_risk_usd,
    get_yv_collateral_liquidity_by_asset,
)
from protocols.morpho.markets_v2 import (
    AdapterInfo,
    V2Vault,
    check_low_liquidity,
    discover_v2_vaults_by_chain,
    list_adapters,
    score_market_allocations,
)
from utils.chains import Chain


class TestMorphoV2Configuration(unittest.TestCase):
    def test_katana_collateral_strategy_vaults_are_monitored(self) -> None:
        configured = {str(entry[1]).lower(): int(entry[2]) for entry in VAULTS_V2_BY_CHAIN[Chain.KATANA]}
        expected = {
            "0x4284d4f9f4d61ea57b8f0943547c7c19c5b9b249": 1,
            "0xca44cbe1fb03691d43d2d93aa460e2fcb03878fe": 1,
            "0xa2d38c8a3d810ebcf4c2075821c5ec8f976bb692": 3,
            "0xac596ad9771a8d0d4df108ae0406e6f913aedceb": 1,
            "0x5920a6fc553af799542eda628adfcc9ea52e141c": 1,
            "0xbeeff2d5d126d4809195eea02b605423917bb6c6": 3,
            "0xbeef042bad4472c3f7eb9a73070703788b5362d7": 1,
        }

        for address, risk_level in expected.items():
            self.assertEqual(configured[address], risk_level)

        collateral_vaults = {
            vault[1].lower()
            for vaults in VAULTS_V2_WITH_YV_COLLATERAL_BY_ASSET[Chain.KATANA].values()
            for vault in vaults
        }
        self.assertEqual(collateral_vaults, expected.keys())

    def test_discovery_fails_if_api_omits_configured_vaults(self) -> None:
        response = MagicMock()
        response.json.return_value = {"data": {"vaultV2s": {"items": []}}}

        with (
            patch("protocols.morpho.markets_v2.request_with_retry", return_value=response),
            self.assertRaisesRegex(MorphoV2MonitoringError, "omitted configured Vault V2"),
        ):
            discover_v2_vaults_by_chain()

    def test_adapter_read_failure_is_not_silently_treated_as_empty(self) -> None:
        client = MagicMock()
        client.get_contract.return_value.functions.adaptersLength.return_value.call.side_effect = RuntimeError(
            "RPC unavailable"
        )

        with self.assertRaisesRegex(MorphoV2MonitoringError, "Failed to read adaptersLength"):
            list_adapters(client, "0x" + "11" * 20)

    def test_market_scoring_consolidates_all_v2_risk_alerts(self) -> None:
        market_id = "0x" + "ab" * 32
        vault = V2Vault(
            name="Example",
            address="0x" + "11" * 20,
            chain=Chain.MAINNET,
            asset_address="0x" + "22" * 20,
            asset_symbol="USDC",
            curator="",
            owner="",
            risk_level=1,
        )
        adapter = AdapterInfo(
            address="0x" + "33" * 20,
            kind="MorphoMarketV1AdapterV2",
            market_ids=[market_id],
            expected_supply_assets=[80],
        )
        metrics = MarketMetrics(
            market_id=market_id,
            loan_asset=Asset(address=vault.asset_address, symbol="USDC"),
            collateral_asset=Asset(address="0x" + "44" * 20, symbol="WETH"),
            state=MarketState(
                utilization=0.5,
                borrow_assets=100,
                supply_assets=100,
                borrow_assets_usd=100,
                supply_assets_usd=100,
            ),
            bad_debt=BadDebt(underlying=1, usd=1),
        )

        with (
            patch("protocols.morpho.markets_v2.fetch_market_metrics", return_value={market_id: metrics}),
            patch("protocols.morpho.markets_v2.send_alert") as send,
        ):
            score_market_allocations(vault, [adapter], 100)

        messages = [call.args[0].message for call in send.call_args_list]
        self.assertEqual(len(messages), 3)
        self.assertTrue(any("V2 high allocation" in message for message in messages))
        self.assertTrue(any("V2 bad debt" in message for message in messages))
        self.assertTrue(any("V2 high risk" in message for message in messages))


class TestMorphoCollateralLiquidity(unittest.TestCase):
    def test_collateral_curve_can_resolve_stable_price_shock(self) -> None:
        self.assertGreaterEqual(YV_COLLATERAL_AT_RISK_POINTS, round(1 / YV_COLLATERAL_STABLE_PRICE_SHOCK))

    def test_collateral_risk_api_failure_is_not_silently_skipped(self) -> None:
        response = MagicMock()
        response.json.return_value = {"errors": [{"message": "unavailable"}]}

        with (
            patch("protocols.morpho.markets.request_with_retry", return_value=response),
            self.assertRaisesRegex(MorphoMonitoringError, "errors fetching collateral at risk"),
        ):
            get_markets_collateral_at_risk_usd({"0x" + "ab" * 32: 0.02}, Chain.KATANA)

    def test_configured_data_fetch_fails_if_v1_vaults_are_omitted(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "data": {
                "vaults": {"items": []},
                "vaultV2s": {"items": []},
                "markets": {"items": []},
            }
        }

        with (
            patch("protocols.morpho.markets.request_with_retry", return_value=response),
            self.assertRaisesRegex(MorphoMonitoringError, "omitted configured Vault V1"),
        ):
            fetch_configured_vaults()

    def test_collateral_markets_do_not_depend_on_v1_vault_allocations(self) -> None:
        market_id = "0x6691cdcadd5d23ac68d2c1cf54dc97ab8242d2a888230de411094480252c2ed3"
        asset_address = "0x203a662b0bd271a6ed5a60edfbd04bfce608fd36"
        market = {
            "marketId": market_id,
            "collateralAsset": {"chain": {"id": Chain.KATANA.chain_id}},
            "state": {"borrowAssetsUsd": 100_000},
        }
        liquidity_group = {"asset_address": asset_address}

        result = collect_yv_collateral_markets(
            Chain.KATANA,
            [market],
            {asset_address: liquidity_group},
        )

        self.assertEqual(result, {market_id: (market, liquidity_group)})

    def test_combines_v1_and_v2_withdrawable_liquidity(self) -> None:
        vaults: list[dict[str, Any]] = [
            {
                "__typename": "Vault",
                "name": "Yearn OG USDC",
                "state": {"totalAssetsUsd": 100_000},
                "liquidity": {"usd": 30_000},
            },
            {
                "__typename": "VaultV2",
                "name": "Yearn OG USDC",
                "totalAssetsUsd": 200_000,
                "liquidityUsd": 80_000,
            },
            {
                "__typename": "VaultV2",
                "name": "Dust",
                "totalAssetsUsd": 9_999,
                "liquidityUsd": 9_999,
            },
        ]

        total_assets, liquidity, names = calculate_combined_metrics(vaults)

        self.assertEqual(total_assets, 300_000)
        self.assertEqual(liquidity, 110_000)
        self.assertEqual(names, ["Yearn OG USDC", "Yearn OG USDC (V2)"])

    def test_shared_market_liquidity_is_counted_once_across_v1_and_v2(self) -> None:
        market_id = "0x" + "ab" * 32
        market = {
            "marketId": market_id,
            "collateralAsset": {"symbol": "WETH"},
            "state": {"liquidityAssetsUsd": 100_000},
        }
        vaults: list[dict[str, Any]] = [
            {
                "__typename": "Vault",
                "name": "V1",
                "state": {
                    "totalAssetsUsd": 100_000,
                    "allocation": [
                        {
                            "supplyAssetsUsd": 80_000,
                            "withdrawQueueIndex": 0,
                            "market": market,
                        }
                    ],
                },
                "liquidity": {"usd": 80_000},
            },
            {
                "__typename": "VaultV2",
                "name": "V2",
                "totalAssetsUsd": 100_000,
                "idleAssetsUsd": 0,
                "liquidityUsd": 80_000,
                "liquidityData": {"market": market},
            },
        ]

        _, liquidity, _ = calculate_combined_metrics(vaults)

        self.assertEqual(liquidity, 100_000)

    def test_v2_low_liquidity_alert_uses_graphql_liquidity(self) -> None:
        vault = V2Vault(
            name="Example",
            address="0x" + "11" * 20,
            chain=Chain.MAINNET,
            asset_address="0x" + "22" * 20,
            asset_symbol="USDC",
            curator="",
            owner="",
            risk_level=1,
            total_assets_usd=100_000,
            liquidity_usd=500,
        )

        with patch("protocols.morpho.markets_v2.send_alert") as send:
            check_low_liquidity(vault)

        alert = send.call_args.args[0]
        self.assertIn("$500.00", alert.message)
        self.assertIn("0.5%", alert.message)

    def test_zero_asset_group_is_retained_for_collateral_risk_check(self) -> None:
        v1_vaults = [
            {
                "__typename": "Vault",
                "address": "0xe107cCdeb8e20E499545C813f98Cc90619b29859",
                "name": "Yearn OG WBTC",
                "chain": {"id": Chain.KATANA.chain_id},
                "asset": {
                    "address": "0x0913DA6Da4b42f538B445599b46Bb4622342Cf52",
                    "symbol": "vbWBTC",
                },
                "state": {"totalAssetsUsd": 0},
                "liquidity": {"usd": 0},
            }
        ]

        groups = get_yv_collateral_liquidity_by_asset(Chain.KATANA, v1_vaults, [])
        wbtc_group = groups["0x0913da6da4b42f538b445599b46bb4622342cf52"]

        self.assertEqual(wbtc_group["combined_total_assets"], 0)
        self.assertEqual(wbtc_group["combined_liquidity"], 0)


if __name__ == "__main__":
    unittest.main()
