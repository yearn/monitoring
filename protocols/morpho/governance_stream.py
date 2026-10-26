"""The ``MorphoGovernanceEvent`` stream followed by the continuous event subscriber.

yearn-envio indexes governance events emitted by every vault in ``config.py``
(V1 ``SubmitCap`` / ``SubmitMarketRemoval`` / ``SubmitTimelock``; V2 ``Submit`` /
``Accept`` / ``SetOwner`` / ``SetCurator`` / ``AddAdapter`` / ``RemoveAdapter``) as
one entity with ABI-named arguments in ``argsJson``. This stream observes them
with the same WebSocket + HTTP reconciliation as the timelock pilot (#447). It
sends no alerts: lifecycle rules and delivery are #448, and the daily
``governance.py`` / ``governance_v2.py`` jobs stay the alerting path.
"""

import json
from typing import Any

from protocols.morpho._shared import PROTOCOL
from protocols.morpho.config import VAULTS_V1_BY_CHAIN, VAULTS_V2_BY_CHAIN, iter_vaults
from utils.subscriber.stream import EventStream, StreamLabel, StreamScope

MORPHO_GOVERNANCE_FIELDS = """
        id
        vaultAddress
        vaultVersion
        eventName
        chainId
        blockNumber
        blockHash
        blockTimestamp
        logIndex
        transactionHash
        transactionIndex
        transactionFrom
        argsJson
"""

# Longest ``argsJson`` shown in a notice; V2 ``Submit`` carries full calldata.
_MAX_ARGS_CHARS = 600


def describe_governance_event(payload: dict[str, Any]) -> list[str]:
    """Return notice lines for one event: vault generation, sender, and its ABI-named arguments."""
    lines = [f"Vault V{payload.get('vaultVersion')}, sent by {payload.get('transactionFrom') or 'n/a'}"]
    raw = payload.get("argsJson")
    if raw:
        try:
            args = json.dumps(json.loads(raw), separators=(",", ":"), sort_keys=True)
        except TypeError, ValueError:
            args = str(raw)
        lines.append(f"Args: {args[:_MAX_ARGS_CHARS]}" + ("…" if len(args) > _MAX_ARGS_CHARS else ""))
    return lines


MORPHO_GOVERNANCE_STREAM = EventStream(
    name="morpho",
    noun="Morpho governance",
    entity="MorphoGovernanceEvent",
    address_field="vaultAddress",
    address_label="Vault",
    fields=MORPHO_GOVERNANCE_FIELDS,
    db_filename="morpho_subscriber.db",
    table_prefix="morpho_sub",
    describe=describe_governance_event,
)


def morpho_governance_scope() -> StreamScope:
    """Return every configured V1 and V2 vault, keyed by chain, with its notice label."""
    addresses: dict[int, set[str]] = {}
    labels: dict[tuple[str, int], StreamLabel] = {}
    for version, vaults_by_chain in (("V1", VAULTS_V1_BY_CHAIN), ("V2", VAULTS_V2_BY_CHAIN)):
        for chain, vault in iter_vaults(vaults_by_chain):
            address = vault.address.lower()
            addresses.setdefault(chain.chain_id, set()).add(address)
            labels[(address, chain.chain_id)] = StreamLabel(f"{vault.name} {version}", PROTOCOL)
    return StreamScope(addresses=addresses, labels=labels)
