from decimal import Decimal

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.config import Config
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

CUSD = "0xcCcc62962d17b8914c62D74FfB843d73B2a3cccC"
PROTOCOL = "cap"
logger = get_logger(PROTOCOL)

CUSD_DECIMALS = 18
MINT_THRESHOLD_PERCENT = Decimal(Config.get_env("CUSD_LARGE_MINT_THRESHOLD_PERCENT", "0.05"))
CACHE_KEY_LAST_SUPPLY = f"{PROTOCOL}_large_mints_last_supply"

ALERT_THRESHOLD = 15_000_000


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_units(raw_value: int) -> Decimal:
    return Decimal(raw_value) / (Decimal(10) ** CUSD_DECIMALS)


def main():
    client = ChainManager.get_client(Chain.MAINNET)
    ctoken = client.eth.contract(address=CUSD, abi=load_abi("cap/abi/CToken.json"))  # aka cusd

    assets = ctoken.functions.assets().call()

    # Batch 1: resolve vault addresses for each asset
    with client.batch_requests() as batch:
        for asset in assets:
            batch.add(ctoken.functions.fractionalReserveVault(asset))
        vault_addresses = batch.execute()

    # Batch 2: for each asset, get vault maxWithdraw for CUSD owner, token balance, decimals, and symbol
    with client.batch_requests() as batch:
        for asset, vault_addr in zip(assets, vault_addresses):
            vault = client.eth.contract(address=vault_addr, abi=load_abi("cap/abi/YearnV3Vault.json"))
            token = client.eth.contract(address=asset, abi=load_abi("common-abi/ERC20.json"))
            batch.add(vault.functions.maxWithdraw(CUSD))
            batch.add(token.functions.balanceOf(CUSD))
            batch.add(token.functions.decimals())
            batch.add(token.functions.symbol())
        responses = batch.execute()

    # Parse batched results (4 entries per asset)
    lines = []
    total_normalized = 0
    for i in range(0, len(responses), 4):
        vault_withdrawable = responses[i] or 0
        direct_balance = responses[i + 1] or 0
        decimals = responses[i + 2] if responses[i + 2] is not None else 18
        symbol = responses[i + 3] or "UNKNOWN"

        total_units = int(vault_withdrawable) + int(direct_balance)

        divisor = 10 ** int(decimals)
        normalized = total_units / divisor if divisor else 0
        line = f"{symbol}: {normalized:,.6f}"
        logger.info("%s", line)
        total_normalized += normalized
        lines.append(line)

    if total_normalized < ALERT_THRESHOLD:
        message = "🔻 CAP Withdrawable Liquidity (Mainnet)\n" + "\n".join(lines)
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    # --- cUSD Large Mint Monitoring (No Event Scanning) ---
    current_supply_raw = int(ctoken.functions.totalSupply().call())
    last_supply_cached = _to_int(get_last_value_for_key_from_file(cache_filename, CACHE_KEY_LAST_SUPPLY))
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

    write_last_value_to_file(cache_filename, CACHE_KEY_LAST_SUPPLY, current_supply_raw)


if __name__ == "__main__":
    logger.info("Running liquidity checks for CAP protocol")
    main()
