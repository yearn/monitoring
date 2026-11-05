"""Decide what the daily Morpho governance jobs would alert for newly indexed events.

The indexer has no revoke/accept events for V1 and no ``Revoke`` for V2, so an
event alone cannot say whether its operation is still pending. Each event is
therefore a trigger: the vault's *current* on-chain state is read (at a block at
or after the event) and fed to the same check functions the daily
``governance.py`` / ``governance_v2.py`` jobs run. That gives the live path the
daily jobs' content, severity, grouping, loss classification, and dedup cache
(an operation the daily job already alerted is not alerted again), and an
operation revoked or executed before the read is simply not pending anymore.

Cache writes buffered by those checks are returned as ``VaultEvaluation.commit``
and never applied here. In observation and shadow mode nobody calls it, so the
daily jobs stay the only writers and keep alerting exactly as before; production
mode calls it once the alert is delivered, like the daily jobs do. Resolutions
(``Accept``; executed/revoked LOW notes) are left to the daily jobs.

V1: ``SubmitCap`` / ``SubmitMarketRemoval`` → that market's pending cap and
removal checks; ``SubmitTimelock`` → the timelock (and guardian) check.
V2: ``Submit`` → ``executableAt(data)`` must still be set, then the pending
operation diff (loss alerts included); ``SetOwner`` / ``SetCurator`` → role diff;
``AddAdapter`` / ``RemoveAdapter`` → adapter set diff.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from web3 import Web3

from protocols.morpho import governance, governance_v2
from protocols.morpho._alerts import VaultAlert, VaultDiff
from protocols.morpho.config import VAULTS_V1_BY_CHAIN, VAULTS_V2_BY_CHAIN, VaultConfig
from protocols.morpho.governance_v2 import PendingConfig, V2GovernanceSnapshot
from protocols.morpho.v2_decoders import selector_function_name
from utils.abi import load_abi
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.logger import get_logger

logger = get_logger("morpho.live_rules")

ABI_VAULT_V2 = load_abi("protocols/morpho/abi/vault_v2.json")
# Any V2 adapter exposes realAssets(); this ABI carries it.
ABI_ADAPTER = load_abi("protocols/morpho/abi/morpho_vault_v1_adapter.json")

V1_MARKET_EVENTS = frozenset({"SubmitCap", "SubmitMarketRemoval"})
RESOLUTION_EVENTS = frozenset({"Accept"})

# Per-event outcome reasons recorded in the decision log.
REASON_ALERTED = "alerted"
REASON_NOT_NEW = "nothing new: no longer pending, or already alerted by the daily job"
REASON_RESOLUTION = "resolution: left to the daily job"
REASON_UNSUPPORTED = "event not handled by the live rules"


class RpcBehindError(Exception):
    """The RPC node has not reached the event's block yet; retry shortly."""


@dataclass(frozen=True)
class GovernanceEvent:
    """One indexed ``MorphoGovernanceEvent`` occurrence."""

    event_id: str
    chain_id: int
    vault: str  # checksummed
    version: int
    name: str
    block_number: int
    transaction_hash: str
    args: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GovernanceEvent":
        """Build from an inbox row's stored Envio payload."""
        try:
            args = json.loads(payload.get("argsJson") or "{}")
        except TypeError, ValueError:
            args = {}
        return cls(
            event_id=str(payload["id"]),
            chain_id=int(payload["chainId"]),
            vault=str(Web3.to_checksum_address(payload["vaultAddress"])),
            version=int(payload.get("vaultVersion") or 0),
            name=str(payload.get("eventName") or ""),
            block_number=int(payload["blockNumber"]),
            transaction_hash=str(payload.get("transactionHash") or ""),
            args=args if isinstance(args, dict) else {},
        )


@dataclass
class VaultEvaluation:
    """What the daily jobs would alert for one vault's batch of events."""

    header: str
    alerts: list[VaultAlert] = field(default_factory=list)
    # event_id -> REASON_* explaining the event's outcome
    reasons: dict[str, str] = field(default_factory=dict)
    # Applies the daily jobs' cache writes for these alerts; call only after delivery.
    commit: Callable[[], None] = field(default=lambda: None, compare=False)


def vault_config(chain: Chain, address: str, version: int) -> VaultConfig | None:
    """Return the configured vault for ``address`` on ``chain`` and generation ``version``."""
    vaults = (VAULTS_V1_BY_CHAIN if version == 1 else VAULTS_V2_BY_CHAIN).get(chain, ())
    return next((v for v in vaults if v.address.lower() == address.lower()), None)


def _current_block(client: Any, events: list[GovernanceEvent]) -> int:
    """Return the RPC head, or raise ``RpcBehindError`` if it trails the newest event."""
    block = int(client.eth.block_number)
    newest = max(e.block_number for e in events)
    if block < newest:
        raise RpcBehindError(f"RPC head {block} is behind event block {newest}")
    return block


def _hex_bytes(value: Any) -> bytes:
    """Decode a ``bytes`` or ``bytes32`` argument from ``argsJson`` (0x hex)."""
    return bytes.fromhex(str(value).removeprefix("0x"))


def _attribute(diff: VaultDiff, before: int, ids: list[str], reasons: dict[str, str]) -> None:
    """Record ``ids`` as alerted when the check added sections since ``before``, else as not new."""
    reason = REASON_ALERTED if len(diff.alerts) > before else REASON_NOT_NEW
    for event_id in ids:
        reasons[event_id] = reason


def evaluate_v1(client: Any, chain: Chain, vault: VaultConfig, events: list[GovernanceEvent]) -> VaultEvaluation:
    """Run the daily V1 checks the events point at, against the vault's current state."""
    block = _current_block(client, events)
    contract = client.eth.contract(address=Web3.to_checksum_address(vault.address), abi=governance.ABI_MORPHO)
    diff = VaultDiff()
    result = VaultEvaluation(header=governance.vault_header(vault.name, vault.address, chain))

    by_market: dict[bytes, list[str]] = {}
    timelock_ids: list[str] = []
    for event in events:
        if event.name in V1_MARKET_EVENTS and event.args.get("id"):
            by_market.setdefault(_hex_bytes(event.args["id"]), []).append(event.event_id)
        elif event.name == "SubmitTimelock":
            timelock_ids.append(event.event_id)
        else:
            result.reasons[event.event_id] = REASON_UNSUPPORTED

    if by_market:
        states = governance.load_market_governance_states(contract, list(by_market), client, block)
        for market_id, state in zip(by_market, states, strict=True):
            before = len(diff.alerts)
            # One check per market: a SubmitCap and a SubmitMarketRemoval on the same
            # market in one batch must not render its sections twice.
            governance.check_market_governance_state(vault.name, state, chain, diff)
            _attribute(diff, before, by_market[market_id], result.reasons)
    if timelock_ids:
        before = len(diff.alerts)
        governance.check_timelock_and_guardian(contract, client, diff, block)
        _attribute(diff, before, timelock_ids, result.reasons)

    result.alerts = diff.alerts
    result.commit = diff.commit
    return result


def load_v2_snapshot(client: Any, chain: Chain, vault: VaultConfig, block: int) -> V2GovernanceSnapshot:
    """Read the parts of a V2 vault's governance snapshot the live rules need, on-chain.

    The Morpho API lags the chain, so the daily job's GraphQL snapshot cannot be
    used for a seconds-old event. Sentinels and allocators are mappings (not
    enumerable on-chain) and are not needed: no indexed event changes them.
    """
    address = Web3.to_checksum_address(vault.address)
    contract = client.eth.contract(address=address, abi=ABI_VAULT_V2)
    with client.batch_requests() as batch:
        batch.add(contract.functions.owner().call(block_identifier=block))
        batch.add(contract.functions.curator().call(block_identifier=block))
        batch.add(contract.functions.asset().call(block_identifier=block))
        batch.add(contract.functions.adaptersLength().call(block_identifier=block))
        owner, curator, asset, length = client.execute_batch(batch)
    adapters: list[str] = []
    if length:
        with client.batch_requests() as batch:
            for index in range(int(length)):
                batch.add(contract.functions.adapters(index).call(block_identifier=block))
            adapters = [str(Web3.to_checksum_address(a)) for a in client.execute_batch(batch)]

    # Adapter valuations decide whether a removal is a potential loss, so a failed
    # read must not look like an empty adapter: it raises, and the vault is retried.
    adapter_assets: dict[str, int] = {}
    if adapters:
        with client.batch_requests() as batch:
            for adapter in adapters:
                batch.add(_adapter(client, adapter).functions.realAssets().call(block_identifier=block))
            adapter_assets = {a: int(v) for a, v in zip(adapters, client.execute_batch(batch), strict=True)}

    metadata = fetch_erc20_metadata(chain.chain_id, str(asset))
    return V2GovernanceSnapshot(
        name=vault.name,
        address=str(address),
        chain=chain,
        owner=str(Web3.to_checksum_address(owner)),
        curator=str(Web3.to_checksum_address(curator)),
        sentinels=[],
        allocators=[],
        adapters=sorted(adapters),
        asset_symbol=metadata.symbol if metadata else "",
        asset_decimals=metadata.decimals if metadata else None,
        total_assets=_try_call(contract.functions.totalAssets(), block),
        adapter_assets=adapter_assets,
    )


def _adapter(client: Any, address: str) -> Any:
    """Return an adapter contract exposing ``realAssets()``."""
    return client.eth.contract(address=address, abi=ABI_ADAPTER)


def _try_call(function: Any, block: int) -> int | None:
    """Best-effort integer view call for display-only values; None when it reverts (e.g. an IRM that reverts)."""
    try:
        return int(function.call(block_identifier=block))
    except Exception as e:  # noqa: BLE001 - a reverting valuation only drops the amount from the alert
        logger.warning("view call failed, alert omits the amount: %s", e)
        return None


def evaluate_v2(client: Any, chain: Chain, vault: VaultConfig, events: list[GovernanceEvent]) -> VaultEvaluation:
    """Run the daily V2 diff for the events' operations, roles, and adapters, against current state."""
    block = _current_block(client, events)
    snapshot = load_v2_snapshot(client, chain, vault, block)
    diff = VaultDiff()
    result = VaultEvaluation(header=governance_v2.vault_header(snapshot))

    submits = [e for e in events if e.name == "Submit" and e.args.get("data")]
    if submits:
        contract = client.eth.contract(address=snapshot.address, abi=ABI_VAULT_V2)
        datas = [_hex_bytes(e.args["data"]) for e in submits]
        with client.batch_requests() as batch:
            for data in datas:
                batch.add(contract.functions.executableAt(data).call(block_identifier=block))
            executable_at = client.execute_batch(batch)
        new_pending: list[tuple[PendingConfig, str]] = []
        for event, data, valid_at in zip(submits, datas, executable_at, strict=True):
            if not valid_at:
                result.reasons[event.event_id] = REASON_NOT_NEW  # accepted or revoked already
                continue
            pc = PendingConfig(
                valid_at=int(valid_at),
                function_name=selector_function_name(data[:4]) or "",
                data=data,
                tx_hash=event.transaction_hash,
            )
            before = len(diff.alerts)
            new = governance_v2.diff_pending_config(snapshot, pc, diff)
            if new is not None:
                new_pending.append(new)
            result.reasons[event.event_id] = (
                REASON_ALERTED if new is not None or len(diff.alerts) > before else REASON_NOT_NEW
            )
        # Same grouping as the daily job: one section for every new non-loss submission.
        governance_v2.alert_pending_new(snapshot, new_pending, diff)

    checks: list[tuple[set[str], Callable[[], None]]] = [
        ({"SetOwner"}, lambda: governance_v2.diff_single_role(snapshot, "owner", snapshot.owner, diff)),
        ({"SetCurator"}, lambda: governance_v2.diff_single_role(snapshot, "curator", snapshot.curator, diff)),
        (
            {"AddAdapter", "RemoveAdapter"},
            lambda: governance_v2.diff_set(snapshot, "adapters", snapshot.adapters, diff),
        ),
    ]
    for names, check in checks:
        ids = [e.event_id for e in events if e.name in names]
        if ids:
            before = len(diff.alerts)
            check()
            _attribute(diff, before, ids, result.reasons)

    for event in events:
        if event.event_id not in result.reasons:
            result.reasons[event.event_id] = (
                REASON_RESOLUTION if event.name in RESOLUTION_EVENTS else REASON_UNSUPPORTED
            )
    result.alerts = diff.alerts
    result.commit = diff.commit
    return result
