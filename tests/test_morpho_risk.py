"""Regression tests for the shared Morpho risk-1 vault policy."""

from typing import Any
from unittest.mock import patch

import pytest

from protocols.morpho._shared import Asset, BadDebt, MarketMetrics, MarketState
from protocols.morpho.config import VAULTS_V1_BY_CHAIN
from protocols.morpho.markets import check_allocation_and_risk
from protocols.morpho.markets_v2 import V2Vault, score_market_allocations
from protocols.morpho.risk import MARKETS_RISK_1, MARKETS_RISK_2, MARKETS_RISK_3
from utils.chains import Chain


def _market_metrics(market_id: str) -> MarketMetrics:
    """Return a funded market with no bad debt."""
    return MarketMetrics(
        market_id=market_id,
        loan_asset=Asset(address="0x" + "11" * 20, symbol="USDT"),
        collateral_asset=Asset(address="0x" + "22" * 20, symbol="COLLATERAL"),
        state=MarketState(
            utilization=0.5,
            borrow_assets=100,
            supply_assets=200,
            borrow_assets_usd=100,
            supply_assets_usd=200,
        ),
        bad_debt=BadDebt(underlying=0, usd=0),
    )


def _check_allocations(version: int, allocations: dict[str, float]) -> list[str]:
    """Run one vault monitor and capture its alerts without sending to Telegram."""
    config = next(vault for vault in VAULTS_V1_BY_CHAIN[Chain.MAINNET] if vault.name == "Yearn USDT")
    if version == 1:
        vault_data: dict[str, Any] = {
            "name": config.name,
            "address": config.address,
            "chain": {"id": Chain.MAINNET.chain_id},
            "state": {
                "totalAssetsUsd": 100_000,
                "allocation": [
                    {
                        "supplyCap": "100000",
                        "supplyAssetsUsd": supplied,
                        "market": {
                            "marketId": market_id,
                            "collateralAsset": {"symbol": "COLLATERAL"},
                            "loanAsset": {"symbol": "USDT"},
                        },
                    }
                    for market_id, supplied in allocations.items()
                ],
            },
        }
        with patch("protocols.morpho.markets.send_alert") as send:
            check_allocation_and_risk(vault_data)
    else:
        vault = V2Vault(
            name=config.name,
            address=config.address,
            chain=Chain.MAINNET,
            asset_address="0x" + "11" * 20,
            asset_symbol="USDT",
            risk_level=1,
            total_assets_usd=100_000,
            market_allocations_usd=allocations,
        )
        with patch("protocols.morpho.markets_v2.send_alert") as send:
            score_market_allocations(vault, {market_id: _market_metrics(market_id) for market_id in allocations})

    return [call.args[0].message.lower() for call in send.call_args_list]


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
@pytest.mark.parametrize(
    ("ratios", "allocation_alert", "risk_alert"),
    [
        pytest.param((0.84, 0.16, 0, 0), False, False, id="reported-risk-1.16"),
        pytest.param((0.70, 0.30, 0, 0), False, False, id="risk-2-at-limit"),
        pytest.param((0.69, 0.31, 0, 0), True, True, id="risk-2-over-limit"),
        pytest.param((0.60, 0.20, 0.20, 0), False, True, id="combined-risk-2-over-limit"),
        pytest.param((0.90, 0, 0, 0.10), False, False, id="risk-3-at-limit"),
        pytest.param((0.89, 0, 0, 0.11), True, False, id="risk-3-over-allocation-limit"),
        pytest.param((0.60, 0.30, 0, 0.10), False, True, id="combined-risk-tiers-over-limit"),
    ],
)
def test_risk_one_vault_alerts(
    version: int,
    ratios: tuple[float, float, float, float],
    allocation_alert: bool,
    risk_alert: bool,
) -> None:
    """Apply the same aggregate ceiling and per-market caps in both vault monitors."""
    market_ids = (
        MARKETS_RISK_1[Chain.MAINNET][0],
        MARKETS_RISK_2[Chain.MAINNET][0],
        MARKETS_RISK_2[Chain.MAINNET][1],
        MARKETS_RISK_3[Chain.MAINNET][0],
    )
    allocations = {market_id: ratio * 100_000 for market_id, ratio in zip(market_ids, ratios) if ratio}
    messages = _check_allocations(version, allocations)
    assert len(messages) == int(allocation_alert) + int(risk_alert)
    assert any("high allocation" in message for message in messages) == allocation_alert
    assert any("high risk" in message for message in messages) == risk_alert
    if risk_alert:
        assert "max: 1.30" in next(message for message in messages if "high risk" in message)
