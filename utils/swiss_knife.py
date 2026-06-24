"""Swiss Knife address-label client.

Wraps the public ``swiss-knife.xyz/api/labels/{address}`` endpoint which
returns a curated label array for well-known addresses (e.g. USDC →
``["Circle: USDC Token", "circle", "stablecoin"]``). High precision, low
recall — falls back to nothing for obscure protocol contracts. Used to
enrich the LLM prompt so the model can say "Circle: USDC Token" instead
of a bare address.
"""

import os
import threading

from utils.disk_cache import DiskCache
from utils.http_client import fetch_json
from utils.logger import get_logger

logger = get_logger("utils.swiss_knife")

_API_URL = "https://swiss-knife.xyz/api/labels"
_REQUEST_TIMEOUT_S = 5

# Per-process cache: (chain_id, address_lower) -> labels (possibly empty).
# Backed by the on-disk cache below so labels survive across cron runs.
_label_cache: dict[tuple[int, str], list[str]] = {}
_label_cache_hits = 0
_label_cache_misses = 0
_label_cache_lock = threading.RLock()
_label_key_locks: dict[tuple[int, str], threading.Lock] = {}

# On-disk layer keyed by "chain_id-address". Labels are tiny and effectively stable, so a
# found label never expires; an empty result (no curated label) gets the short negative TTL
# so an address that later gains a label is picked up. Entry-count bounded (no byte cap
# needed — each entry is well under a KB). Tunable via env.
_label_disk_cache = DiskCache(
    namespace="label-cache",
    max_entries=int(os.getenv("LABEL_CACHE_MAX_ENTRIES", "50000")),
)


def _disk_key(chain_id: int, address: str) -> str:
    return f"{chain_id}-{address.lower()}"


def _lock_for_key(cache_key: tuple[int, str]) -> threading.Lock:
    with _label_cache_lock:
        return _label_key_locks.setdefault(cache_key, threading.Lock())


def _record_cache_event(source: str, hit: bool, cache_key: tuple[int, str]) -> None:
    global _label_cache_hits, _label_cache_misses
    with _label_cache_lock:
        if hit:
            _label_cache_hits += 1
        else:
            _label_cache_misses += 1
        hits = _label_cache_hits
        misses = _label_cache_misses
    logger.debug(
        "label cache %s %s for %s:%s (hits=%s misses=%s)",
        source,
        "hit" if hit else "miss",
        cache_key[0],
        cache_key[1],
        hits,
        misses,
    )


def fetch_swiss_knife_labels(address: str, chain_id: int) -> list[str]:
    """Return Swiss Knife labels for ``address`` on ``chain_id`` (empty on miss).

    Treats every failure mode — missing address, HTTP error, malformed
    response — as "no labels available" so callers can layer this safely
    on top of other label sources without worrying about exceptions.
    """
    if not address or not address.startswith("0x") or len(address) != 42:
        return []

    cache_key = (chain_id, address.lower())
    with _label_cache_lock:
        cached = _label_cache.get(cache_key)
        if cached is not None:
            _record_cache_event("memory", True, cache_key)
            return cached

    with _lock_for_key(cache_key):
        with _label_cache_lock:
            cached = _label_cache.get(cache_key)
            if cached is not None:
                _record_cache_event("memory", True, cache_key)
                return cached

        disk_key = _disk_key(chain_id, address)
        disk_val = _label_disk_cache.get(disk_key)
        if isinstance(disk_val, list):
            _record_cache_event("disk", True, cache_key)
            cached_labels = [s for s in disk_val if isinstance(s, str) and s]
            with _label_cache_lock:
                _label_cache[cache_key] = cached_labels
            return cached_labels
        _record_cache_event("disk", False, cache_key)

        url = f"{_API_URL}/{address}"
        data = fetch_json(url, params={"chainId": chain_id}, timeout=_REQUEST_TIMEOUT_S)

        # Swiss Knife returns a JSON array directly. fetch_json's type hint is
        # `dict | None` but it returns whatever `resp.json()` parses to. A non-None
        # body is a real 200 response — either a label array or a `{"error": ...}`
        # dict for unknown addresses, both of which we persist (the dict as an empty
        # negative). `None` means an HTTP/network error, which we leave unpersisted so
        # a transient blip is not cached as "no labels".
        got_response = data is not None
        labels: list[str] = []
        if isinstance(data, list):
            labels = [s for s in data if isinstance(s, str) and s]

        with _label_cache_lock:
            _label_cache[cache_key] = labels
        if got_response:
            if labels:
                _label_disk_cache.set_positive(disk_key, labels)
            else:
                _label_disk_cache.set_negative(disk_key, [])
        return labels


def pick_display_name(labels: list[str]) -> str:
    """Pick a human display name from a Swiss Knife label array.

    Their API returns ``[name, *tags]`` for well-known addresses — the head
    looks like ``"Circle: USDC Token"`` and tags are short lowercase words
    like ``"stablecoin"``. We only want the head, and only when it actually
    looks like a name: must contain a separator (space, colon, dot for ENS)
    or an uppercase letter. Bare tags like ``"stablecoin"`` are not useful
    as a label since they don't identify the address.
    """
    if not labels:
        return ""
    head = labels[0]
    if " " in head or ":" in head or "." in head or any(c.isupper() for c in head):
        return head
    return ""


def reset_cache() -> None:
    """Reset the in-memory label cache. Useful for tests."""
    global _label_cache_hits, _label_cache_misses
    with _label_cache_lock:
        _label_cache.clear()
        _label_key_locks.clear()
        _label_cache_hits = 0
        _label_cache_misses = 0
