"""Unwrap governance wrapper calls into the inner calls they carry.

A Safe that schedules or executes a timelock operation sends one outer call
(``executeBatch``, ``queueTransaction``, …) whose parameters hold the real
target/calldata pairs. Anything that inspects calldata for a specific action —
proxy upgrade detection first of all — sees only the wrapper unless it looks
inside. This module recognizes the common wrapper shapes by selector and returns
the inner calls, decoding with fixed ABI types so no 4byte lookup is involved.

Supported wrappers:
    - OpenZeppelin TimelockController: ``schedule``, ``scheduleBatch``,
      ``execute``, ``executeBatch``
    - Compound-style Timelock: ``queueTransaction``, ``executeTransaction``
      (the inner calldata is ``selector(signature) ++ data`` when ``signature``
      is non-empty, else ``data`` as-is)
    - Maple GovernorTimelock: ``scheduleProposals``
"""

from collections.abc import Callable
from dataclasses import dataclass

from eth_abi import decode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from utils.logger import get_logger

logger = get_logger("utils.calldata.wrappers")


@dataclass(frozen=True)
class InnerCall:
    """One call carried inside a wrapper, with where it came from."""

    target: str  # checksummed
    data: str  # 0x-prefixed calldata
    via: str  # e.g. "executeBatch call 2", for attribution in alerts


_Extractor = Callable[[tuple], list[tuple[str, bytes]]]


def _single(values: tuple) -> list[tuple[str, bytes]]:
    """(target, value, data, …) — OpenZeppelin schedule/execute."""
    return [(values[0], values[2])]


def _batch(values: tuple) -> list[tuple[str, bytes]]:
    """(targets, values, payloads, …) — OpenZeppelin scheduleBatch/executeBatch."""
    return list(zip(values[0], values[2]))


def _compound(values: tuple) -> list[tuple[str, bytes]]:
    """(target, value, signature, data, eta) — Compound-style Timelock."""
    target, _value, signature, data, _eta = values
    if signature:
        data = function_signature_to_4byte_selector(signature) + data
    return [(target, data)]


def _maple(values: tuple) -> list[tuple[str, bytes]]:
    """(targets, payloads) — Maple GovernorTimelock.scheduleProposals."""
    return list(zip(values[0], values[1]))


# signature → how to pull (target, calldata) pairs out of the decoded values
_WRAPPER_SIGNATURES: dict[str, _Extractor] = {
    "schedule(address,uint256,bytes,bytes32,bytes32,uint256)": _single,
    "execute(address,uint256,bytes,bytes32,bytes32)": _single,
    "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)": _batch,
    "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)": _batch,
    "queueTransaction(address,uint256,string,bytes,uint256)": _compound,
    "executeTransaction(address,uint256,string,bytes,uint256)": _compound,
    "scheduleProposals(address[],bytes[])": _maple,
}


def _parse_types(signature: str) -> list[str]:
    """Top-level parameter types of a signature with no tuple parameters."""
    inner = signature[signature.index("(") + 1 : -1]
    return inner.split(",") if inner else []


_WRAPPERS: dict[str, tuple[str, list[str], _Extractor]] = {
    "0x" + function_signature_to_4byte_selector(sig).hex(): (sig.split("(")[0], _parse_types(sig), extractor)
    for sig, extractor in _WRAPPER_SIGNATURES.items()
}

# Wrappers whose inner calls run inside the wrapping transaction. The others
# only queue their inner calls for a later transaction.
_EXECUTING_WRAPPERS = frozenset({"execute", "executeBatch", "executeTransaction"})


def is_wrapper_call(data_hex: str) -> bool:
    """True when the calldata's selector is a supported wrapper."""
    return bool(data_hex) and data_hex[:10].lower() in _WRAPPERS


def unwrap_calls(data_hex: str) -> list[InnerCall]:
    """Inner calls of a supported wrapper, in order; [] for anything else.

    Malformed wrapper calldata also yields [] — this feeds best-effort
    enrichment, and a decode failure must not break the alert.
    """
    if not is_wrapper_call(data_hex):
        return []

    name, types, extractor = _WRAPPERS[data_hex[:10].lower()]
    try:
        values = decode(types, bytes.fromhex(data_hex[10:]))
        pairs = extractor(values)
    except Exception as e:  # noqa: BLE001 - adversarial/malformed payloads
        logger.debug("could not unwrap %s calldata: %s", name, e)
        return []

    multiple = len(pairs) > 1
    return [
        InnerCall(
            target=to_checksum_address(target),
            data="0x" + bytes(data).hex(),
            via=f"{name} call {i}" if multiple else name,
        )
        for i, (target, data) in enumerate(pairs, start=1)
    ]


def unwrap_executed_calls(data_hex: str) -> list[InnerCall]:
    """Inner calls that run in this transaction: those of an ``execute``-type wrapper.

    A ``schedule``/``queueTransaction`` only queues its inner calls, so treating
    them as executed now would describe state changes this transaction does not
    make — wrong whenever one batch schedules one operation and executes another.
    """
    if not is_wrapper_call(data_hex) or _WRAPPERS[data_hex[:10].lower()][0] not in _EXECUTING_WRAPPERS:
        return []
    return unwrap_calls(data_hex)
