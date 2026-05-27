"""Decode raw calldata into human-readable function calls.

Uses a local lookup table for common selectors and falls back to the
Sourcify 4byte signature database API for unknown ones. The Sourcify
results are persisted between runs in a file-backed cache so each
selector pays the lookup cost only once across all workflow invocations.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode
from eth_utils import to_checksum_address

from utils.calldata.known_selectors import KNOWN_SELECTORS
from utils.http import fetch_json
from utils.logging import get_logger

logger = get_logger("calldata_decoder")

# Sourcify 4byte signature database (successor to openchain.xyz)
_SELECTOR_LOOKUP_URL = "https://api.4byte.sourcify.dev/signature-database/v1/lookup"

# Persistent selector cache: append-only file backing the in-memory dict so we
# don't re-query Sourcify for the same selector on every cron run. Negative
# results are stored too (Sourcify miss → __NONE__ sentinel) so we don't retry
# selectors that aren't in any database. Format: one `selector|signature` per
# line, pipe-delimited because ABI signatures don't contain pipes.
_SELECTOR_CACHE_FILE = os.getenv("SELECTOR_CACHE_FILENAME", "selector-cache.txt")
_NEGATIVE_SENTINEL = "__NONE__"


def _load_selector_cache() -> dict[str, str | None]:
    cache: dict[str, str | None] = {}
    if not os.path.exists(_SELECTOR_CACHE_FILE):
        return cache
    try:
        with open(_SELECTOR_CACHE_FILE, "r") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if "|" not in line:
                    continue
                sel, sig = line.split("|", 1)
                cache[sel.lower()] = None if sig == _NEGATIVE_SENTINEL else sig
    except OSError as e:
        logger.warning("Failed to load selector cache %s: %s", _SELECTOR_CACHE_FILE, e)
    return cache


def _persist_selector(selector: str, signature: str | None) -> None:
    """Append one selector→signature pair to the on-disk cache (best-effort)."""
    try:
        with open(_SELECTOR_CACHE_FILE, "a") as f:
            f.write(f"{selector.lower()}|{signature or _NEGATIVE_SENTINEL}\n")
    except OSError as e:
        logger.debug("Failed to persist selector %s: %s", selector, e)


# In-memory cache: selector hex -> function signature or None.
# Bootstrapped from the on-disk cache so each workflow starts warm.
_selector_cache: dict[str, str | None] = _load_selector_cache()


@dataclass(frozen=True)
class DecodedCall:
    """Result of decoding a calldata hex string."""

    function_name: str
    signature: str
    params: list[tuple[str, Any]] = field(default_factory=list)


def is_selector_resolvable_offline(selector_hex: str) -> bool:
    """True if this selector is known without making a network call.

    Used by callers that want to attempt decoding only when it's free —
    e.g. recursive bytes-parameter decoding, where blindly calling the
    remote 4byte API on every blob would be both slow and likely wrong.
    """
    sel = selector_hex.lower()
    return sel in KNOWN_SELECTORS or _selector_cache.get(sel) is not None


def resolve_selector(selector_hex: str) -> str | None:
    """Resolve a 4-byte function selector to its text signature.

    Checks the local known_selectors table first, then the in-memory cache,
    and finally falls back to the Sourcify 4byte API.

    Args:
        selector_hex: The 4-byte selector including 0x prefix, e.g. "0xabaa1988".

    Returns:
        Function signature like "saveAssets()" or None if lookup fails.
    """
    selector_hex = selector_hex.lower()

    # 1. Local lookup table (no API call needed)
    if selector_hex in KNOWN_SELECTORS:
        return KNOWN_SELECTORS[selector_hex]

    # 2. In-memory cache from previous API calls
    if selector_hex in _selector_cache:
        return _selector_cache[selector_hex]

    # 3. Remote API fallback. Persist *only* when the response is structurally
    # well-formed — a transient Sourcify failure (timeout, 5xx, 429, network
    # blip) must NOT be written to the on-disk cache, because that file is
    # shared across every workflow run via actions/cache. A single bad minute
    # would otherwise blacklist a selector permanently across the CI fleet.
    data = fetch_json(_SELECTOR_LOOKUP_URL, params={"function": selector_hex})
    if not isinstance(data, dict):
        # `fetch_json` returns None on HTTP error / timeout / network failure.
        # Cache the miss in-memory (so we don't re-query within this run) but
        # don't persist — let the next run retry.
        _selector_cache[selector_hex] = None
        return None

    result_section = data.get("result")
    function_section = result_section.get("function") if isinstance(result_section, dict) else None
    if not isinstance(function_section, dict):
        # Response didn't match Sourcify's documented shape. Treat as transient.
        _selector_cache[selector_hex] = None
        return None

    results = function_section.get(selector_hex)
    if results:
        try:
            sig = results[0].get("name") if isinstance(results[0], dict) else None
        except (IndexError, AttributeError):
            sig = None
        if sig:
            _selector_cache[selector_hex] = sig
            _persist_selector(selector_hex, sig)
            return sig

    # Well-formed response with no signature for this selector — Sourcify
    # explicitly says "we don't know this one". Safe to persist so we skip
    # the lookup on future runs.
    _selector_cache[selector_hex] = None
    _persist_selector(selector_hex, None)
    return None


def _parse_param_types(signature: str) -> list[str]:
    """Extract parameter types from a function signature.

    Args:
        signature: Function signature like "grantRole(bytes32,address)".

    Returns:
        List of type strings, e.g. ["bytes32", "address"]. Empty list for no-arg functions.
    """
    match = re.search(r"\(([^)]*)\)", signature)
    if not match:
        return []
    params_str = match.group(1).strip()
    if not params_str:
        return []
    return [t.strip() for t in params_str.split(",")]


def _format_param_value(type_str: str, value: Any) -> str:
    """Format a decoded parameter value for display.

    Args:
        type_str: The ABI type, e.g. "address", "uint256", "bytes32".
        value: The decoded value from eth_abi.

    Returns:
        Human-readable string representation.
    """
    if type_str == "address":
        return to_checksum_address(value)
    if type_str == "bytes32":
        if isinstance(value, bytes):
            return "0x" + value.hex()
        return str(value)
    if type_str.startswith("bytes"):
        if isinstance(value, bytes):
            hex_str = "0x" + value.hex()
            if len(hex_str) > 66:
                return hex_str[:66] + "..."
            return hex_str
        return str(value)
    if type_str.startswith("uint") or type_str.startswith("int"):
        return str(value)
    if type_str == "bool":
        return str(value)
    if type_str == "string":
        return f'"{value}"'
    # Fallback
    return str(value)


def _resolve_signature_via_abi(chain_id: int, target: str, selector: str) -> str | None:
    """Look up the selector's signature in the target's verified ABI (best-effort)."""
    try:
        from utils.source_context import get_function_signature_by_selector

        sig: str | None = get_function_signature_by_selector(chain_id, target, selector)
        return sig
    except Exception as e:  # noqa: BLE001 - enrichment only; fall back to Sourcify
        logger.debug("ABI signature lookup failed for %s on %s: %s", selector, target, e)
        return None


def decode_calldata(data_hex: str, chain_id: int | None = None, target: str | None = None) -> DecodedCall | None:
    """Decode raw calldata into a structured representation.

    Args:
        data_hex: Full calldata hex string including 0x prefix.
        chain_id: Optional chain ID. When given with ``target``, the target's
            verified ABI is consulted first to resolve the signature — more
            reliable than the Sourcify 4byte database, which can't disambiguate
            selector collisions.
        target: Optional target address (see ``chain_id``).

    Returns:
        DecodedCall with function name, signature, and decoded params, or None if decoding fails.
    """
    if not data_hex or len(data_hex) < 10:
        return None

    selector = data_hex[:10]
    signature = None
    if chain_id is not None and target:
        signature = _resolve_signature_via_abi(chain_id, target, selector)
    if not signature:
        signature = resolve_selector(selector)
    if not signature:
        return None

    # Extract function name (everything before the first parenthesis)
    func_name = signature.split("(")[0]
    param_types = _parse_param_types(signature)

    params: list[tuple[str, Any]] = []
    if param_types:
        raw_data = bytes.fromhex(data_hex[10:])
        if raw_data:
            try:
                decoded_values = decode(param_types, raw_data)
                params = list(zip(param_types, decoded_values))
            except Exception:
                logger.debug("Failed to decode params for %s with types %s", selector, param_types)

    return DecodedCall(function_name=func_name, signature=signature, params=params)


def format_call_lines(data_hex: str) -> list[str]:
    """Decode calldata and return formatted lines for an alert message.

    Args:
        data_hex: Full calldata hex string including 0x prefix.

    Returns:
        List of formatted strings. Falls back to raw selector display on failure.
    """
    if not data_hex or len(data_hex) < 10:
        return []

    result = decode_calldata(data_hex)
    if not result:
        return [f"\U0001f4dd Function: `{data_hex[:10]}`"]

    lines = [f"\U0001f4dd Function: `{result.signature}`"]
    for type_str, value in result.params:
        formatted = _format_param_value(type_str, value)
        lines.append(f"    \u251c {type_str}: `{formatted}`")
    return lines
