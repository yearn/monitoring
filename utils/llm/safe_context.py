"""Resolve transactions referenced by Safe owner hash approvals.

The transaction service supplies a candidate preimage; the receiving Safe's
getTransactionHash verifies every signed field before we describe its effects.
This applies to CAP, Yearn, and any other protocol using nested Safe ownership.
"""

import os
import re
from dataclasses import dataclass, field

from eth_abi import decode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from protocols.safe.addresses import safe_apis
from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.chains import Chain, safe_network_to_chain_id
from utils.http_client import fetch_json
from utils.known_addresses import lookup
from utils.llm.report import address_link
from utils.web3_wrapper import ChainManager

MAX_APPROVALS = 8
_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_TX_FIELDS = (
    ("to", "address"),
    ("value", "uint256"),
    ("data", "bytes"),
    ("operation", "uint8"),
    ("safeTxGas", "uint256"),
    ("baseGas", "uint256"),
    ("gasPrice", "uint256"),
    ("gasToken", "address"),
    ("refundReceiver", "address"),
    ("nonce", "uint256"),
)


def _function(name: str, inputs: list[tuple[str, str]], output: str) -> dict:
    return {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [{"name": name, "type": typ} for name, typ in inputs],
        "outputs": [{"name": "", "type": output}],
    }


_SAFE_ABI = [
    _function("getTransactionHash", list(_TX_FIELDS), "bytes32"),
    _function("getOwners", [], "address[]"),
    _function("getThreshold", [], "uint256"),
    _function("nonce", [], "uint256"),
]
# Decode Safe's own administration calls directly, including when the proxy's
# verified ABI has no methods. Generic calls still use the normal ABI resolver.
_OWNER_SIGNATURES = (
    "addOwnerWithThreshold(address,uint256)",
    "removeOwner(address,address,uint256)",
    "swapOwner(address,address,address)",
    "changeThreshold(uint256)",
)
_OWNER_SELECTORS = {
    "0x" + function_signature_to_4byte_selector(signature).hex(): signature for signature in _OWNER_SIGNATURES
}


@dataclass
class SafeApprovalContext:
    safe: str
    approved_hash: str
    reason: str = ""
    transaction: dict | None = None
    call: DecodedCall | None = None
    owners: list[str] | None = None
    threshold: int | None = None
    nonce: int | None = None
    addresses: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


def _transaction_values(tx: dict) -> list:
    values = []
    for name, typ in _TX_FIELDS:
        value = tx[name]
        if typ == "address":
            value = to_checksum_address(value)
        elif typ == "bytes":
            # Safe's API represents empty calldata as null.
            value = bytes.fromhex((value or "0x")[2:])
        else:
            value = int(value)
            if value < 0:
                raise ValueError("Negative Safe transaction field")
        values.append(value)
    if values[3] not in (0, 1):
        raise ValueError("Invalid Safe operation")
    return values


def _decode_payload(tx: dict, chain_id: int) -> DecodedCall | None:
    data = tx.get("data") or "0x"
    signature = _OWNER_SELECTORS.get(data[:10].lower())
    if signature and tx["operation"] == 0 and tx["to"].lower() == tx["safe"].lower():
        types = signature.split("(")[1][:-1].split(",")
        params = list(zip(types, decode(types, bytes.fromhex(data[10:]))))
        return DecodedCall(signature.split("(")[0], signature, params)
    return decode_calldata(data, chain_id=chain_id, target=tx["to"])


def _resolve_approval(chain_id: int, safe: str, approved_hash: str, base_url: str) -> SafeApprovalContext:
    context = SafeApprovalContext(safe, approved_hash, addresses=[safe])
    key = os.getenv("SAFE_API_KEY") or os.getenv("SAFE_API_KEY_2")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    tx = fetch_json(
        f"{base_url}/api/v2/multisig-transactions/{approved_hash}/",
        timeout=15,
        headers=headers,
    )
    if not isinstance(tx, dict):
        context.reason = "Safe transaction service did not return a preimage (unindexed hash or API unavailable)."
        return context
    if (
        str(tx.get("safe", "")).lower() != safe.lower()
        or str(tx.get("safeTxHash", "")).lower() != approved_hash.lower()
    ):
        context.reason = "Rejected transaction service candidate: receiving Safe or hash does not match."
        return context
    try:
        values = _transaction_values(tx)
        client = ChainManager.get_client(Chain.from_chain_id(chain_id))
        contract = client.get_contract(safe, _SAFE_ABI)
        computed = contract.functions.getTransactionHash(*values).call()
        if bytes(computed) != bytes.fromhex(approved_hash[2:]):
            context.reason = "Rejected transaction service candidate: on-chain transaction hash does not match."
            return context
    except Exception:  # noqa: BLE001 - incomplete/unverifiable data is not ground truth
        context.reason = (
            "Transaction service candidate could not be verified with the receiving Safe's getTransactionHash."
        )
        return context

    # Only signed fields become authoritative; API labels, origin, decoded data,
    # confirmations, and execution status are deliberately not trusted here.
    context.transaction = dict(zip((name for name, _ in _TX_FIELDS), values))
    context.transaction["safe"] = safe
    context.transaction["data"] = "0x" + values[2].hex()
    context.addresses.append(values[0])
    try:
        context.call = _decode_payload(context.transaction, chain_id)
        if context.call:
            for typ, value in context.call.params:
                if typ == "address":
                    context.addresses.append(to_checksum_address(value))
    except Exception:  # noqa: BLE001 - preserve verified raw payload if decoding fails
        pass
    try:
        with client.batch_requests() as batch:
            batch.add(contract.functions.getOwners())
            batch.add(contract.functions.getThreshold())
            batch.add(contract.functions.nonce())
            owners, threshold, nonce = client.execute_batch(batch)
        current_owners = [str(to_checksum_address(owner)) for owner in owners]
        current_threshold, current_nonce = int(threshold), int(nonce)
        context.owners = current_owners
        context.threshold, context.nonce = current_threshold, current_nonce
        context.addresses.extend(current_owners)
    except Exception:  # noqa: BLE001 - payload verification survives optional state failure
        pass
    context.addresses = list(dict.fromkeys(context.addresses))
    context.labels = {address: label for address in context.addresses if (label := lookup(chain_id, address))}
    return context


def resolve_safe_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[SafeApprovalContext]:
    """Enrich approveHash only, for every protocol on configured Safe networks.

    Deduplicated and bounded per alert; never cache mutable Safe state or a
    missing preimage, since a queued transaction may be indexed shortly later.
    """
    base_url = next((url for network, url in safe_apis.items() if safe_network_to_chain_id(network) == chain_id), None)
    if not base_url:
        return []
    contexts = []
    seen: set[tuple[str, str]] = set()
    for target, call in targets_and_calls:
        if call.signature != "approveHash(bytes32)" or len(call.params) != 1 or call.params[0][0] != "bytes32":
            continue
        value = call.params[0][1]
        approved_hash = "0x" + value.hex() if isinstance(value, bytes) else str(value)
        if not _HASH_RE.fullmatch(approved_hash):
            continue
        try:
            safe = to_checksum_address(target)
        except ValueError:
            continue
        approved_hash = approved_hash.lower()
        identity = (safe.lower(), approved_hash)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            contexts.append(_resolve_approval(chain_id, safe, approved_hash, base_url))
        except Exception:  # noqa: BLE001 - one lookup must not drop other approvals
            contexts.append(
                SafeApprovalContext(
                    safe, approved_hash, reason="Safe approval context lookup failed.", addresses=[safe]
                )
            )
        if len(contexts) >= MAX_APPROVALS:
            break
    return contexts


def _effect_lines(context: SafeApprovalContext) -> list[str]:
    tx, call = context.transaction, context.call
    if not tx or not call or tx["operation"] != 0 or tx["to"].lower() != context.safe.lower():
        return []
    if context.nonce is not None and tx["nonce"] < context.nonce:
        # Hash verification accepts historical nonces, but execTransaction
        # always authorizes the hash formed with the Safe's current nonce.
        return []
    params = [value for _, value in call.params]
    if call.function_name == "addOwnerWithThreshold" and context.owners is not None:
        owner, threshold = to_checksum_address(params[0]), int(params[1])
        if owner in context.owners:
            return [
                "The proposed new owner is already a current owner; addOwnerWithThreshold would revert against this state."
            ]
        count = len(context.owners) + 1
        if threshold < 1 or threshold > count:
            return [
                f"The proposed threshold {threshold} is invalid for {count} owners; execution would revert against this state."
            ]
        if owner.lower() in {"0x" + "00" * 20, "0x" + "00" * 19 + "01", context.safe.lower()}:
            return ["The proposed owner is invalid for this Safe; execution would revert."]
        return [
            f"On successful separate execution: add owner {owner}; threshold {context.threshold} -> {threshold}; owners {len(context.owners)} -> {count} ({threshold}-of-{count})."
        ]
    if call.function_name == "changeThreshold" and context.owners is not None:
        threshold, count = int(params[0]), len(context.owners)
        if threshold < 1 or threshold > count:
            return [
                f"The proposed threshold {threshold} is invalid for {count} owners; execution would revert against this state."
            ]
        return [f"On successful separate execution: change threshold {context.threshold} -> {threshold}."]
    return []


def format_safe_prompt(contexts: list[SafeApprovalContext]) -> str:
    lines = [
        "Safe hash approvals: approveHash records approval on the RECEIVING Safe below, not on the calling owner's Safe.",
        "The current call does not execute the referenced payload. Describe its downstream effect conditionally, on separate execution.",
        "Approval is permanent; no built-in revocation. Judge the resolved payload's actions, not hash opacity alone.",
    ]
    for context in contexts:
        lines.extend([f"Receiving Safe: {context.safe}", f"Approved hash: {context.approved_hash}"])
        if not context.transaction:
            lines.append(f"Payload unresolved: {context.reason} Its impact remains unknown; do not guess a preimage.")
            continue
        tx = context.transaction
        lines.extend(
            [
                "Preimage VERIFIED: receiving Safe's on-chain getTransactionHash matches the approved hash.",
                f"Referenced transaction: to={tx['to']}; value={tx['value']} wei; operation={tx['operation']} ({'CALL' if tx['operation'] == 0 else 'DELEGATECALL'}); nonce={tx['nonce']}.",
                f"Referenced calldata: {tx['data']}",
                f"Gas/refund fields: safeTxGas={tx['safeTxGas']}, baseGas={tx['baseGas']}, gasPrice={tx['gasPrice']}, gasToken={tx['gasToken']}, refundReceiver={tx['refundReceiver']}.",
            ]
        )
        if context.call:
            lines.append(f"Referenced decoded call: {context.call.signature}; arguments={context.call.params!r}.")
        if context.owners is not None:
            lines.append(
                f"Current receiving Safe: {context.threshold}-of-{len(context.owners)}; nonce={context.nonce}; owners={', '.join(context.owners)}."
            )
            nonce_consumed = context.nonce is not None and tx["nonce"] < context.nonce
            if nonce_consumed:
                lines.append(
                    f"Referenced nonce {tx['nonce']} is already consumed (current nonce {context.nonce}). "
                    "This transaction is permanently unexecutable through normal Safe execution. "
                    "The approval cannot enable execution of this payload; do not describe its requested changes as prospective effects."
                )
            elif context.nonce is not None and tx["nonce"] > context.nonce:
                lines.append(
                    f"Referenced nonce {tx['nonce']} is higher than current nonce {context.nonce}; "
                    "this payload is queued behind earlier nonces and cannot execute yet. "
                    "Any effects below are conditional on the state when its nonce becomes current; "
                    "owners and threshold may change before then."
                )
            effects = _effect_lines(context)
            lines.extend(effects)
            if not nonce_consumed:
                lines.append(
                    "Separate execution must satisfy the receiving Safe's current authorization requirements and applicable guards."
                )
            if any(effect.startswith("On successful separate execution:") for effect in effects):
                lines.append(
                    "The proposed threshold applies only after successful separate execution; it is not the threshold authorizing this payload."
                )
        else:
            lines.append(
                "Current receiving Safe owners/threshold/nonce unavailable; no authorization count is established."
            )
        lines.append(
            "Owner address identity, existing hash approvals, collected signatures, and final execution status are not established by this context."
        )
    return "\n".join(lines)


def format_safe_report(contexts: list[SafeApprovalContext], chain_id: int, labels: dict[str, str]) -> str:
    # Render the same facts independently of the model, with linked addresses.
    text = format_safe_prompt(contexts)
    text = re.sub(
        r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])",
        lambda match: address_link(match[0], chain_id, labels),
        text,
    )
    return "### Safe Approval Context\n\n" + "\n\n".join(text.splitlines())
