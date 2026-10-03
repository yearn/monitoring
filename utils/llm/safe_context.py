"""Explain the intended transaction behind a Safe owner's hash approval."""

import os
import re
from dataclasses import dataclass, field

from eth_abi import decode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from protocols.safe.addresses import safe_apis
from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.chains import Chain, safe_network_to_chain_id
from utils.disk_cache import MISS, DiskCache
from utils.http_client import fetch_json
from utils.known_addresses import lookup
from utils.llm.report import address_link
from utils.web3_wrapper import ChainManager

MAX_APPROVALS = 8
_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_PAYLOAD_CACHE = DiskCache("safe-approval-payloads", max_entries=256, max_bytes=4 * 1024 * 1024)


_SAFE_ABI = [
    {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": output}],
    }
    for name, output in (("getOwners", "address[]"), ("getThreshold", "uint256"), ("nonce", "uint256"))
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
    """Referenced payload and optional current Safe configuration."""

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


def _transaction_payload(tx: dict) -> dict:
    """Keep only the fields needed to describe the intended action."""
    # The service represents empty calldata as null.
    data = tx["data"] or "0x"
    if not data.startswith("0x"):
        raise ValueError("Invalid Safe calldata")
    data = "0x" + bytes.fromhex(data[2:]).hex()
    payload = {
        "safe": to_checksum_address(tx["safe"]),
        "safeTxHash": tx["safeTxHash"],
        "to": to_checksum_address(tx["to"]),
        "value": int(tx["value"]),
        "data": data,
        "operation": int(tx["operation"]),
        "nonce": int(tx["nonce"]),
    }
    if payload["operation"] not in (0, 1):
        raise ValueError("Invalid Safe operation")
    if payload["value"] < 0 or payload["nonce"] < 0:
        raise ValueError("Negative Safe transaction field")
    return payload


def _decode_payload(tx: dict, chain_id: int) -> DecodedCall | None:
    """Decode Safe administration locally, or use the normal calldata decoder."""
    data = tx.get("data") or "0x"
    signature = _OWNER_SELECTORS.get(data[:10].lower())
    if signature and tx["operation"] == 0 and tx["to"].lower() == tx["safe"].lower():
        types = signature.split("(")[1][:-1].split(",")
        params = [
            (typ, to_checksum_address(value) if typ == "address" else value)
            for typ, value in zip(types, decode(types, bytes.fromhex(data[10:])))
        ]
        return DecodedCall(signature.split("(")[0], signature, params)
    return decode_calldata(data, chain_id=chain_id, target=tx["to"])


def _resolve_approval(chain_id: int, safe: str, approved_hash: str, base_url: str) -> SafeApprovalContext:
    """Look up the approved transaction and add context useful to its explanation."""
    context = SafeApprovalContext(safe, approved_hash, addresses=[safe])
    cache_key = f"{chain_id}-{safe.lower()}-{approved_hash}"
    tx = _PAYLOAD_CACHE.get(cache_key)
    if tx is MISS:
        key = os.getenv("SAFE_API_KEY") or os.getenv("SAFE_API_KEY_2")
        tx = fetch_json(
            f"{base_url}/api/v2/multisig-transactions/{approved_hash}/",
            timeout=15,
            headers={"Authorization": f"Bearer {key}"} if key else {},
            retries=0,
        )
    if not isinstance(tx, dict):
        context.reason = "Referenced transaction not found or Safe transaction service unavailable."
        return context
    if (
        str(tx.get("safe", "")).lower() != safe.lower()
        or str(tx.get("safeTxHash", "")).lower() != approved_hash.lower()
    ):
        context.reason = "Transaction service record's Safe or hash does not match the approval."
        return context
    try:
        context.transaction = _transaction_payload(tx)
    except KeyError, TypeError, ValueError, AttributeError:
        context.reason = "Referenced transaction has incomplete or malformed action fields."
        return context
    # Cache the immutable action, excluding status, signatures and API descriptions.
    _PAYLOAD_CACHE.set_positive(cache_key, context.transaction)
    context.addresses.append(context.transaction["to"])
    try:
        context.call = _decode_payload(context.transaction, chain_id)
        if context.call:
            for typ, value in context.call.params:
                if typ == "address":
                    context.addresses.append(to_checksum_address(value))
    except Exception:  # noqa: BLE001 - preserve raw payload if decoding fails
        pass
    # State reads only help with the configuration changes described below.
    if (
        context.call
        and context.call.signature in _OWNER_SIGNATURES
        and context.transaction["operation"] == 0
        and context.transaction["to"].lower() == safe.lower()
    ):
        _read_configuration(context, chain_id)
    context.addresses = list(dict.fromkeys(context.addresses))
    context.labels = {address: label for address in context.addresses if (label := lookup(chain_id, address))}
    return context


def _read_configuration(context: SafeApprovalContext, chain_id: int) -> None:
    """Read current Safe configuration without making payload decoding depend on RPC."""
    try:
        client = ChainManager.get_client(Chain.from_chain_id(chain_id))
        contract = client.get_contract(context.safe, _SAFE_ABI)
        with client.batch_requests() as batch:
            batch.add(contract.functions.getOwners())
            batch.add(contract.functions.getThreshold())
            batch.add(contract.functions.nonce())
            owners, threshold, nonce = client.execute_batch(batch)
        current_owners = [str(to_checksum_address(owner)) for owner in owners]
        current_threshold, current_nonce = int(threshold), int(nonce)
        context.owners = current_owners
        context.threshold, context.nonce = current_threshold, current_nonce
    except Exception:  # noqa: BLE001 - current configuration is optional
        pass


def resolve_safe_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[SafeApprovalContext]:
    """Enrich approveHash only, for every protocol on configured Safe networks.

    Deduplicate and bound lookups. Cache found payloads, but refresh Safe state
    and retry missing records on later alerts.
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
    """Describe configuration changes that remain possible against current state."""
    tx, call = context.transaction, context.call
    if not tx or not call or tx["operation"] != 0 or tx["to"].lower() != context.safe.lower():
        return []
    if context.nonce is not None and tx["nonce"] < context.nonce:
        # A transaction with a consumed nonce cannot run through execTransaction.
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
            f"If executed against current configuration: add owner {owner}; threshold {context.threshold} -> {threshold}; owners {len(context.owners)} -> {count} ({threshold}-of-{count})."
        ]
    if call.function_name == "changeThreshold" and context.owners is not None:
        threshold, count = int(params[0]), len(context.owners)
        if threshold < 1 or threshold > count:
            return [
                f"The proposed threshold {threshold} is invalid for {count} owners; execution would revert against this state."
            ]
        return [f"If executed against current configuration: change threshold {context.threshold} -> {threshold}."]
    return []


def _context_lines(contexts: list[SafeApprovalContext]) -> list[str]:
    """Render compact facts for both the prompt and report."""
    lines = []
    for context in contexts:
        lines.extend([f"Receiving Safe: {context.safe}", f"Approved hash: {context.approved_hash}"])
        if not context.transaction:
            lines.append(f"Payload unresolved: {context.reason} Downstream impact unknown.")
            continue
        tx = context.transaction
        lines.append(
            f"Referenced transaction (Safe transaction service): to={tx['to']}; value={tx['value']} wei; operation={'CALL' if tx['operation'] == 0 else 'DELEGATECALL'}; nonce={tx['nonce']}."
        )
        if context.call:
            lines.append(f"Referenced decoded call: {context.call.signature}; arguments={context.call.params!r}.")
        elif tx["data"] != "0x":
            lines.append(f"Referenced calldata (undecoded): {tx['data']}")
        if context.owners is not None:
            lines.append(
                f"Current receiving Safe: {context.threshold}-of-{len(context.owners)}; nonce={context.nonce}."
            )
            nonce_consumed = context.nonce is not None and tx["nonce"] < context.nonce
            if nonce_consumed:
                lines.append(
                    f"Referenced nonce {tx['nonce']} is already consumed (current nonce {context.nonce}). "
                    "The referenced transaction can no longer execute through normal Safe execution."
                )
            elif context.nonce is not None and tx["nonce"] > context.nonce:
                lines.append(
                    f"Referenced nonce {tx['nonce']} is queued behind current nonce {context.nonce}; "
                    "configuration may change before it can execute."
                )
            lines.extend(_effect_lines(context))
    return lines


def format_safe_prompt(contexts: list[SafeApprovalContext]) -> str:
    """Tell the model to explain the approved action prospectively."""
    return "\n".join(
        [
            "approveHash records an owner's approval on the receiving Safe; it does not execute the referenced transaction.",
            "Explain the intended action below, conditional on separate execution. A consumed nonce cannot execute.",
            *_context_lines(contexts),
        ]
    )


def format_safe_report(contexts: list[SafeApprovalContext], chain_id: int, labels: dict[str, str]) -> str:
    """Show the referenced action with linked addresses, without prompt instructions."""
    text = "\n".join(["This approval does not execute the referenced transaction.", *_context_lines(contexts)])
    text = re.sub(
        r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])",
        lambda match: address_link(match[0], chain_id, labels),
        text,
    )
    return "### Safe Approval Context\n\n" + "\n\n".join(text.splitlines())
