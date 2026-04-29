"""Shared helpers used by both v1 and v2 Morpho monitors."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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


@dataclass(frozen=True)
class Asset:
    """Token metadata as returned by Morpho GraphQL."""

    address: str
    symbol: str
    decimals: Optional[int] = None


@dataclass(frozen=True)
class MarketState:
    """Per-market borrow/supply balances at the time of query."""

    utilization: float
    borrow_assets: int
    supply_assets: int
    borrow_assets_usd: float
    supply_assets_usd: float


@dataclass(frozen=True)
class BadDebt:
    """Bad debt accrued on a market."""

    underlying: int
    usd: float


@dataclass(frozen=True)
class MarketMetrics:
    """State + bad debt for a single Morpho Blue market."""

    unique_key: str
    loan_asset: Asset
    collateral_asset: Optional[Asset]
    state: MarketState
    bad_debt: BadDebt


def _parse_asset(raw: Optional[Dict[str, Any]]) -> Optional[Asset]:
    if not raw:
        return None
    return Asset(
        address=raw.get("address", ""),
        symbol=raw.get("symbol", ""),
        decimals=raw.get("decimals"),
    )


def _parse_market_metrics(raw: Dict[str, Any]) -> MarketMetrics:
    state_raw = raw.get("state") or {}
    bad_debt_raw = raw.get("badDebt") or {}
    loan_asset = _parse_asset(raw.get("loanAsset")) or Asset(address="", symbol="")
    return MarketMetrics(
        unique_key=raw["uniqueKey"],
        loan_asset=loan_asset,
        collateral_asset=_parse_asset(raw.get("collateralAsset")),
        state=MarketState(
            utilization=float(state_raw.get("utilization") or 0),
            borrow_assets=int(state_raw.get("borrowAssets") or 0),
            supply_assets=int(state_raw.get("supplyAssets") or 0),
            borrow_assets_usd=float(state_raw.get("borrowAssetsUsd") or 0),
            supply_assets_usd=float(state_raw.get("supplyAssetsUsd") or 0),
        ),
        bad_debt=BadDebt(
            underlying=int(bad_debt_raw.get("underlying") or 0),
            usd=float(bad_debt_raw.get("usd") or 0),
        ),
    )


def normalize_vault_name(name: str) -> str:
    """Normalize vault names for cross-version matching (case- and whitespace-insensitive)."""
    return " ".join(name.lower().split())


def build_v1_name_index(
    vaults_by_chain: Dict[Chain, List[List[Any]]],
) -> Dict[Chain, Dict[str, Tuple[int, str]]]:
    """Build ``{chain: {normalized_name: (risk_level, v1_address)}}`` from the v1 list.

    Used by both markets_v2 and governance_v2 to match v2 vaults discovered via
    GraphQL against the v1 monitor's hardcoded list, inheriting the v1 risk
    tier.
    """
    index: Dict[Chain, Dict[str, Tuple[int, str]]] = {}
    for chain, vaults in vaults_by_chain.items():
        index[chain] = {}
        for entry in vaults:
            index[chain][normalize_vault_name(str(entry[0]))] = (int(str(entry[2])), str(entry[1]))
    return index


def fetch_market_metrics(market_ids: List[str], chain: Chain) -> Dict[str, MarketMetrics]:
    """Fetch state + bad debt for a batch of market uniqueKeys.

    Returns a dict keyed by lowercase market_id mapping to a ``MarketMetrics``
    dataclass. Empty dict on GraphQL error or empty input.
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
    return {item["uniqueKey"].lower(): _parse_market_metrics(item) for item in items}
