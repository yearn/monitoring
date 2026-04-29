"""Shared helpers used by both v1 and v2 Morpho monitors."""

from typing import Any, Dict, List

from utils.chains import Chain
from utils.http import request_with_retry
from utils.logging import get_logger

API_URL = "https://api.morpho.org/graphql"
MORPHO_URL = "https://app.morpho.org"
COMPOUND_URL = "https://compound.blue"

logger = get_logger("morpho.shared")


def get_chain_name(chain: Chain) -> str:
    """Return the chain segment used in Morpho frontend URLs."""
    if chain == Chain.MAINNET:
        return "ethereum"
    return chain.name.lower()


def get_market_url(market_id: str, chain: Chain) -> str:
    """Build the Morpho UI URL for a market by its uniqueKey."""
    if chain == Chain.POLYGON:
        return f"{COMPOUND_URL}/borrow/{market_id}"
    return f"{MORPHO_URL}/{get_chain_name(chain)}/market/{market_id}"


def get_vault_url(vault_address: str, chain: Chain) -> str:
    """Build the Morpho UI URL for a vault by address."""
    if chain == Chain.POLYGON:
        return f"{COMPOUND_URL}/{vault_address}"
    return f"{MORPHO_URL}/{get_chain_name(chain)}/vault/{vault_address}"


def fetch_market_name(market_id: str, chain: Chain) -> str:
    """Fetch a human-readable name like 'WBTC/USDC (86.00%)' for a market_id.

    Falls back to the raw market_id on error so alerts always render.
    """
    query = """
    query GetMarket($uniqueKey: String!, $chainId: Int!) {
        marketByUniqueKey(uniqueKey: $uniqueKey, chainId: $chainId) {
            lltv
            loanAsset { symbol, decimals }
            collateralAsset { symbol }
        }
    }
    """
    try:
        response = request_with_retry(
            "post",
            API_URL,
            json={"query": query, "variables": {"uniqueKey": market_id, "chainId": chain.chain_id}},
        )
        market = response.json()["data"]["marketByUniqueKey"]
        collateral_symbol = market["collateralAsset"]["symbol"] if market.get("collateralAsset") else "idle"
        loan_symbol = market["loanAsset"]["symbol"]
        lltv_pct = int(market["lltv"]) / 1e18 * 100
        return f"{collateral_symbol}/{loan_symbol} ({lltv_pct:.2f}%)"
    except Exception as e:
        logger.warning("Failed to fetch market name for %s: %s", market_id, e)
        return market_id


def fetch_market_metrics(market_ids: List[str], chain: Chain) -> Dict[str, Dict[str, Any]]:
    """Fetch state + bad debt for a batch of market uniqueKeys keyed by id.

    Returns a dict keyed by lowercase market_id with shape::

        {
            "uniqueKey": str,
            "loanAsset": {"symbol": str, "address": str},
            "collateralAsset": {"symbol": str, "address": str} | None,
            "state": {"utilization": float, "borrowAssetsUsd": float, "supplyAssetsUsd": float},
            "badDebt": {"underlying": int, "usd": float},
        }
    """
    if not market_ids:
        return {}

    query = """
    query GetMarkets($keys: [String!]!, $chainId: Int!) {
        markets(where: { uniqueKey_in: $keys, chainId_in: [$chainId] }) {
            items {
                uniqueKey
                loanAsset { address, symbol, decimals }
                collateralAsset { address, symbol }
                state {
                    utilization
                    borrowAssets
                    supplyAssets
                    borrowAssetsUsd
                    supplyAssetsUsd
                }
                badDebt { underlying, usd }
            }
        }
    }
    """
    keys = [mid.lower() for mid in market_ids]
    response = request_with_retry(
        "post", API_URL, json={"query": query, "variables": {"keys": keys, "chainId": chain.chain_id}}
    )
    payload = response.json()
    if "errors" in payload:
        logger.warning("GraphQL error fetching market metrics: %s", payload["errors"])
        return {}
    items = payload.get("data", {}).get("markets", {}).get("items", []) or []
    return {item["uniqueKey"].lower(): item for item in items}
