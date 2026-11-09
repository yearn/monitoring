"""DeFiLlama price utilities."""

from dataclasses import dataclass
from decimal import Decimal, getcontext

from utils.http_client import request_with_retry
from utils.logger import get_logger

getcontext().prec = 18

logger = get_logger("defillama")

CURRENT_PRICES_URL = "https://coins.llama.fi/prices/current"


@dataclass(frozen=True)
class PricePoint:
    """One DeFiLlama price sample.

    Args:
        price: Token price in USD.
        timestamp: Unix time DeFiLlama recorded the price, or None when it was omitted.
    """

    price: Decimal
    timestamp: int | None


def fetch_prices(token_keys: list[str]) -> dict[str, Decimal]:
    """Fetch current prices from DeFiLlama for the given token keys.

    Args:
        token_keys: List of DeFiLlama keys ("chain:token_address").

    Returns:
        Mapping of token key to price as Decimal. Missing tokens are omitted.

    Raises:
        Exception: If the DeFiLlama API call fails.
    """
    return {key: point.price for key, point in fetch_price_points(token_keys).items()}


def fetch_price_points(token_keys: list[str]) -> dict[str, PricePoint]:
    """Fetch current prices from DeFiLlama along with when each was recorded.

    Args:
        token_keys: List of DeFiLlama keys ("chain:token_address").

    Returns:
        Mapping of token key to its PricePoint. Missing tokens are omitted.

    Raises:
        Exception: If the DeFiLlama API call fails.
    """
    if not token_keys:
        return {}

    logger.info("Fetching prices for %d tokens from DeFiLlama", len(token_keys))
    url = f"{CURRENT_PRICES_URL}/{','.join(token_keys)}"
    response = request_with_retry("get", url, headers={"Accept": "application/json"})
    result = response.json()
    coins = result.get("coins", {})
    return {
        key: PricePoint(price=Decimal(str(data["price"])), timestamp=_sample_timestamp(data))
        for key, data in coins.items()
        if "price" in data
    }


def _sample_timestamp(data: dict[str, object]) -> int | None:
    """Return a coin's sample time as an int, or None when absent or not numeric."""
    timestamp = data.get("timestamp")
    return int(timestamp) if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) else None


# DeFiLlama's chain slug for each chain id we monitor.
DEFILLAMA_CHAINS: dict[int, str] = {
    1: "ethereum",
    10: "optimism",
    137: "polygon",
    999: "hyperliquid",
    8453: "base",
    42161: "arbitrum",
    747474: "katana",
}


def defillama_key(chain_id: int, address: str) -> str | None:
    """DeFiLlama price key (``"chain:address"``) for a token, or None for an unmapped chain."""
    chain = DEFILLAMA_CHAINS.get(chain_id)
    return f"{chain}:{address}" if chain else None
