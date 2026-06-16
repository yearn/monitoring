"""Swiss Knife address-label client.

Wraps the public ``swiss-knife.xyz/api/labels/{address}`` endpoint which
returns a curated label array for well-known addresses (e.g. USDC →
``["Circle: USDC Token", "circle", "stablecoin"]``). High precision, low
recall — falls back to nothing for obscure protocol contracts. Used to
enrich the LLM prompt so the model can say "Circle: USDC Token" instead
of a bare address.
"""

from utils.http_client import fetch_json
from utils.logger import get_logger

logger = get_logger("utils.swiss_knife")

_API_URL = "https://swiss-knife.xyz/api/labels"
_REQUEST_TIMEOUT_S = 5

# Per-process cache: (chain_id, address_lower) -> labels (possibly empty).
_label_cache: dict[tuple[int, str], list[str]] = {}


def fetch_swiss_knife_labels(address: str, chain_id: int) -> list[str]:
    """Return Swiss Knife labels for ``address`` on ``chain_id`` (empty on miss).

    Treats every failure mode — missing address, HTTP error, malformed
    response — as "no labels available" so callers can layer this safely
    on top of other label sources without worrying about exceptions.
    """
    if not address or not address.startswith("0x") or len(address) != 42:
        return []

    cache_key = (chain_id, address.lower())
    cached = _label_cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{_API_URL}/{address}"
    data = fetch_json(url, params={"chainId": chain_id}, timeout=_REQUEST_TIMEOUT_S)

    # Swiss Knife returns a JSON array directly. fetch_json's type hint is
    # `dict | None` but it returns whatever `resp.json()` parses to.
    labels: list[str] = []
    if isinstance(data, list):
        labels = [s for s in data if isinstance(s, str) and s]

    _label_cache[cache_key] = labels
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
    _label_cache.clear()
