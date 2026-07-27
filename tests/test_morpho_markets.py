"""Behavior tests for Morpho v1/v2 market and liquidity monitoring."""

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from protocols.morpho._shared import (
    Asset,
    BadDebt,
    MarketMetrics,
    MarketState,
    MorphoMonitoringError,
    MorphoV2MonitoringError,
)
from protocols.morpho.config import VAULTS_V2_BY_CHAIN, get_collateral_vaults_by_asset
from protocols.morpho.markets import (
    YV_COLLATERAL_AT_RISK_POINTS,
    YV_COLLATERAL_STABLE_PRICE_SHOCK,
    calculate_combined_metrics,
    collect_yv_collateral_markets,
    fetch_configured_vaults,
    get_markets_collateral_at_risk_usd,
    get_yv_collateral_liquidity_by_asset,
)
from protocols.morpho.markets_v2 import (
    V2Vault,
    _parse_market_allocations,
    check_low_liquidity,
    discover_v2_vaults_by_chain,
    main,
    score_market_allocations,
)
from utils.chains import Chain


def _sample_metrics(market_id: str, asset_address: str = "0x" + "22" * 20) -> MarketMetrics:
    return MarketMetrics(
        market_id=market_id,
        loan_asset=Asset(address=asset_address, symbol="USDC"),
        collateral_asset=Asset(address="0x" + "44" * 20, symbol="WETH"),
        state=MarketState(
            utilization=0.5,
            borrow_assets=100,
            supply_assets=100,
            borrow_assets_usd=100,
            supply_assets_usd=100,
        ),
        bad_debt=BadDebt(underlying=0, usd=0),
    )


class TestMorphoV2Configuration(unittest.TestCase):
    def test_katana_collateral_strategy_vaults_are_monitored(self) -> None:
        configured = {vault.address.lower() for vault in VAULTS_V2_BY_CHAIN[Chain.KATANA]}
        expected = {
            "0x4284d4f9f4d61ea57b8f0943547c7c19c5b9b249",
            "0xca44cbe1fb03691d43d2d93aa460e2fcb03878fe",
            "0xa2d38c8a3d810ebcf4c2075821c5ec8f976bb692",
            "0xac596ad9771a8d0d4df108ae0406e6f913aedceb",
            "0x5920a6fc553af799542eda628adfcc9ea52e141c",
            "0xbeeff2d5d126d4809195eea02b605423917bb6c6",
            "0xbeef042bad4472c3f7eb9a73070703788b5362d7",
        }

        self.assertTrue(expected <= configured)

        collateral_vaults = {
            vault.address.lower()
            for vaults in get_collateral_vaults_by_asset(Chain.KATANA, version=2).values()
            for vault in vaults
        }
        self.assertEqual(collateral_vaults, expected)

    def test_discovery_fails_if_api_omits_configured_vaults(self) -> None:
        response = MagicMock()
        response.json.return_value = {"data": {"vaultV2s": {"items": []}}}

        with (
            patch("protocols.morpho._shared.request_with_retry", return_value=response),
            self.assertRaisesRegex(MorphoV2MonitoringError, "omitted configured Vault V2"),
        ):
            discover_v2_vaults_by_chain()

    def test_discovery_rejects_non_market_adapters(self) -> None:
        item = {
            "adapters": {
                "items": [
                    {
                        "address": "0x" + "33" * 20,
                        "type": "MetaMorpho",
                        "assetsUsd": 100_000,
                    }
                ]
            }
        }

        with self.assertRaisesRegex(MorphoV2MonitoringError, "unsupported adapter type"):
            _parse_market_allocations(item, "Example", Chain.MAINNET)

    def test_parse_market_allocations_aggregates_morpho_market_v1_positions(self) -> None:
        market_a = "0x" + "aa" * 32
        market_b = "0x" + "bb" * 32
        item = {
            "adapters": {
                "items": [
                    {
                        "address": "0x" + "33" * 20,
                        "type": "MorphoMarketV1",
                        "positions": {
                            "items": [
                                {
                                    "market": {"marketId": market_a},
                                    "state": {"supplyAssetsUsd": 10_000},
                                },
                                {
                                    "market": {"marketId": market_a},
                                    "state": {"supplyAssetsUsd": 5_000},
                                },
                                {
                                    "market": {"marketId": market_b},
                                    "state": {"supplyAssetsUsd": 0},
                                },
                            ]
                        },
                    }
                ]
            }
        }

        self.assertEqual(
            _parse_market_allocations(item, "Example", Chain.MAINNET),
            {market_a: 15_000.0},
        )

    def test_market_scoring_consolidates_all_v2_risk_alerts(self) -> None:
        market_id = "0x" + "ab" * 32
        vault = V2Vault(
            name="Example",
            address="0x" + "11" * 20,
            chain=Chain.MAINNET,
            asset_address="0x" + "22" * 20,
            asset_symbol="USDC",
            risk_level=1,
            total_assets_usd=100,
            market_allocations_usd={market_id: 80},
        )
        metrics = {
            market_id: MarketMetrics(
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
        }

        with patch("protocols.morpho.markets_v2.send_alert") as send:
            score_market_allocations(vault, metrics)

        messages = [call.args[0].message for call in send.call_args_list]
        self.assertEqual(len(messages), 3)
        self.assertTrue(any("V2 high allocation" in message for message in messages))
        self.assertTrue(any("V2 bad debt" in message for message in messages))
        self.assertTrue(any("V2 high risk" in message for message in messages))

    def test_main_batches_metrics_per_chain_and_continues_after_failure(self) -> None:
        market_a = "0x" + "aa" * 32
        market_b = "0x" + "bb" * 32
        market_c = "0x" + "cc" * 32
        vault_mainnet_a = V2Vault(
            name="Mainnet A",
            address="0x" + "11" * 20,
            chain=Chain.MAINNET,
            asset_address="0x" + "22" * 20,
            asset_symbol="USDC",
            risk_level=1,
            total_assets_usd=100_000,
            market_allocations_usd={market_a: 40_000, market_b: 60_000},
        )
        vault_mainnet_b = V2Vault(
            name="Mainnet B",
            address="0x" + "33" * 20,
            chain=Chain.MAINNET,
            asset_address="0x" + "22" * 20,
            asset_symbol="USDC",
            risk_level=1,
            total_assets_usd=50_000,
            market_allocations_usd={market_b: 50_000},
        )
        vault_base = V2Vault(
            name="Base Vault",
            address="0x" + "55" * 20,
            chain=Chain.BASE,
            asset_address="0x" + "66" * 20,
            asset_symbol="USDC",
            risk_level=1,
            total_assets_usd=100_000,
            market_allocations_usd={market_c: 100_000},
        )
        base_metrics = {market_c: _sample_metrics(market_c, vault_base.asset_address)}

        def fetch_side_effect(market_ids: list[str], chain: Chain) -> dict[str, MarketMetrics]:
            if chain == Chain.MAINNET:
                self.assertEqual(market_ids, sorted({market_a, market_b}))
                raise MorphoV2MonitoringError("mainnet metrics unavailable")
            self.assertEqual(chain, Chain.BASE)
            self.assertEqual(market_ids, [market_c])
            return base_metrics

        with (
            patch(
                "protocols.morpho.markets_v2.discover_v2_vaults_by_chain",
                return_value={
                    Chain.MAINNET: [vault_mainnet_a, vault_mainnet_b],
                    Chain.BASE: [vault_base],
                },
            ),
            patch("protocols.morpho.markets_v2.fetch_market_metrics", side_effect=fetch_side_effect) as fetch,
            patch("protocols.morpho.markets_v2.analyze_v2_vault") as analyze,
            self.assertRaisesRegex(MorphoV2MonitoringError, "markets on MAINNET"),
        ):
            main()

        self.assertEqual(fetch.call_count, 2)
        analyze.assert_called_once_with(vault_base, base_metrics)


class TestMorphoCollateralLiquidity(unittest.TestCase):
    def test_collateral_curve_can_resolve_stable_price_shock(self) -> None:
        self.assertGreaterEqual(YV_COLLATERAL_AT_RISK_POINTS, round(1 / YV_COLLATERAL_STABLE_PRICE_SHOCK))

    def test_collateral_risk_api_failure_is_not_silently_skipped(self) -> None:
        response = MagicMock()
        response.json.return_value = {"errors": [{"message": "unavailable"}]}

        with (
            patch("protocols.morpho._shared.request_with_retry", return_value=response),
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
            patch("protocols.morpho._shared.request_with_retry", return_value=response),
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
