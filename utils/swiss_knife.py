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
import time

import requests

from utils.disk_cache import DiskCache
from utils.http_client import request_with_retry
from utils.logger import get_logger

logger = get_logger("utils.swiss_knife")

_API_URL = "https://swiss-knife.xyz/api/labels"
_REQUEST_TIMEOUT_S = 5
_API_BACKOFF_S = 300

# Per-process cache: (chain_id, address_lower) -> labels (possibly empty).
# Backed by the on-disk cache below so labels survive across cron runs.
_label_cache: dict[tuple[int, str], list[str]] = {}
_label_cache_hits = 0
_label_cache_misses = 0
_label_cache_lock = threading.RLock()
_label_key_locks: dict[tuple[int, str], threading.Lock] = {}
_api_backoff_until: dict[int, float] = {}

# On-disk layer keyed by "chain_id-address". Labels are tiny and effectively stable, so a
# found label never expires; an empty result (no curated label) gets the short negative TTL
# so an address that later gains a label is picked up. Entry-count bounded (no byte cap
# needed — each entry is well under a KB). Tunable via env.
_label_disk_cache = DiskCache(
    namespace="label-cache",
    max_entries=int(os.getenv("LABEL_CACHE_MAX_ENTRIES", "50000")),
)
_api_backoff_cache = DiskCache(namespace="label-api-backoff", max_entries=100)


def _api_in_backoff(chain_id: int) -> bool:
    """Skip unavailable label services briefly, including across cron runs."""
    now = time.time()
    with _label_cache_lock:
        if _api_backoff_until.get(chain_id, 0) > now:
            return True
    retry_after = _api_backoff_cache.get(str(chain_id))
    if isinstance(retry_after, (int, float)) and retry_after > now:
        with _label_cache_lock:
            _api_backoff_until[chain_id] = retry_after
        return True
    return False


def _back_off_api(chain_id: int) -> None:
    """Report an optional dependency outage once and permit a later recovery."""
    now = time.time()
    with _label_cache_lock:
        if _api_backoff_until.get(chain_id, 0) > now:
            return
        retry_after = now + _API_BACKOFF_S
        _api_backoff_until[chain_id] = retry_after
    _api_backoff_cache.set(str(chain_id), retry_after, ttl=_API_BACKOFF_S)
    logger.warning(
        "Swiss Knife labels unavailable on chain %s; skipping uncached lookups for %ss (other label sources remain active)",
        chain_id,
        _API_BACKOFF_S,
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

        if _api_in_backoff(chain_id):
            return []

        url = f"{_API_URL}/{address}"
        try:
            response = request_with_retry(
                "get", url, params={"chainId": chain_id}, timeout=_REQUEST_TIMEOUT_S, log_errors=False
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429 or (status is not None and status >= 500):
                _back_off_api(chain_id)
            else:
                # A 400 wrapping an upstream 404 may be specific to this address.
                # It neither proves a chain-wide outage nor a permanent label miss.
                logger.debug("Swiss Knife lookup failed for %s:%s: %s", chain_id, address, exc)
            return []
        except requests.ConnectionError, requests.Timeout:
            _back_off_api(chain_id)
            return []
        except requests.RequestException as exc:
            logger.debug("Swiss Knife lookup failed for %s:%s: %s", chain_id, address, exc)
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.debug("Swiss Knife returned invalid JSON for %s:%s: %s", chain_id, address, exc)
            return []
        if data is None:
            return []

        # Successful label arrays and empty responses retain their normal caches.
        # Failed requests above never become address-level negative entries.
        labels: list[str] = []
        if isinstance(data, list):
            labels = [s for s in data if isinstance(s, str) and s]

        with _label_cache_lock:
            _label_cache[cache_key] = labels
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
        _api_backoff_until.clear()
        _label_cache_hits = 0
        _label_cache_misses = 0
