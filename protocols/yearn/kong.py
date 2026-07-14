"""Kong GraphQL helpers for Yearn vault metadata."""

from typing import Any, Dict, List

import requests

from utils.chains import Chain

KONG_GQL_URL = "https://kong.yearn.fi/api/gql"
KONG_VAULTS_QUERY = """
query YearnVaults($chainId: Int) {
  vaults(chainId: $chainId, v3: true, yearn: true) {
    address
    symbol
    decimals
    strategies
    get_default_queue
    meta {
      isRetired
    }
  }
}
"""

STRATEGY_SOURCE_ALL = "strategies"
STRATEGY_SOURCE_DEFAULT_QUEUE = "default_queue"
STRATEGY_SOURCES = {STRATEGY_SOURCE_ALL, STRATEGY_SOURCE_DEFAULT_QUEUE}


class KongRequestError(requests.RequestException):
    """Raised when Kong returns an invalid GraphQL response."""


def _post_graphql(query: str, variables: Dict[str, object]) -> Dict[str, Any]:
    """Execute a Kong GraphQL query and return the response data."""
    response = requests.post(
        KONG_GQL_URL,
        json={"query": query, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as e:
        raise KongRequestError("Kong returned a non-JSON response") from e

    errors = payload.get("errors")
    if errors:
        raise KongRequestError(f"Kong GraphQL errors: {errors}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise KongRequestError("Kong response missing data object")

    return data


def _lower_addresses(addresses: object) -> List[str]:
    """Normalize a GraphQL address list to lowercase strings."""
    if not isinstance(addresses, list):
        return []
    return [addr.lower() for addr in addresses if isinstance(addr, str)]


def _strategy_objects(addresses: List[str]) -> List[Dict[str, str]]:
    """Return strategy objects for existing monitor callers."""
    return [{"address": address} for address in addresses]


def _parse_decimals(raw_decimals: object) -> int | None:
    """Parse Kong BigInt decimals into an int when present."""
    if raw_decimals is None:
        return None
    try:
        return int(str(raw_decimals))
    except ValueError:
        return None


def _is_retired(vault: Dict[str, Any]) -> bool:
    """Return whether Kong metadata marks the vault retired."""
    meta = vault.get("meta")
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("isRetired"))


def _strategy_field(strategy_source: str) -> str:
    """Return the Kong field backing the requested strategy source."""
    if strategy_source == STRATEGY_SOURCE_ALL:
        return "strategies"
    if strategy_source == STRATEGY_SOURCE_DEFAULT_QUEUE:
        return "get_default_queue"
    raise ValueError(f"Unknown strategy_source: {strategy_source}")


def fetch_kong_vaults(
    chain: Chain,
    *,
    strategy_source: str = STRATEGY_SOURCE_ALL,
) -> List[Dict[str, object]]:
    """Fetch active Yearn v3 vault metadata from Kong.

    Args:
        chain: Chain to fetch.
        strategy_source: ``strategies`` for all known strategies, or
            ``default_queue`` for only ``get_default_queue`` strategies.

    Returns:
        Vault dicts with ``address``, ``symbol``, ``decimals``, ``strategies``,
        and ``known_strategies`` keys.
    """
    strategy_field = _strategy_field(strategy_source)
    data = _post_graphql(KONG_VAULTS_QUERY, {"chainId": chain.chain_id})
    vaults = data.get("vaults")
    if not isinstance(vaults, list):
        raise KongRequestError("Kong response missing vaults list")

    result: List[Dict[str, object]] = []
    for vault in vaults:
        if not isinstance(vault, dict) or _is_retired(vault):
            continue

        address = vault.get("address")
        if not isinstance(address, str):
            continue

        strategy_addresses = _lower_addresses(vault.get(strategy_field))
        result.append(
            {
                "address": address.lower(),
                "symbol": vault.get("symbol") or "UNKNOWN",
                "decimals": _parse_decimals(vault.get("decimals")),
                "strategies": _strategy_objects(strategy_addresses),
                "known_strategies": strategy_addresses,
            }
        )

    return result
