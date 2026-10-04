from decimal import Decimal, localcontext

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    DAILY_CACHE_STALE_AFTER_SECONDS,
    cache_filename,
    get_fresh_last_value_for_key_from_file,
    write_last_value_with_timestamp_to_file,
)
from utils.chains import Chain
from utils.config import Config
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

CUSD = "0xcCcc62962d17b8914c62D74FfB843d73B2a3cccC"
PROTOCOL = "cap"
logger = get_logger(PROTOCOL)

CUSD_DECIMALS = 18
MINT_THRESHOLD_PERCENT = Decimal(Config.get_env("CUSD_LARGE_MINT_THRESHOLD_PERCENT", "0.05"))
CACHE_KEY_LAST_SUPPLY = f"{PROTOCOL}_large_mints_last_supply"

HIGH_LIQUIDITY_THRESHOLD = Decimal("0.10")
CRITICAL_LIQUIDITY_THRESHOLD = Decimal("0.03")


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_units(raw_value: int) -> Decimal:
    return Decimal(raw_value) / (Decimal(10) ** CUSD_DECIMALS)


def main():
    client = ChainManager.get_client(Chain.MAINNET)
    block_number = int(client.eth.block_number)
    ctoken = client.eth.contract(address=CUSD, abi=load_abi("protocols/cap/abi/CToken.json"))  # aka cusd

    assets = ctoken.functions.assets().call(block_identifier=block_number)

    # Batch 1: resolve vault addresses for each asset
    with client.batch_requests() as batch:
        for asset in assets:
            batch.add(ctoken.functions.fractionalReserveVault(asset).call(block_identifier=block_number))
        vault_addresses = batch.execute()

    # Batch 2: read withdrawable liquidity and total supplied assets at the same block.
    with client.batch_requests() as batch:
        for asset, vault_addr in zip(assets, vault_addresses):
            vault = client.eth.contract(address=vault_addr, abi=load_abi("protocols/cap/abi/YearnV3Vault.json"))
            token = client.eth.contract(address=asset, abi=load_abi("common-abi/ERC20.json"))
            batch.add(vault.functions.maxWithdraw(CUSD).call(block_identifier=block_number))
            batch.add(token.functions.balanceOf(CUSD).call(block_identifier=block_number))
            batch.add(token.functions.decimals().call(block_identifier=block_number))
            batch.add(token.functions.symbol().call(block_identifier=block_number))
            batch.add(ctoken.functions.totalSupplies(asset).call(block_identifier=block_number))
        responses = batch.execute()

    # Keep uint256 amounts precise even when other monitors lower the global Decimal precision.
    with localcontext(prec=100):
        # Parse batched results (5 entries per asset), valuing normalized backing assets at par.
        lines = []
        total_normalized = Decimal(0)
        total_tvl = Decimal(0)
        for i in range(0, len(responses), 5):
            vault_withdrawable = responses[i] or 0
            direct_balance = responses[i + 1] or 0
            decimals = responses[i + 2] if responses[i + 2] is not None else 18
            symbol = responses[i + 3] or "UNKNOWN"
            total_supplied = responses[i + 4]
            if total_supplied is None:
                raise RuntimeError(f"CAP liquidity RPC returned no value for {symbol} totalSupplies")

            total_units = int(vault_withdrawable) + int(direct_balance)

            divisor = Decimal(10) ** int(decimals)
            normalized = Decimal(total_units) / divisor
            line = f"{symbol}: {normalized:,.6f}"
            logger.info("%s", line)
            total_normalized += normalized
            total_tvl += Decimal(int(total_supplied)) / divisor
            lines.append(line)

        if total_tvl > 0:
            liquidity_percent = total_normalized / total_tvl * Decimal(100)
            logger.info("CAP withdrawable liquidity: %s%% of total TVL %s", liquidity_percent, total_tvl)
            severity = None
            if total_normalized < total_tvl * CRITICAL_LIQUIDITY_THRESHOLD:
                severity = AlertSeverity.CRITICAL
            elif total_normalized < total_tvl * HIGH_LIQUIDITY_THRESHOLD:
                severity = AlertSeverity.HIGH

            if severity is not None:
                message = (
                    "🔻 CAP Withdrawable Liquidity (Mainnet)\n"
                    + "\n".join(lines)
                    + f"\nTotal withdrawable: {total_normalized:,.6f}\n"
                    f"Total TVL: {total_tvl:,.6f}\n"
                    f"Withdrawable / TVL: {liquidity_percent:,.2f}%"
                )
                send_alert(Alert(severity, message, PROTOCOL))
        else:
            logger.warning("CAP total TVL is zero; skipping withdrawable liquidity ratio check")

    # --- cUSD Large Mint Monitoring (No Event Scanning) ---
    current_supply_raw = int(ctoken.functions.totalSupply().call(block_identifier=block_number))
    last_supply_cached = _to_int(
        get_fresh_last_value_for_key_from_file(cache_filename, CACHE_KEY_LAST_SUPPLY, DAILY_CACHE_STALE_AFTER_SECONDS)
    )
    if last_supply_cached > 0:
        delta_raw = current_supply_raw - last_supply_cached
        threshold_raw = int(last_supply_cached * MINT_THRESHOLD_PERCENT)
        if delta_raw >= threshold_raw:
            delta = _format_units(delta_raw)
            previous = _format_units(last_supply_cached)
            current = _format_units(current_supply_raw)
            threshold_tokens = _format_units(int(last_supply_cached * MINT_THRESHOLD_PERCENT))
            threshold_percent_display = MINT_THRESHOLD_PERCENT * Decimal(100)

            msg = (
                "*cUSD Large Mint Alert (Supply Delta)*\n\n"
                f"Threshold: {threshold_percent_display:,.2f}% of totalSupply "
                f"(~{threshold_tokens:,.2f} cUSD at previous supply)\n"
                f"Supply increase: {delta:,.2f} cUSD\n"
                f"Previous totalSupply: {previous:,.2f}\n"
                f"Current totalSupply: {current:,.2f}\n\n"
                "This monitor intentionally uses only totalSupply deltas (no event scanning)."
            )
            send_alert(Alert(AlertSeverity.LOW, msg, PROTOCOL))

    write_last_value_with_timestamp_to_file(cache_filename, CACHE_KEY_LAST_SUPPLY, current_supply_raw)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    logger.info("Running liquidity checks for CAP protocol")
    run_with_alert(main, PROTOCOL)
