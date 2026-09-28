"""Checked-in ABIs and verified-ABI probes shared by the 3Jane context adapters.

3Jane's contracts sit behind transparent and beacon proxies, so the question
"is this target a ProtocolConfig / RewardsDistributor / LCCVault?" is answered
from the implementation's verified ABI, and the reads themselves go through
small checked-in ABIs rather than whatever Etherscan returns at alert time.
"""

from functools import lru_cache

from utils.abi import load_abi
from utils.source_context import fetch_abi_entries

ABI_DIR = "protocols/3jane/abi"


@lru_cache(maxsize=None)
def threejane_abi(name: str) -> list[dict]:
    """Load a checked-in 3Jane ABI once per process.

    Lazily, not at import: this module sits in the explainer's import chain, and
    a missing or unreadable file should degrade one protocol's context rather
    than break every AI alert.
    """
    entries: list[dict] = load_abi(f"{ABI_DIR}/{name}.json")
    return entries


def _abi_function_names(entries: list[dict]) -> frozenset[str]:
    """Function names present in a verified ABI."""
    return frozenset(
        str(entry.get("name")) for entry in entries if entry.get("type") == "function" and entry.get("name")
    )


@lru_cache(maxsize=64)
def _own_function_names(chain_id: int, address: str) -> frozenset[str]:
    """Function names on the address's own verified ABI. No RPC — Etherscan is cached."""
    return _abi_function_names(fetch_abi_entries(chain_id, address) or [])


@lru_cache(maxsize=64)
def _implementation_function_names(chain_id: int, address: str) -> frozenset[str]:
    """Function names behind a proxy, or empty when there is no proxy.

    Cached because one alert probes the same target for several shapes — the
    slot read is identical every time, and one governance transaction cannot
    change the implementation it is still only scheduled against.
    """
    from utils.proxy import get_current_implementation

    implementation = get_current_implementation(address, chain_id)
    if not implementation or implementation.lower() == address.lower():
        return frozenset()
    return _abi_function_names(fetch_abi_entries(chain_id, implementation) or [])


def exposes(chain_id: int, address: str, wanted: set[str]) -> bool:
    """Whether a contract exposes every wanted getter, following the proxy.

    3Jane's ProtocolConfig and MorphoCredit sit behind transparent proxies and
    each LCCVault behind a beacon proxy, so the address's own verified ABI lists
    the proxy's functions, not `config` or the distributor getters. The
    implementation is only read when the proxy ABI comes up short, and then only
    once per address.
    """
    if wanted.issubset(_own_function_names(chain_id, address)):
        return True
    return wanted.issubset(_implementation_function_names(chain_id, address))


def reset_cache() -> None:
    """Reset process caches for tests or long-running workers."""
    threejane_abi.cache_clear()
    _own_function_names.cache_clear()
    _implementation_function_names.cache_clear()
