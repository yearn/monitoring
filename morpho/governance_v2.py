"""Morpho VaultV2 timelock + role-change monitor.

V2 timelocks store pending changes in a per-calldata mapping
(``executableAt[bytes data]``) that cannot be enumerated, so we replay
``Submit`` / ``Accept`` / ``Revoke`` events from each vault and each
``MorphoMarketV1AdapterV2`` adapter, decoding the embedded calldata into a
human-readable Telegram alert.

Owner-controlled, *non*-timelocked changes (``SetOwner``, ``SetCurator``,
``SetIsSentinel``) are alerted on first sighting since they take effect
instantly and can hand control of the vault to an unexpected actor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from web3 import Web3

from morpho._shared import get_vault_url
from morpho.markets_v2 import (
    ABI_MARKET_ADAPTER,
    ABI_VAULT_V2,
    ADAPTER_KIND_MARKET,
    AdapterInfo,
    V2Vault,
    classify_adapter,
    discover_v2_vaults_by_chain,
    list_adapters,
)
from morpho.v2_decoders import decode_submit, selector_function_name, submit_data_key
from utils.cache import (
    get_last_executed_morpho_from_file,
    get_last_processed_block,
    write_last_executed_morpho_to_file,
    write_last_processed_block,
)
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "morpho"
logger = get_logger("morpho.governance_v2")

# Cache value-type tags used with utils.cache.morpho_key.
SUBMIT_TYPE = "v2_submit"
INSTANT_TYPE = "v2_instant"

# Default lookback per chain when no last_processed_block is recorded.
# Conservative ~24h windows; the actual cadence is daily so this only matters
# on first run.
LOOKBACK_BLOCKS_BY_CHAIN: dict[Chain, int] = {
    Chain.MAINNET: 8000,
    Chain.BASE: 50000,
    Chain.KATANA: 50000,
    Chain.POLYGON: 50000,
}
DEFAULT_LOOKBACK_BLOCKS = 50000

# Block range chunk size for eth_getLogs to stay within RPC limits.
LOG_CHUNK_SIZE = 5000

# Events polled on the VaultV2 contract.
_VAULT_TIMELOCK_EVENTS = ("Submit", "Accept", "Revoke")
_VAULT_INSTANT_EVENTS = ("SetOwner", "SetCurator", "SetIsSentinel")
_VAULT_AUDIT_EVENTS = (
    "AddAdapter",
    "RemoveAdapter",
    "IncreaseTimelock",
    "DecreaseTimelock",
    "Abdicate",
)

# Events polled on each MorphoMarketV1AdapterV2 contract.
_ADAPTER_TIMELOCK_EVENTS = ("Submit", "Accept", "Revoke")
_ADAPTER_AUDIT_EVENTS = ("IncreaseTimelock", "DecreaseTimelock", "Abdicate")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _vault_url(vault: V2Vault) -> str:
    return get_vault_url(vault.address, vault.chain)


def _explorer_link(chain: Chain, tx_hash: str) -> str:
    base = chain.explorer_url
    if not base:
        return tx_hash
    return f"[{tx_hash[:10]}…]({base}/tx/{tx_hash})"


def _coerce_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return bytes.fromhex(value[2:] if value.startswith("0x") else value)
    raise TypeError(f"Cannot coerce {type(value)!r} to bytes")


def _get_logs(
    client: Web3Client, contract: Any, event_name: str, from_block: int, to_block: int
) -> tuple[list[Any], int]:
    """Fetch logs for one event in chunks to respect RPC range limits.

    Returns ``(logs, last_successful_block)``. ``last_successful_block`` is the
    highest block fully covered by a successful chunk; if a chunk fails, the
    caller MUST NOT advance its checkpoint past that value or it will silently
    drop alerts. Returns ``(_, from_block - 1)`` if the very first chunk fails.
    """
    if from_block > to_block:
        return [], to_block
    event = contract.events[event_name]
    out: list[Any] = []
    cursor = from_block
    last_successful = from_block - 1
    while cursor <= to_block:
        end = min(cursor + LOG_CHUNK_SIZE - 1, to_block)
        try:
            chunk = list(event.get_logs(from_block=cursor, to_block=end))
        except Exception as e:
            logger.warning(
                "get_logs %s failed for %s [%d-%d]: %s — checkpoint will not advance",
                event_name,
                contract.address,
                cursor,
                end,
                e,
            )
            return out, last_successful
        out.extend(chunk)
        last_successful = end
        cursor = end + 1
    return out, last_successful


def _resolve_lookback(chain: Chain) -> int:
    return LOOKBACK_BLOCKS_BY_CHAIN.get(chain, DEFAULT_LOOKBACK_BLOCKS)


# ----------------------------------------------------------------------------
# Alerting
# ----------------------------------------------------------------------------


def _alert_submit(vault: V2Vault, emitter: str, source_label: str, log: Any) -> None:
    """Telegram alert for a freshly submitted timelocked operation.

    ``emitter`` is the contract that emitted the Submit event (vault or adapter).
    Including it in the cache key prevents two adapters submitting identical
    calldata from masking each other.
    """
    args = log["args"]
    data = _coerce_bytes(args["data"])
    executable_at = int(args["executableAt"])
    decoded = decode_submit(data)
    cache_key_id = submit_data_key(data)
    last = get_last_executed_morpho_from_file(emitter.lower(), cache_key_id, SUBMIT_TYPE)
    if last == executable_at or last == -1:
        # Already alerted (and possibly executed) — skip.
        return

    ts = _format_ts(executable_at)
    tx_link = _explorer_link(vault.chain, log["transactionHash"].hex())
    send_telegram_message(
        f"⏳ V2 [{vault.name}]({_vault_url(vault)}) {source_label} on {vault.chain.name}\n"
        f"📥 Submitted: `{decoded}`\n"
        f"⏰ Executable at: {ts}\n"
        f"🔗 Tx: {tx_link}",
        PROTOCOL,
    )
    write_last_executed_morpho_to_file(emitter.lower(), cache_key_id, SUBMIT_TYPE, executable_at)


def _alert_accept(vault: V2Vault, emitter: str, source_label: str, log: Any) -> None:
    args = log["args"]
    data = _coerce_bytes(args["data"])
    decoded = decode_submit(data)
    cache_key_id = submit_data_key(data)
    last = get_last_executed_morpho_from_file(emitter.lower(), cache_key_id, SUBMIT_TYPE)
    if last == -1:
        return
    tx_link = _explorer_link(vault.chain, log["transactionHash"].hex())
    send_telegram_message(
        f"✅ V2 [{vault.name}]({_vault_url(vault)}) {source_label} on {vault.chain.name}\n"
        f"🔓 Executed: `{decoded}`\n"
        f"🔗 Tx: {tx_link}",
        PROTOCOL,
    )
    write_last_executed_morpho_to_file(emitter.lower(), cache_key_id, SUBMIT_TYPE, -1)


def _alert_revoke(vault: V2Vault, emitter: str, source_label: str, log: Any) -> None:
    args = log["args"]
    data = _coerce_bytes(args["data"])
    decoded = decode_submit(data)
    cache_key_id = submit_data_key(data)
    last = get_last_executed_morpho_from_file(emitter.lower(), cache_key_id, SUBMIT_TYPE)
    if last == 0:
        return
    sender = args.get("sender", "?")
    tx_link = _explorer_link(vault.chain, log["transactionHash"].hex())
    send_telegram_message(
        f"⚠️ V2 [{vault.name}]({_vault_url(vault)}) {source_label} on {vault.chain.name}\n"
        f"🛑 Revoked by `{sender}`: `{decoded}`\n"
        f"🔗 Tx: {tx_link}",
        PROTOCOL,
    )
    write_last_executed_morpho_to_file(emitter.lower(), cache_key_id, SUBMIT_TYPE, 0)


def _alert_instant(vault: V2Vault, emitter: str, event_name: str, log: Any) -> None:
    """Alert for owner-controlled instant changes (no timelock)."""
    tx_hash = log["transactionHash"].hex()
    instant_id = f"{tx_hash}+{log.get('logIndex', 0)}"
    last = get_last_executed_morpho_from_file(emitter.lower(), instant_id, INSTANT_TYPE)
    if last:
        return
    args = log["args"]
    if event_name == "SetOwner":
        body = f"👑 New owner: `{args['newOwner']}`"
    elif event_name == "SetCurator":
        body = f"🎩 New curator: `{args['newCurator']}`"
    elif event_name == "SetIsSentinel":
        body = f"🛡️ Sentinel `{args['account']}` set to {bool(args['newIsSentinel'])}"
    else:
        body = f"{event_name}: {dict(args)}"

    tx_link = _explorer_link(vault.chain, tx_hash)
    send_telegram_message(
        f"🚨 V2 [{vault.name}]({_vault_url(vault)}) instant change on {vault.chain.name}\n{body}\n🔗 Tx: {tx_link}",
        PROTOCOL,
    )
    write_last_executed_morpho_to_file(emitter.lower(), instant_id, INSTANT_TYPE, 1)


def _alert_audit(vault: V2Vault, emitter: str, source_label: str, event_name: str, log: Any) -> None:
    """Lightweight informational alert for audit events that are useful but not critical."""
    tx_hash = log["transactionHash"].hex()
    instant_id = f"{tx_hash}+{log.get('logIndex', 0)}+{event_name}"
    last = get_last_executed_morpho_from_file(emitter.lower(), instant_id, INSTANT_TYPE)
    if last:
        return
    args = log["args"]
    if event_name in ("AddAdapter", "RemoveAdapter"):
        verb = "added" if event_name == "AddAdapter" else "removed"
        body = f"🧩 Adapter `{args['account']}` {verb}"
    elif event_name in ("IncreaseTimelock", "DecreaseTimelock"):
        sel = _coerce_bytes(args["selector"])
        sel_name = selector_function_name(sel) or f"0x{sel.hex()}"
        verb = "increased" if event_name == "IncreaseTimelock" else "decreased"
        body = f"⏱️ Timelock {verb} for `{sel_name}` → {int(args['newDuration'])}s"
    elif event_name == "Abdicate":
        sel = _coerce_bytes(args["selector"])
        sel_name = selector_function_name(sel) or f"0x{sel.hex()}"
        body = f"🪦 Abdicated `{sel_name}` (function permanently disabled)"
    else:
        body = f"{event_name}: {dict(args)}"

    tx_link = _explorer_link(vault.chain, tx_hash)
    send_telegram_message(
        f"ℹ️ V2 [{vault.name}]({_vault_url(vault)}) {source_label} on {vault.chain.name}\n{body}\n🔗 Tx: {tx_link}",
        PROTOCOL,
    )
    write_last_executed_morpho_to_file(emitter.lower(), instant_id, INSTANT_TYPE, 1)


# ----------------------------------------------------------------------------
# Per-target processing
# ----------------------------------------------------------------------------


def _process_target(
    client: Web3Client,
    vault: V2Vault,
    address: str,
    abi: list[dict[str, Any]],
    label: str,
    timelock_events: Iterable[str],
    audit_events: Iterable[str],
    instant_events: Iterable[str],
    chain_id: int,
    latest_block: int,
) -> None:
    """Replay events on a single contract (vault or adapter) and alert on each.

    The checkpoint only advances to ``min(last_successful_block)`` across all
    polled events. If any chunk fails, the next run replays the failed range so
    no Submit / Accept / Revoke is silently dropped.
    """
    contract = client.get_contract(Web3.to_checksum_address(address), abi)

    last_block = get_last_processed_block(address, chain_id)
    if last_block <= 0:
        last_block = max(0, latest_block - _resolve_lookback(vault.chain))
    from_block = last_block + 1
    if from_block > latest_block:
        return

    safe_checkpoints: list[int] = []

    for event_name in timelock_events:
        logs, last_ok = _get_logs(client, contract, event_name, from_block, latest_block)
        safe_checkpoints.append(last_ok)
        for log in logs:
            try:
                if event_name == "Submit":
                    _alert_submit(vault, address, label, log)
                elif event_name == "Accept":
                    _alert_accept(vault, address, label, log)
                elif event_name == "Revoke":
                    _alert_revoke(vault, address, label, log)
            except Exception as e:
                logger.exception("Failed to process %s event on %s: %s", event_name, address, e)

    for event_name in audit_events:
        logs, last_ok = _get_logs(client, contract, event_name, from_block, latest_block)
        safe_checkpoints.append(last_ok)
        for log in logs:
            try:
                _alert_audit(vault, address, label, event_name, log)
            except Exception as e:
                logger.exception("Failed to process audit %s on %s: %s", event_name, address, e)

    for event_name in instant_events:
        logs, last_ok = _get_logs(client, contract, event_name, from_block, latest_block)
        safe_checkpoints.append(last_ok)
        for log in logs:
            try:
                _alert_instant(vault, address, event_name, log)
            except Exception as e:
                logger.exception("Failed to process instant %s on %s: %s", event_name, address, e)

    if not safe_checkpoints:
        return
    new_checkpoint = min(safe_checkpoints)
    if new_checkpoint > last_block:
        write_last_processed_block(address, chain_id, new_checkpoint)
    else:
        logger.warning(
            "Not advancing checkpoint for %s on chain %d: log fetch failed in range [%d, %d]",
            address,
            chain_id,
            from_block,
            latest_block,
        )


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def process_vault(client: Web3Client, vault: V2Vault, latest_block: int) -> None:
    """Process timelock + role events on a vault and on each market-v1 adapter."""
    chain_id = vault.chain.chain_id

    _process_target(
        client,
        vault,
        vault.address,
        ABI_VAULT_V2,
        label="vault",
        timelock_events=_VAULT_TIMELOCK_EVENTS,
        audit_events=_VAULT_AUDIT_EVENTS,
        instant_events=_VAULT_INSTANT_EVENTS,
        chain_id=chain_id,
        latest_block=latest_block,
    )

    # Discover adapters on-chain and process timelock events on the market-v1 ones.
    adapter_addresses = list_adapters(client, vault.address)
    for adapter_addr in adapter_addresses:
        info: AdapterInfo = classify_adapter(client, adapter_addr)
        if info.kind != ADAPTER_KIND_MARKET:
            continue
        _process_target(
            client,
            vault,
            info.address,
            ABI_MARKET_ADAPTER,
            label=f"adapter `{info.address}`",
            timelock_events=_ADAPTER_TIMELOCK_EVENTS,
            audit_events=_ADAPTER_AUDIT_EVENTS,
            instant_events=(),
            chain_id=chain_id,
            latest_block=latest_block,
        )


def main() -> None:
    """Discover Yearn-relevant V2 vaults and replay governance events on each."""
    logger.info("Checking Morpho V2 governance...")
    vaults_by_chain = discover_v2_vaults_by_chain()
    if not any(vaults_by_chain.values()):
        logger.info("No matching V2 vaults found; nothing to monitor yet.")
        return

    for chain, vaults in vaults_by_chain.items():
        if not vaults:
            continue
        client = ChainManager.get_client(chain)
        latest_block = client.eth.block_number
        for vault in vaults:
            try:
                process_vault(client, vault, latest_block)
            except Exception as e:
                logger.exception("Failed to process governance for %s on %s: %s", vault.address, chain.name, e)


if __name__ == "__main__":
    main()
