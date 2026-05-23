"""Fetch ERC20 token metadata (symbol, decimals) for LLM prompt enrichment.

Without this, the LLM sees raw amounts like ``transfer(0xUSDC, 1000000000)``
and can't tell if that's $1 (6 decimals) or $1e-12 (18 decimals). Looking
up ``symbol()`` and ``decimals()`` once per unique address lets us annotate
the prompt with ``(USDC, 6 dec)`` so the model can compute concrete values.

This is best-effort: any non-ERC20 address fails the eth_call cleanly and
returns ``None``, treated as "no metadata available".
"""

from dataclasses import dataclass

from eth_utils import to_checksum_address

from utils.abi import load_abi
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.erc20_metadata")

_ERC20_ABI = None  # loaded lazily on first call

# Per-process cache: (chain_id, address_lower) -> ERC20Metadata or None for miss.
_cache: dict[tuple[int, str], "ERC20Metadata | None"] = {}


@dataclass(frozen=True)
class ERC20Metadata:
    """Decoded ERC20 token metadata."""

    symbol: str
    decimals: int


def fetch_erc20_metadata(chain_id: int, address: str) -> ERC20Metadata | None:
    """Return token metadata for an address, or None if it isn't ERC20-compatible.

    Both ``symbol()`` and ``decimals()`` must succeed — partial responses are
    treated as a miss. The pair is fetched via batch_requests so it costs a
    single round-trip instead of two.
    """
    if not address or len(address) != 42 or not address.startswith("0x"):
        return None

    cache_key = (chain_id, address.lower())
    if cache_key in _cache:
        return _cache[cache_key]

    global _ERC20_ABI
    if _ERC20_ABI is None:
        _ERC20_ABI = load_abi("common-abi/ERC20.json")

    try:
        chain = Chain.from_chain_id(chain_id)
        client = ChainManager.get_client(chain)
        token = client.get_contract(to_checksum_address(address), _ERC20_ABI)
        with client.batch_requests() as batch:
            batch.add(token.functions.symbol())
            batch.add(token.functions.decimals())
            symbol, decimals = client.execute_batch(batch)
        meta = ERC20Metadata(symbol=str(symbol), decimals=int(decimals))
    except Exception as e:  # noqa: BLE001 - any eth_call failure -> not an ERC20
        logger.debug("ERC20 metadata fetch failed for %s on chain %s: %s", address, chain_id, e)
        _cache[cache_key] = None
        return None

    _cache[cache_key] = meta
    return meta


def reset_cache() -> None:
    """Reset the in-memory metadata cache. Useful for tests."""
    _cache.clear()
