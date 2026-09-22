"""Fetch compiler-derived storage layouts from Sourcify.

Solidity's ``storageLayout`` output is the only correct description of where a
contract's variables live: it already accounts for C3 linearization, packing,
byte offsets, structs and fixed arrays spanning slots, and user-defined value
types. Declaration order in source text does not.

Sourcify serves that layout for contracts it has verified, together with the
deployed-bytecode match it performed — which is exactly the check we would
otherwise have to reimplement around a downloaded solc. So the monitoring cron
never compiles anything: no compiler-version management, library linking,
IR-codegen drift, integrity verification or sandboxing in the hot path.

Coverage is partial (Sourcify may not know a contract Etherscan has verified),
so every caller must handle ``None``. A missing contract is reported as HTTP 200
with ``match: null``, so the status code alone proves nothing.
"""

import os
from dataclasses import dataclass

from utils.disk_cache import MISS, DiskCache
from utils.http_client import fetch_json
from utils.logger import get_logger

logger = get_logger("utils.sourcify_layout")

SOURCIFY_V2_API_URL = "https://sourcify.dev/server/v2/contract"

# Layouts for a verified address are immutable, so positive entries never expire.
# Misses get the short negative TTL: Sourcify coverage grows over time, and a
# contract unknown today is often verified a day later.
_layout_cache = DiskCache(
    namespace="sourcify-layout-v1",
    max_entries=int(os.getenv("SOURCIFY_LAYOUT_CACHE_MAX_ENTRIES", "5000")),
    max_bytes=int(os.getenv("SOURCIFY_LAYOUT_CACHE_MAX_BYTES", str(64 * 1024 * 1024))),
)


@dataclass(frozen=True)
class StorageLayout:
    """A verified contract's compiler storage layout."""

    address: str
    match: str  # Sourcify's match quality, e.g. "match" / "exact_match"
    storage: list[dict]  # entries with slot / offset / label / type
    types: dict[str, dict]  # type id -> type description

    @property
    def is_usable(self) -> bool:
        """A layout with no entries tells us nothing — treat it as no coverage."""
        return bool(self.storage and self.types)


def fetch_storage_layout(chain_id: int, address: str) -> StorageLayout | None:
    """Return the compiler storage layout for ``address``, or None.

    None covers every "we don't know" case — unknown to Sourcify, verified but
    unmatched, malformed payload, or a network error. Callers must map that to
    an UNKNOWN verdict rather than assuming compatibility.
    """
    key = f"{chain_id}-{address.lower()}"
    cached = _layout_cache.get(key)
    if cached is not MISS:
        return _from_payload(address, cached) if cached else None

    url = f"{SOURCIFY_V2_API_URL}/{chain_id}/{address}"
    payload = fetch_json(url, params={"fields": "storageLayout"})
    if payload is None:
        # Transient failure or 404 — don't persist, so a later run retries.
        logger.debug("no Sourcify layout response for %s on chain %s", address, chain_id)
        return None

    layout = _from_payload(address, payload)
    if layout is None:
        _layout_cache.set_negative(key)
        return None

    _layout_cache.set_positive(key, payload)
    return layout


def _from_payload(address: str, payload: object) -> StorageLayout | None:
    """Validate a Sourcify response into a usable layout, or None."""
    if not isinstance(payload, dict):
        return None

    match = payload.get("match")
    if not isinstance(match, str) or not match:
        # `match: null` — Sourcify knows the address but has no verified match.
        return None

    raw_layout = payload.get("storageLayout")
    if not isinstance(raw_layout, dict):
        return None
    storage = raw_layout.get("storage")
    types = raw_layout.get("types")
    if not isinstance(storage, list) or not isinstance(types, dict):
        return None

    layout = StorageLayout(
        address=address,
        match=match,
        storage=[e for e in storage if isinstance(e, dict)],
        types={str(k): v for k, v in types.items() if isinstance(v, dict)},
    )
    return layout if layout.is_usable else None
