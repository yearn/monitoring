"""Resolve a contract address to a human-readable label.

Single entry point for "what should I call this address?" The actual lookups
fan out across several backends in priority order — first non-empty result
wins. Backends are registered as a list so adding a new source (ENS reverse,
Dune labels, internal yearn registry, …) is just appending one callable.

Each backend is a callable ``(chain_id, address) -> str`` that returns
either a usable label or ``""``. Exceptions are caught here so a broken
backend can't take down the chain.
"""

from typing import Callable

from utils.logging import get_logger

logger = get_logger("utils.address_resolver")

Backend = Callable[[int, str], str]


def _safe_utility_backend(chain_id: int, address: str) -> str:
    """Canonical Safe utilities (MultiSendCallOnly, SignMessageLib, …). No IO."""
    from protocols.safe.multisend import safe_utility_label

    return safe_utility_label(address)


def _swiss_knife_backend(chain_id: int, address: str) -> str:
    """Curated labels for well-known dApps from swiss-knife.xyz."""
    from utils.swiss_knife import fetch_swiss_knife_labels, pick_display_name

    return pick_display_name(fetch_swiss_knife_labels(address, chain_id))


def _etherscan_backend(chain_id: int, address: str) -> str:
    """Verified-contract ContractName, with EIP-1967 impl follow for generic proxies."""
    from utils.source_context import (
        _GENERIC_PROXY_NAMES,
        fetch_source,
    )

    fetched = fetch_source(chain_id, address)
    if not fetched:
        return ""

    name = fetched[0]
    if name and name not in _GENERIC_PROXY_NAMES:
        return name

    from utils.proxy import get_current_implementation

    impl = get_current_implementation(address, chain_id)
    if not impl or impl.lower() == address.lower():
        return name

    impl_fetched = fetch_source(chain_id, impl)
    if impl_fetched and impl_fetched[0]:
        return impl_fetched[0]
    return name


# Resolution order, by backend name. Earlier wins when it returns a non-empty
# label. Stored as names rather than function references so monkey-patching the
# functions at test time (e.g. via `@patch("utils.address_resolver._swiss_knife_backend")`)
# actually swaps what the chain calls. Also lets callers `register_backend` a
# new function attached to this module and have it resolve correctly.
_BACKEND_NAMES: list[str] = [
    "_safe_utility_backend",
    "_swiss_knife_backend",
    "_etherscan_backend",
]


def resolve_address_label(chain_id: int, address: str) -> str:
    """Best-effort human label for a contract address.

    Tries each backend in order, returns the first non-empty result. A
    failing backend (raised exception, network error) is skipped quietly
    so a transient outage in one source doesn't break the others.
    Returns ``""`` for EOAs, unverified contracts, or all-misses.
    """
    if not address:
        return ""

    module_globals = globals()
    for name in _BACKEND_NAMES:
        backend = module_globals.get(name)
        if backend is None:
            continue
        try:
            label = backend(chain_id, address)
        except Exception as e:  # noqa: BLE001 - best-effort resolution
            logger.info("Address-label backend %s failed for %s: %s", name, address, e)
            continue
        if label:
            return label
    return ""


def register_backend(backend: Backend, position: int | None = None) -> None:
    """Add a new resolution backend. ``position`` defaults to end-of-list.

    The callable is attached to this module under its ``__name__`` so the
    name-based lookup in :func:`resolve_address_label` finds it. Lower
    position = higher priority.
    """
    name = backend.__name__
    globals()[name] = backend
    if name in _BACKEND_NAMES:
        return
    if position is None:
        _BACKEND_NAMES.append(name)
    else:
        _BACKEND_NAMES.insert(position, name)
