"""DeFiLlama price utilities."""

from decimal import Decimal, getcontext

from defillama_sdk import DefiLlama

from utils.logging import get_logger

getcontext().prec = 18

logger = get_logger("defillama")

_dl_client = DefiLlama()


def fetch_prices(token_keys: list[str]) -> dict[str, Decimal]:
    """Fetch current prices from DeFiLlama for the given token keys.

    Args:
        token_keys: List of DeFiLlama keys ("chain:token_address").

    Returns:
        Mapping of token key to price as Decimal. Missing tokens are omitted.

    Raises:
        Exception: If the DeFiLlama API call fails.
    """
    logger.info("Fetching prices for %d tokens from DeFiLlama", len(token_keys))
    result = _dl_client.prices.getCurrentPrices(token_keys)
    coins = result.get("coins", {})
    return {key: Decimal(str(data["price"])) for key, data in coins.items() if "price" in data}
