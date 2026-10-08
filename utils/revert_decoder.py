"""Decode EVM revert payloads into readable errors.

Tenderly names ``require(cond, "reason")`` reverts but leaves custom errors
(``revert InvalidOracle(asset)``) as raw bytes, so a simulated revert reached
reports as a bare "reverted". The reason is usually the whole story — an
oracle not yet set, a token not yet enabled — so it is decoded here from the
reverting contract's verified ABI.
"""

from eth_abi import decode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from utils.logger import get_logger

logger = get_logger("utils.revert_decoder")

_ERROR_STRING_SELECTOR = "08c379a0"  # Error(string)
_PANIC_SELECTOR = "4e487b71"  # Panic(uint256)
_PANIC_CODES = {
    0x01: "assertion failed",
    0x11: "arithmetic overflow/underflow",
    0x12: "division by zero",
    0x21: "invalid enum value",
    0x31: "pop on empty array",
    0x32: "array index out of bounds",
    0x41: "out of memory",
    0x51: "call to uninitialized function",
}


def _type_string(param: dict) -> str:
    """Canonical ABI type, expanding tuples (``(address,uint256)[]``)."""
    kind = str(param.get("type", ""))
    if kind.startswith("tuple"):
        inner = ",".join(_type_string(component) for component in param.get("components") or [])
        return f"({inner}){kind[len('tuple') :]}"
    return kind


def _render(value: object) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
        return to_checksum_address(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render(item) for item in value) + "]"
    return str(value)


def _custom_errors(abi: list[dict]) -> dict[str, tuple[str, list[dict]]]:
    """Selector (hex, no 0x) → (error name, inputs) for every ``error`` entry."""
    errors: dict[str, tuple[str, list[dict]]] = {}
    for entry in abi:
        if entry.get("type") != "error" or not entry.get("name"):
            continue
        inputs = list(entry.get("inputs") or [])
        signature = f"{entry['name']}({','.join(_type_string(param) for param in inputs)})"
        errors[function_signature_to_4byte_selector(signature).hex()] = (str(entry["name"]), inputs)
    return errors


def decode_revert(data_hex: str, abi: list[dict] | None = None) -> str:
    """Readable form of a revert payload, or "" when it cannot be decoded.

    Args:
        data_hex: Revert data as 0x-prefixed hex.
        abi: Verified ABI of the reverting contract; its ``error`` entries name
            custom errors. ``Error(string)`` and ``Panic(uint256)`` need none.

    Returns:
        e.g. ``InvalidOracle(_asset=0x5086…)``, ``require: "not allowed"``,
        ``Panic(0x11: arithmetic overflow/underflow)``; "" for an empty or
        unknown payload.
    """
    if not data_hex or not data_hex.startswith("0x") or len(data_hex) < 10:
        return ""
    selector, body = data_hex[2:10].lower(), bytes.fromhex(data_hex[10:])
    try:
        if selector == _ERROR_STRING_SELECTOR:
            return f'require: "{decode(["string"], body)[0]}"'
        if selector == _PANIC_SELECTOR:
            code = int(decode(["uint256"], body)[0])
            return f"Panic(0x{code:02x}: {_PANIC_CODES.get(code, 'unknown panic code')})"
        error = _custom_errors(abi or []).get(selector)
        if error is None:
            return ""
        name, inputs = error
        values = decode([_type_string(param) for param in inputs], body) if inputs else ()
        args = ", ".join(
            f"{param.get('name') or f'arg{i}'}={_render(value)}" for i, (param, value) in enumerate(zip(inputs, values))
        )
        return f"{name}({args})"
    except Exception as error:  # noqa: BLE001 - malformed payloads decode to ""
        logger.debug("Could not decode revert data %s: %s", data_hex[:10], error)
        return ""
