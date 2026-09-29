"""Helpers for Safe transactions that target the canonical multisend utility.

When a Safe executes a batch via MultiSendCallOnly (or legacy MultiSend), the
outer call is a DELEGATECALL from the Safe into the utility, which then makes
each inner transaction from the Safe's own context. The Safe API already
provides the decoded inner calls in ``dataDecoded.parameters[0].valueDecoded``;
we use that decoded form directly rather than decoding the packed
`multiSend(bytes)` payload ourselves.

An inner transaction with ``operation == 0`` is a plain CALL whose
``msg.sender`` is the Safe, so a batch made only of those is exactly a
sequence of calls from the Safe and can be simulated as one ordered bundle.
Only an inner DELEGATECALL (legacy MultiSend permits them) cannot be modeled.
"""

# Canonical Safe utility contract addresses are identical across all chains
# Safe is deployed on. Source: https://github.com/safe-global/safe-deployments
_SAFE_UTILS: dict[str, str] = {
    "0x40a2accbd92bca938b02010e17a5b8929b49130d": "Safe MultiSendCallOnly",
    "0x9641d764fc13c8b624c04430c7356c1c7c8102e2": "Safe MultiSendCallOnly v1.4.1",
    "0x38869bf66a61cf6bdb996a6ae40d5853fd43b526": "Safe MultiSend (legacy, permits nested DELEGATECALL)",
    "0x998739bfdaadde7c933b942a68053933098f9eda": "Safe MultiSend v1.3.0",
    "0xa238cbeb142c10ef7ad8442c6d1f9e89e07e7761": "Safe MultiSend v1.4.1",
    "0xd53cd0ab83d845ac265be939c57f53ad838012c9": "Safe SignMessageLib",
}

# The multisend utilities: every inner transaction runs from the Safe, as a
# CALL unless its own operation says DELEGATECALL (MultiSendCallOnly rejects those).
_MULTISEND_UTILS = frozenset(address for address, label in _SAFE_UTILS.items() if "MultiSend" in label)


def safe_utility_label(address: str) -> str:
    """Return a human-readable label for a canonical Safe utility, or ""."""
    return _SAFE_UTILS.get(address.lower(), "")


def extract_inner_calls(tx: dict) -> list[dict[str, str]]:
    """Pull the multisend inner calls out of a Safe transaction.

    Expects ``tx['dataDecoded']['parameters'][0]['valueDecoded']`` to contain a
    list of inner txs (the Safe API's decoded multiSend payload). Returns a list
    of ``{target, data, value}`` dicts suitable for ``explain_batch_transaction``.
    Returns ``[]`` when the structure isn't present.
    """
    data_decoded = tx.get("dataDecoded") or {}
    if data_decoded.get("method") not in {"multiSend"}:
        return []

    params = data_decoded.get("parameters") or []
    if not params:
        return []

    value_decoded = params[0].get("valueDecoded")
    if not isinstance(value_decoded, list):
        return []

    calls: list[dict[str, str]] = []
    for inner in value_decoded:
        to = inner.get("to")
        if not to:
            continue
        calls.append(
            {
                "target": to,
                "data": inner.get("data") or "0x",
                "value": str(inner.get("value", "0")),
                "operation": str(inner.get("operation", "0")),
            }
        )
    return calls


def is_simulatable_multisend(tx: dict, inner_calls: list[dict[str, str]]) -> bool:
    """Whether a multisend batch is exactly a sequence of plain calls from the Safe.

    True when the outer call is a DELEGATECALL into a canonical multisend
    utility and every inner transaction is a CALL. The utility then executes
    each one with ``msg.sender`` = the Safe, so simulating the inner calls as
    one ordered bundle from the Safe reproduces the real execution. Any inner
    DELEGATECALL, or an unknown delegate target, is not modeled.
    """
    if int(tx.get("operation", 0) or 0) != 1:
        return False
    if (tx.get("to") or "").lower() not in _MULTISEND_UTILS:
        return False
    return bool(inner_calls) and all(int(call.get("operation", "0") or 0) == 0 for call in inner_calls)


def build_context_note(tx: dict, safe_address: str, simulated: bool = False) -> str:
    """Describe the outer-call semantics so the LLM doesn't have to guess.

    Returns a short note that explains DELEGATECALL semantics and identifies
    the multisend target when applicable. Empty string for plain CALLs.
    ``simulated`` says whether the inner calls were simulated as a bundle.
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
            "Every inner transaction is a plain CALL from the Safe, so the batch was simulated as one "
            "ordered bundle of those calls from the Safe — the same execution the multisend performs."
            if simulated
            else "Tenderly simulation was skipped because our simulator only models plain CALL "
            "and would produce spurious revert info for this delegated flow."
        ),
    ]
    return "\n".join(lines)
