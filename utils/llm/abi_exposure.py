"""Check which functions a contract's verified ABI exposes, following EIP-1967 proxies.

Protocol context adapters identify contracts by the getters they expose rather
than by hard-coded addresses. Upgradeable contracts sit behind proxies whose own
ABI lists only the proxy's functions, so the implementation is consulted when
the proxy ABI comes up short.
"""

from functools import lru_cache

from utils.source_context import fetch_abi_entries


def _abi_function_names(entries: list[dict]) -> frozenset[str]:
    """Function names present in a verified ABI."""
    return frozenset(
        str(entry.get("name")) for entry in entries if entry.get("type") == "function" and entry.get("name")
    )


@lru_cache(maxsize=128)
def _own_function_names(chain_id: int, address: str) -> frozenset[str]:
    """Function names on the address's own verified ABI. No RPC — Etherscan is cached."""
    return _abi_function_names(fetch_abi_entries(chain_id, address) or [])


@lru_cache(maxsize=128)
def _implementation_function_names(chain_id: int, address: str) -> frozenset[str]:
    """Function names behind an EIP-1967 proxy, or empty when there is no proxy."""
    from utils.proxy import get_current_implementation

    implementation = get_current_implementation(address, chain_id)
    if not implementation or implementation.lower() == address.lower():
        return frozenset()
    return _abi_function_names(fetch_abi_entries(chain_id, implementation) or [])


def exposes(chain_id: int, address: str, wanted: set[str]) -> bool:
    """Whether a contract exposes every wanted function, following EIP-1967.

    Args:
        chain_id: Chain the contract lives on.
        address: Contract (or proxy) address.
        wanted: Function names that must all be present.

    Returns:
        True when the address's own ABI, or its implementation's, has them all.
    """
    if wanted.issubset(_own_function_names(chain_id, address)):
        return True
    return wanted.issubset(_implementation_function_names(chain_id, address))


def reset_cache() -> None:
    """Reset process caches for tests or long-running workers."""
    _own_function_names.cache_clear()
    _implementation_function_names.cache_clear()
