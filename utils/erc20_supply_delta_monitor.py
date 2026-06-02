"""Generic ERC20 supply-delta monitoring helper.

This module provides a reusable way to monitor large supply increases without
event scanning. It is intended for protocols that only need cached
`totalSupply` deltas on a fixed schedule.
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

logger = get_logger("utils.erc20_supply_delta_monitor")


@dataclass(frozen=True)
class ERC20SupplyDeltaMonitorConfig:
    protocol: str
    chain: Chain
    token_address: str
    threshold_percent: Decimal
    cache_suffix: str = "large_mints"
    alert_severity: AlertSeverity = AlertSeverity.LOW
    alert_label: str = "Large Mint Alert (Supply Delta)"
    monitor_note: str = "This monitor intentionally uses only totalSupply deltas (no event scanning)."


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_units(raw_value: int, decimals: int) -> Decimal:
    return Decimal(raw_value) / (Decimal(10) ** decimals)


def _cache_key_last_supply(config: ERC20SupplyDeltaMonitorConfig) -> str:
    return f"{config.protocol}_{config.cache_suffix}_last_supply"


def _build_alert_message(
    *,
    config: ERC20SupplyDeltaMonitorConfig,
    token_symbol: str,
    previous_raw: int,
    current_raw: int,
    delta_raw: int,
    decimals: int,
) -> str:
    previous = _format_units(previous_raw, decimals)
    current = _format_units(current_raw, decimals)
    delta = _format_units(delta_raw, decimals)
    threshold_tokens = _format_units(int(previous_raw * config.threshold_percent), decimals)
    threshold_percent_display = config.threshold_percent * Decimal(100)
    return (
        f"*{token_symbol} {config.alert_label}*\n\n"
        f"Threshold: {threshold_percent_display:,.2f}% of totalSupply "
        f"(~{threshold_tokens:,.2f} {token_symbol} at previous supply)\n"
        f"Supply increase: {delta:,.2f} {token_symbol}\n"
        f"Previous totalSupply: {previous:,.2f}\n"
        f"Current totalSupply: {current:,.2f}\n\n"
        f"{config.monitor_note}"
    )


def run_erc20_supply_delta_monitor(config: ERC20SupplyDeltaMonitorConfig) -> None:
    client = ChainManager.get_client(config.chain)
    erc20_abi = load_abi("common-abi/ERC20.json")
    token_addr = Web3.to_checksum_address(config.token_address)
    token = client.get_contract(token_addr, erc20_abi)

    try:
        with client.batch_requests() as batch:
            batch.add(token.functions.decimals())
            batch.add(token.functions.symbol())
            decimals, token_symbol = client.execute_batch(batch)

        current_supply_raw = int(token.functions.totalSupply().call())
        last_supply_cached = _to_int(get_last_value_for_key_from_file(cache_filename, _cache_key_last_supply(config)))

        if last_supply_cached > 0:
            delta_raw = current_supply_raw - last_supply_cached
            threshold_raw = int(last_supply_cached * config.threshold_percent)
            if delta_raw >= threshold_raw:
                send_alert(
                    Alert(
                        config.alert_severity,
                        _build_alert_message(
                            config=config,
                            token_symbol=str(token_symbol),
                            previous_raw=last_supply_cached,
                            current_raw=current_supply_raw,
                            delta_raw=delta_raw,
                            decimals=int(decimals),
                        ),
                        config.protocol,
                    )
                )

        write_last_value_to_file(cache_filename, _cache_key_last_supply(config), current_supply_raw)

    except Exception as exc:
        logger.error("ERC20 supply-delta monitor failed for %s: %s", config.protocol, exc)
        send_alert(
            Alert(
                config.alert_severity,
                f"ERC20 supply-delta monitor failed for {config.protocol}: {exc}",
                config.protocol,
            ),
            plain_text=True,
        )
