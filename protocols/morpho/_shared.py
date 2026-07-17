"""Shared helpers used by both v1 and v2 Morpho monitors."""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import requests

from utils.chains import Chain
from utils.http_client import request_with_retry
from utils.logger import get_logger

API_URL = "https://api.morpho.org/graphql"
MORPHO_URL = "https://app.morpho.org"
PROTOCOL = "morpho"

logger = get_logger("morpho.shared")


class MorphoMonitoringError(RuntimeError):
    """Raised when Morpho monitoring cannot produce a complete result."""


class MorphoV2MonitoringError(MorphoMonitoringError):
    """Raised when configured Morpho Vault V2 monitoring is incomplete."""


def execute_graphql(
    query: str,
    variables: dict[str, Any],
    context: str,
    *,
    error_type: type[MorphoMonitoringError] = MorphoMonitoringError,
) -> dict[str, Any]:
    """Execute a strict Morpho GraphQL request and return its data object."""
    try:
        response = request_with_retry("post", API_URL, json={"query": query, "variables": variables})
    except requests.RequestException as exc:
        raise error_type(f"Failed to fetch {context}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise error_type(f"Morpho returned invalid JSON while fetching {context}") from exc

    if payload.get("errors"):
        raise error_type(f"Morpho GraphQL errors fetching {context}: {payload['errors']}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise error_type(f"Morpho GraphQL returned no data while fetching {context}")
    return data


def require_configured_keys(
    expected: Iterable[str],
    found: Iterable[str],
    context: str,
    *,
    error_type: type[MorphoMonitoringError] = MorphoMonitoringError,
) -> None:
    """Raise when a GraphQL collection omits any configured key."""
    missing = sorted({key.lower() for key in expected} - {key.lower() for key in found})
    if missing:
        raise error_type(f"Morpho API omitted configured {context}: " + ", ".join(missing))


def get_chain_name(chain: Chain) -> str:
    """Return the chain segment used in Morpho frontend URLs."""
    if chain == Chain.MAINNET:
        return "ethereum"
    return str(chain.name).lower()


def get_market_url(market_id: str, chain: Chain) -> str:
    """Build the Morpho UI URL for a market by its marketId."""
    return f"{MORPHO_URL}/{get_chain_name(chain)}/market/{market_id}/"


def get_vault_url(vault_address: str, chain: Chain) -> str:
    """Build the Morpho UI URL for a vault by address."""
    return f"{MORPHO_URL}/{get_chain_name(chain)}/vault/{vault_address}"


def format_low_liquidity_message(
    vault_name: str,
    vault_url: str,
    chain: Chain,
    total_assets_usd: float,
    liquidity_usd: float,
    threshold: float,
    *,
    version_label: str = "",
) -> str:
    """Format the common V1/V2 low-liquidity alert body."""
    liquidity_ratio = liquidity_usd / total_assets_usd
    prefix = f"{version_label} " if version_label else ""
    return (
        f"⚠️ Low liquidity in {prefix}[{vault_name}]({vault_url}) on {chain.name}\n"
        f"💰 Liquidity: ${liquidity_usd:,.2f} ({liquidity_ratio:.1%} of ${total_assets_usd:,.2f})\n"
        f"📊 Min threshold: {threshold:.1%}\n"
    )


def fetch_market_metadata(market_id: str, chain: Chain) -> dict[str, Any] | None:
    """Fetch symbols and loan decimals for a Morpho market.

    Returns None on error so alert rendering can fall back to decoded calldata.
    """
    query = """
    query GetMarket($marketId: String!, $chainId: Int!) {
        marketById(marketId: $marketId, chainId: $chainId) {
            lltv
            loanAsset { symbol, decimals }
            collateralAsset { symbol }
        }
    }
    """
    try:
        data = execute_graphql(
            query,
            {"marketId": market_id, "chainId": chain.chain_id},
            f"market metadata for {market_id} on {chain.name}",
        )
        market = data["marketById"]
        collateral_symbol = market["collateralAsset"]["symbol"] if market.get("collateralAsset") else "idle"
        loan_asset = market["loanAsset"]
        return {
            "name": f"{collateral_symbol}/{loan_asset['symbol']}",
            "loan_symbol": loan_asset["symbol"],
            "loan_decimals": int(loan_asset["decimals"]),
            "lltv": int(market.get("lltv") or 0),
        }
    except Exception as e:
        logger.warning("Failed to fetch market metadata for %s: %s", market_id, e)
        return None


def fetch_market_name(market_id: str, chain: Chain) -> str:
    """Fetch a human-readable name like 'WBTC/USDC' for a market_id.

    Falls back to the raw market_id on error so alerts always render.
    """
    metadata = fetch_market_metadata(market_id, chain)
    return metadata["name"] if metadata else market_id


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

    market_id: str
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
        market_id=raw["marketId"],
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


def fetch_market_metrics(market_ids: List[str], chain: Chain) -> Dict[str, MarketMetrics]:
    """Fetch state + bad debt for a batch of market IDs.

    Returns a dict keyed by lowercase market_id mapping to a ``MarketMetrics``
    dataclass. Raises if Morpho returns errors or omits a requested market.
    """
    if not market_ids:
        return {}

    query = """
    query GetMarkets($keys: [String!]!, $chainId: Int!) {
        markets(where: { uniqueKey_in: $keys, chainId_in: [$chainId] }) {
            items {
                marketId
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
    data = execute_graphql(
        query,
        {"keys": keys, "chainId": chain.chain_id},
        f"market metrics on {chain.name}",
        error_type=MorphoV2MonitoringError,
    )
    items = data.get("markets", {}).get("items", []) or []
    metrics = {item["marketId"].lower(): _parse_market_metrics(item) for item in items}
    missing_market_ids = sorted(set(keys) - set(metrics))
    if missing_market_ids:
        raise MorphoV2MonitoringError("Morpho API omitted configured market IDs: " + ", ".join(missing_market_ids))
    return metrics
