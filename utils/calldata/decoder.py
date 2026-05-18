"""Decode raw calldata into human-readable function calls.

Uses a local lookup table for common selectors and falls back to the
Sourcify 4byte signature database API for unknown ones.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode
from eth_utils import to_checksum_address

from utils.calldata.known_selectors import KNOWN_SELECTORS
from utils.http import fetch_json
from utils.logging import get_logger

logger = get_logger("calldata_decoder")

# In-memory cache: selector hex -> function signature or None
_selector_cache: dict[str, str | None] = {}

# Sourcify 4byte signature database (successor to openchain.xyz)
_SELECTOR_LOOKUP_URL = "https://api.4byte.sourcify.dev/signature-database/v1/lookup"


@dataclass(frozen=True)
class DecodedCall:
    """Result of decoding a calldata hex string."""

    function_name: str
    signature: str
    params: list[tuple[str, Any]] = field(default_factory=list)


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

    # 3. Remote API fallback
    data = fetch_json(_SELECTOR_LOOKUP_URL, params={"function": selector_hex})
    if not data:
        _selector_cache[selector_hex] = None
        return None

    try:
        results = data.get("result", {}).get("function", {}).get(selector_hex)
        if results and len(results) > 0:
            sig = results[0].get("name")
            _selector_cache[selector_hex] = sig
            return sig
    except (AttributeError, IndexError, TypeError):
        pass

    _selector_cache[selector_hex] = None
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


def decode_calldata(data_hex: str) -> DecodedCall | None:
    """Decode raw calldata into a structured representation.

    Args:
        data_hex: Full calldata hex string including 0x prefix.

    Returns:
        DecodedCall with function name, signature, and decoded params, or None if decoding fails.
    """
    if not data_hex or len(data_hex) < 10:
        return None

    selector = data_hex[:10]
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
