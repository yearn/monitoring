"""Generic ERC20 large-transfer monitoring helper.

This module provides a reusable, incremental Transfer-event scanner:
- scans only from cached block + 1 to latest
- chunks eth_getLogs requests to control RPC usage
- alerts on transfer values above a configurable token threshold
"""

from dataclasses import dataclass
from decimal import Decimal, getcontext

from web3 import Web3

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

getcontext().prec = 40

logger = get_logger("utils.erc20_large_transfer_monitor")

TRANSFER_TOPIC0 = Web3.keccak(text="Transfer(address,address,uint256)").hex()


@dataclass(frozen=True)
class ERC20LargeTransferMonitorConfig:
    protocol: str
    chain: Chain
    token_address: str
    threshold_tokens: Decimal
    chunk_size_blocks: int = 2_000
    first_run_lookback_blocks: int = 2_000
    cache_suffix: str = "large_transfers"
    alert_severity: AlertSeverity = AlertSeverity.MEDIUM
    alert_label: str = "Large Transfer Alert"
    monitor_note: str = "This monitor scans Transfer logs incrementally using cached block state."


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_units(raw_value: int, decimals: int) -> Decimal:
    return Decimal(raw_value) / (Decimal(10) ** decimals)


def _cache_key_last_block(config: ERC20LargeTransferMonitorConfig) -> str:
    return f"{config.protocol}_{config.cache_suffix}_last_block"


def _topic_to_address(topic) -> str:
    topic_hex = topic.hex() if hasattr(topic, "hex") else str(topic)
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def _build_tx_link(chain: Chain, tx_hash: str) -> str:
    if chain.explorer_url:
        return f"{chain.explorer_url}/tx/{tx_hash}"
    return tx_hash


def _build_alert_message(
    *,
    config: ERC20LargeTransferMonitorConfig,
    token_symbol: str,
    from_addr: str,
    to_addr: str,
    value_raw: int,
    decimals: int,
    block_number: int,
    tx_hash: str,
) -> str:
    threshold = config.threshold_tokens
    value = _format_units(value_raw, decimals)
    tx_link = _build_tx_link(config.chain, tx_hash)
    return (
        f"*{token_symbol} {config.alert_label}*\n\n"
        f"Threshold: {threshold:,.0f} {token_symbol}\n"
        f"Transfer amount: {value:,.2f} {token_symbol}\n"
        f"From: `{from_addr}`\n"
        f"To: `{to_addr}`\n"
        f"Block: {block_number}\n"
        f"Tx: {tx_link}\n\n"
        f"{config.monitor_note}"
    )


def _emit_large_transfer_alerts_for_logs(
    *,
    config: ERC20LargeTransferMonitorConfig,
    logs,
    token_symbol: str,
    threshold_raw: int,
    decimals: int,
) -> None:
    for log in logs:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue

        value_raw = int(log["data"].hex(), 16)
        if value_raw < threshold_raw:
            continue

        from_addr = _topic_to_address(topics[1])
        to_addr = _topic_to_address(topics[2])
        tx_hash = log["transactionHash"].hex()
        block_number = int(log["blockNumber"])

        send_alert(
            Alert(
                config.alert_severity,
                _build_alert_message(
                    config=config,
                    token_symbol=token_symbol,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    value_raw=value_raw,
                    decimals=decimals,
                    block_number=block_number,
                    tx_hash=tx_hash,
                ),
                config.protocol,
            )
        )


def run_erc20_large_transfer_monitor(config: ERC20LargeTransferMonitorConfig) -> None:
    client = ChainManager.get_client(config.chain)
    erc20_abi = load_abi("common-abi/ERC20.json")
    token_addr = Web3.to_checksum_address(config.token_address)
    token = client.get_contract(token_addr, erc20_abi)

    try:
        with client.batch_requests() as batch:
            batch.add(token.functions.decimals())
            batch.add(token.functions.symbol())
            decimals, token_symbol = client.execute_batch(batch)

        decimals = int(decimals)
        token_symbol = str(token_symbol)
        threshold_raw = int(config.threshold_tokens * (Decimal(10) ** decimals))

        latest_block = int(client.eth.block_number)
        last_block_cached = _to_int(get_last_value_for_key_from_file(cache_filename, _cache_key_last_block(config)))
        if last_block_cached > 0:
            start_block = last_block_cached + 1
        else:
            start_block = max(latest_block - max(config.first_run_lookback_blocks, 0) + 1, 0)

        if start_block > latest_block:
            write_last_value_to_file(cache_filename, _cache_key_last_block(config), latest_block)
            return

        chunk_size = max(config.chunk_size_blocks, 1)
        for from_block in range(start_block, latest_block + 1, chunk_size):
            to_block = min(from_block + chunk_size - 1, latest_block)
            logs = client.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": token_addr,
                    "topics": [TRANSFER_TOPIC0],
                }
            )
            _emit_large_transfer_alerts_for_logs(
                config=config,
                logs=logs,
                token_symbol=token_symbol,
                threshold_raw=threshold_raw,
                decimals=decimals,
            )
            write_last_value_to_file(cache_filename, _cache_key_last_block(config), to_block)

    except Exception as exc:
        logger.error("ERC20 large transfer monitor failed for %s: %s", config.protocol, exc)
        send_alert(
            Alert(
                config.alert_severity,
                f"ERC20 large transfer monitor failed for {config.protocol}: {exc}",
                config.protocol,
            ),
            plain_text=True,
        )
