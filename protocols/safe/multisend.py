"""Helpers for Safe transactions that target the canonical multisend utility.

When a Safe executes a batch via MultiSendCallOnly (or legacy MultiSend), the
outer call is a DELEGATECALL from the Safe into the utility, which then makes
each inner transaction from the Safe's own context. The Safe Queue Service does
not decode calldata, so the packed ``multiSend(bytes)`` payload is decoded here,
and only when the outer call delegates into a known multisend utility: the
selector alone does not prove the destination runs Safe's packed call format.

An inner transaction with ``operation == 0`` is a plain CALL whose
``msg.sender`` is the Safe, so a batch made only of those is exactly a
sequence of calls from the Safe and can be simulated as one ordered bundle.
Only an inner DELEGATECALL (legacy MultiSend permits them) cannot be modeled.
"""

from eth_abi import decode
from eth_utils import to_checksum_address

from utils.logger import get_logger

logger = get_logger("safe.multisend")

# Official Safe utility deployments. Each address is the same on every chain it is deployed to; v1.3.0
# has both a "canonical" and an "EIP-155" deployment, and a chain may use either.
# Source: https://github.com/safe-global/safe-deployments (src/assets/v*/multi_send*.json)
_SAFE_UTILS: dict[str, str] = {
    "0x40a2accbd92bca938b02010e17a5b8929b49130d": "Safe MultiSendCallOnly",  # v1.3.0 canonical
    "0xa1dabef33b3b82c7814b6d82a79e50f4ac44102b": "Safe MultiSendCallOnly v1.3.0 (EIP-155)",
    "0x9641d764fc13c8b624c04430c7356c1c7c8102e2": "Safe MultiSendCallOnly v1.4.1",
    "0x8d29be29923b68abfdd21e541b9374737b49cdad": "Safe MultiSend v1.1.1 (permits nested DELEGATECALL)",
    "0xa238cbeb142c10ef7ad8442c6d1f9e89e07e7761": "Safe MultiSend v1.3.0 (permits nested DELEGATECALL)",
    "0x998739bfdaadde7c933b942a68053933098f9eda": "Safe MultiSend v1.3.0 EIP-155 (permits nested DELEGATECALL)",
    "0x38869bf66a61cf6bdb996a6ae40d5853fd43b526": "Safe MultiSend v1.4.1 (permits nested DELEGATECALL)",
    "0xd53cd0ab83d845ac265be939c57f53ad838012c9": "Safe SignMessageLib",
}

# The multisend utilities: every inner transaction runs from the Safe, as a
# CALL unless its own operation says DELEGATECALL (MultiSendCallOnly rejects those).
_MULTISEND_UTILS = frozenset(address for address, label in _SAFE_UTILS.items() if "MultiSend" in label)

# bytes4(keccak256("multiSend(bytes)"))
MULTISEND_SELECTOR = "0x8d80ff0a"
# operation (1) + to (20) + value (32) + data length (32)
_PACKED_HEADER_BYTES = 85


def safe_utility_label(address: str) -> str:
    """Return a human-readable label for a canonical Safe utility, or ""."""
    return _SAFE_UTILS.get(address.lower(), "")


def is_multisend_call(tx: dict) -> bool:
    """Whether a Safe tx is a DELEGATECALL of ``multiSend(bytes)`` into a known multisend utility.

    Args:
        tx: Safe Queue Service multisig transaction.

    Returns:
        True when the destination, operation and selector all match.
    """
    if int(tx.get("operation", 0) or 0) != 1:
        return False
    if (tx.get("to") or "").lower() not in _MULTISEND_UTILS:
        return False
    return (tx.get("data") or "").lower().startswith(MULTISEND_SELECTOR)


def decode_multisend(data_hex: str) -> list[dict[str, str]]:
    """Decode ``multiSend(bytes)`` calldata into its inner calls.

    The ``bytes`` argument packs each call as ``operation (1 byte) | to (20) |
    value (32) | data length (32) | data``, back to back, and must be consumed
    exactly.

    Args:
        data_hex: 0x-prefixed ``multiSend(bytes)`` calldata.

    Returns:
        ``{target, data, value, operation}`` dicts (all strings, ``target``
        checksummed) in execution order.

    Raises:
        ValueError: If the calldata is not a well-formed, non-empty multisend.
    """
    if not data_hex.lower().startswith(MULTISEND_SELECTOR):
        raise ValueError("not multiSend(bytes) calldata")
    try:
        (packed,) = decode(["bytes"], bytes.fromhex(data_hex[10:]))
    except Exception as e:  # noqa: BLE001 - eth_abi raises several decoding error types
        raise ValueError(f"multiSend argument does not decode: {e}") from e

    calls: list[dict[str, str]] = []
    offset = 0
    while offset < len(packed):
        if offset + _PACKED_HEADER_BYTES > len(packed):
            raise ValueError(f"truncated multisend call header at byte {offset}")
        operation = packed[offset]
        if operation not in (0, 1):
            raise ValueError(f"invalid multisend operation {operation} at byte {offset}")
        to = packed[offset + 1 : offset + 21]
        value = int.from_bytes(packed[offset + 21 : offset + 53], "big")
        size = int.from_bytes(packed[offset + 53 : offset + 85], "big")
        offset += _PACKED_HEADER_BYTES
        if offset + size > len(packed):
            raise ValueError(f"multisend call data at byte {offset} overruns the payload")
        calls.append(
            {
                "target": to_checksum_address(to),
                "data": "0x" + packed[offset : offset + size].hex(),
                "value": str(value),
                "operation": str(operation),
            }
        )
        offset += size
    if not calls:
        raise ValueError("empty multisend payload")
    return calls


def extract_inner_calls(tx: dict) -> list[dict[str, str]]:
    """Pull the multisend inner calls out of a Safe transaction.

    Decodes only a DELEGATECALL into a known multisend utility (see
    :func:`is_multisend_call`); anything else stays a single call. Returns a
    list of ``{target, data, value, operation}`` dicts suitable for
    ``explain_batch_transaction``, or ``[]`` when the tx is not a multisend or
    its payload is malformed.
    """
    if not is_multisend_call(tx):
        return []
    try:
        return decode_multisend(tx["data"])
    except ValueError as e:
        logger.warning("Could not decode multisend payload of Safe tx %s: %s", tx.get("safeTxHash", "?"), e)
        return []


def is_simulatable_multisend(tx: dict, inner_calls: list[dict[str, str]]) -> bool:
    """Whether a multisend batch is exactly a sequence of plain calls from the Safe.

    True when the outer call is a DELEGATECALL into a canonical multisend
    utility and every inner transaction is a CALL. The utility then executes
    each one with ``msg.sender`` = the Safe, so simulating the inner calls as
    one ordered bundle from the Safe reproduces the real execution. Any inner
    DELEGATECALL, or an unknown delegate target, is not modeled.
    """
    return (
        is_multisend_call(tx)
        and bool(inner_calls)
        and all(int(call.get("operation", "0") or 0) == 0 for call in inner_calls)
    )


def build_context_note(tx: dict, safe_address: str, *, simulate_as_bundle: bool = False) -> str:
    """Describe the outer-call semantics so the LLM doesn't have to guess.

    Returns a short note that explains DELEGATECALL semantics and identifies
    the multisend target when applicable. Empty string for plain CALLs.
    ``simulate_as_bundle`` says whether the simulator can model the inner calls.
    """
    operation = int(tx.get("operation", 0) or 0)
    if operation != 1:
        return ""

    target = tx.get("to") or ""
    label = safe_utility_label(target)
    lines = [
        f"Outer call is DELEGATECALL from the Safe ({safe_address}) into {target}"
        + (f" ({label})" if label else "")
        + ".",
        f"Inner calls execute in the Safe's own storage context: msg.sender = {safe_address}.",
        (
            "Every inner transaction is a plain CALL from the Safe, so the batch can be simulated as one "
            "ordered bundle of calls from the Safe."
            if simulate_as_bundle
            else "Tenderly simulation was skipped because our simulator only models plain CALL "
            "and would produce spurious revert info for this delegated flow."
        ),
    ]
    return "\n".join(lines)
