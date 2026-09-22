"""DeFiLlama price utilities."""

from decimal import Decimal, getcontext

from utils.http_client import request_with_retry
from utils.logger import get_logger

getcontext().prec = 18

logger = get_logger("defillama")

CURRENT_PRICES_URL = "https://coins.llama.fi/prices/current"


def fetch_prices(token_keys: list[str]) -> dict[str, Decimal]:
    """Fetch current prices from DeFiLlama for the given token keys.

    Args:
        token_keys: List of DeFiLlama keys ("chain:token_address").

    Returns:
        Mapping of token key to price as Decimal. Missing tokens are omitted.

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
    return {key: Decimal(str(data["price"])) for key, data in coins.items() if "price" in data}
