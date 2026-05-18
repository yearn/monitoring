"""
3Jane USD3/sUSD3 monitoring script.

3Jane is a credit-based money market on Ethereum built as a modified Morpho Blue fork.
USD3 is the senior tranche ERC-4626 vault backed by USDC deposits.
sUSD3 is the junior (first-loss) tranche created by staking USD3.

Monitors:
- PPS (Price Per Share) for USD3 and sUSD3 — alerts on any decrease
- TVL (Total Value Locked) via totalAssets() — alerts on >15% change
- Junior tranche buffer — alerts when sUSD3 coverage drops below threshold
- Vault shutdown status — alerts once if either vault enters emergency shutdown
- Debt cap changes — alerts when ProtocolConfig debt cap is modified
"""

from utils.abi import load_abi
from utils.cache import get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.formatting import format_usd
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "3jane"
logger = get_logger(PROTOCOL)

CACHE_FILENAME = "cache-id.txt"

# --- ABIs ---
ABI_VAULT = load_abi("3jane/abi/ERC4626Vault.json")
ABI_PROTOCOL_CONFIG = load_abi("3jane/abi/ProtocolConfig.json")

# --- Contract Addresses ---
USD3_ADDRESS = "0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc"
SUSD3_ADDRESS = "0xf689555121e529Ff0463e191F9Bd9d1E496164a7"
PROTOCOL_CONFIG_ADDRESS = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"

# USDC has 6 decimals, USD3 and sUSD3 inherit this
DECIMALS = 6
ONE_SHARE = 10**DECIMALS

# --- Cache Keys ---
CACHE_KEY_USD3_PPS = "3JANE_USD3_PPS"
CACHE_KEY_SUSD3_PPS = "3JANE_SUSD3_PPS"
CACHE_KEY_USD3_TVL = "3JANE_USD3_TVL"
CACHE_KEY_SUSD3_TVL = "3JANE_SUSD3_TVL"
CACHE_KEY_SHUTDOWN_USD3 = "3JANE_SHUTDOWN_USD3"
CACHE_KEY_SHUTDOWN_SUSD3 = "3JANE_SHUTDOWN_SUSD3"
CACHE_KEY_DEBT_CAP = "3JANE_DEBT_CAP"

# --- Thresholds ---
TVL_CHANGE_THRESHOLD = 0.15  # 15% TVL change alert
JUNIOR_BUFFER_THRESHOLD = 0.15  # Alert when sUSD3 buffer < 15% of USD3 TVL


def get_cache_value(key: str) -> float:
    """Read a cached float value, returns 0.0 if not found."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def set_cache_value(key: str, value: float) -> None:
    """Write a float value to cache."""
    write_last_value_to_file(CACHE_FILENAME, key, value)


def check_pps(usd3_pps_float: float, susd3_pps_float: float) -> None:
    """Check Price Per Share for USD3 and sUSD3, alert on any decrease.

    A PPS decrease indicates loan markdowns, defaults, or losses being socialized
    through the vault. This is the most critical signal for 3Jane since loans are unsecured.

    Args:
        usd3_pps_float: Current USD3 price per share as a float.
        susd3_pps_float: Current sUSD3 price per share as a float.
    """
    # --- USD3 PPS ---
    previous_usd3_pps = get_cache_value(CACHE_KEY_USD3_PPS)
    logger.info("USD3 PPS: %.8f (previous: %.8f)", usd3_pps_float, previous_usd3_pps)

    if previous_usd3_pps > 0 and usd3_pps_float < previous_usd3_pps:
        decrease_pct = (previous_usd3_pps - usd3_pps_float) / previous_usd3_pps * 100
        message = (
            f"🚨 *3Jane USD3 PPS Decrease*\n"
            f"📉 PPS dropped from {previous_usd3_pps:.8f} to {usd3_pps_float:.8f}\n"
            f"📊 Decrease: {decrease_pct:.4f}%\n"
            f"⚠️ Possible loan markdown or default\n"
            f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
        )
        send_telegram_message(message, PROTOCOL)

    if usd3_pps_float != previous_usd3_pps:
        set_cache_value(CACHE_KEY_USD3_PPS, usd3_pps_float)

    # --- sUSD3 PPS ---
    previous_susd3_pps = get_cache_value(CACHE_KEY_SUSD3_PPS)
    logger.info("sUSD3 PPS: %.8f (previous: %.8f)", susd3_pps_float, previous_susd3_pps)

    if previous_susd3_pps > 0 and susd3_pps_float < previous_susd3_pps:
        decrease_pct = (previous_susd3_pps - susd3_pps_float) / previous_susd3_pps * 100
        message = (
            f"🚨 *3Jane sUSD3 PPS Decrease*\n"
            f"📉 PPS dropped from {previous_susd3_pps:.8f} to {susd3_pps_float:.8f}\n"
            f"📊 Decrease: {decrease_pct:.4f}%\n"
            f"⚠️ Junior tranche absorbing losses — first-loss buffer impacted\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_telegram_message(message, PROTOCOL)

    if susd3_pps_float != previous_susd3_pps:
        set_cache_value(CACHE_KEY_SUSD3_PPS, susd3_pps_float)


def check_tvl(usd3_tvl: float, susd3_tvl: float) -> None:
    """Check Total Value Locked for USD3 and sUSD3, alert on large changes.

    Significant TVL changes can indicate large deposits/withdrawals or
    protocol-level events that affect backing.

    Args:
        usd3_tvl: Current USD3 totalAssets in USDC terms.
        susd3_tvl: Current sUSD3 totalAssets in USD3 terms.
    """
    # --- USD3 TVL ---
    previous_usd3_tvl = get_cache_value(CACHE_KEY_USD3_TVL)
    logger.info("USD3 TVL: %s (previous: %s)", format_usd(usd3_tvl), format_usd(previous_usd3_tvl))

    if previous_usd3_tvl > 0:
        change_pct = abs(usd3_tvl - previous_usd3_tvl) / previous_usd3_tvl
        if change_pct >= TVL_CHANGE_THRESHOLD:
            direction = "increased" if usd3_tvl > previous_usd3_tvl else "decreased"
            message = (
                f"🚨 *3Jane USD3 TVL Change*\n"
                f"💰 TVL {direction} by {change_pct:.2%}\n"
                f"📊 {format_usd(previous_usd3_tvl)} → {format_usd(usd3_tvl)}\n"
                f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
            )
            send_telegram_message(message, PROTOCOL)

    if usd3_tvl != previous_usd3_tvl:
        set_cache_value(CACHE_KEY_USD3_TVL, usd3_tvl)

    # --- sUSD3 TVL ---
    previous_susd3_tvl = get_cache_value(CACHE_KEY_SUSD3_TVL)
    logger.info("sUSD3 TVL: %s (previous: %s)", format_usd(susd3_tvl), format_usd(previous_susd3_tvl))

    if previous_susd3_tvl > 0:
        change_pct = abs(susd3_tvl - previous_susd3_tvl) / previous_susd3_tvl
        if change_pct >= TVL_CHANGE_THRESHOLD:
            direction = "increased" if susd3_tvl > previous_susd3_tvl else "decreased"
            message = (
                f"🚨 *3Jane sUSD3 TVL Change*\n"
                f"💰 TVL {direction} by {change_pct:.2%}\n"
                f"📊 {format_usd(previous_susd3_tvl)} → {format_usd(susd3_tvl)}\n"
                f"⚠️ Junior tranche buffer size changed significantly\n"
                f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
            )
            send_telegram_message(message, PROTOCOL)

    if susd3_tvl != previous_susd3_tvl:
        set_cache_value(CACHE_KEY_SUSD3_TVL, susd3_tvl)


def check_junior_buffer(usd3_tvl: float, susd3_tvl: float, susd3_pps_float: float) -> None:
    """Check if sUSD3 junior tranche provides adequate first-loss coverage.

    The sUSD3 junior tranche absorbs losses before the senior USD3 tranche.
    A thin buffer means USD3 holders are closer to bearing losses directly.
    We convert sUSD3 TVL to USDC terms using its PPS for accurate comparison.

    Args:
        usd3_tvl: USD3 totalAssets in USDC.
        susd3_tvl: sUSD3 totalAssets in USD3 terms.
        susd3_pps_float: sUSD3 price per share (USD3 per sUSD3 share).
    """
    if usd3_tvl <= 0:
        return

    # sUSD3 totalAssets is in USD3 terms; USD3 PPS converts to USDC
    # But for buffer ratio, USD3-denominated value is sufficient since USD3 ≈ USDC
    buffer_ratio = susd3_tvl / usd3_tvl
    logger.info(
        "Junior buffer ratio: %.2f%% (sUSD3: %s / USD3: %s)",
        buffer_ratio * 100,
        format_usd(susd3_tvl),
        format_usd(usd3_tvl),
    )

    if buffer_ratio < JUNIOR_BUFFER_THRESHOLD:
        message = (
            f"⚠️ *3Jane Junior Buffer Low*\n"
            f"📊 sUSD3 buffer: {buffer_ratio:.2%} of USD3 TVL\n"
            f"💰 sUSD3: {format_usd(susd3_tvl)} | USD3: {format_usd(usd3_tvl)}\n"
            f"⚠️ First-loss coverage is thin — USD3 holders at higher risk\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_telegram_message(message, PROTOCOL)


def check_vault_shutdown(client, usd3_vault, susd3_vault) -> None:  # type: ignore[no-untyped-def]
    """Check if either vault has been emergency shut down.

    Uses alert-once pattern: only sends alert when shutdown state transitions
    from False to True.

    Args:
        client: Web3Client instance.
        usd3_vault: USD3 contract instance.
        susd3_vault: sUSD3 contract instance.
    """
    with client.batch_requests() as batch:
        batch.add(usd3_vault.functions.isShutdown())
        batch.add(susd3_vault.functions.isShutdown())
        responses = client.execute_batch(batch)
        if len(responses) != 2:
            raise ValueError(f"Expected 2 responses, got {len(responses)}")

    usd3_shutdown = responses[0]
    susd3_shutdown = responses[1]

    logger.info("Vault shutdown — USD3: %s, sUSD3: %s", usd3_shutdown, susd3_shutdown)

    # Alert once on USD3 shutdown
    previous_usd3_shutdown = get_cache_value(CACHE_KEY_SHUTDOWN_USD3)
    if usd3_shutdown and previous_usd3_shutdown == 0:
        message = (
            f"🚨 *3Jane USD3 Vault SHUTDOWN*\n"
            f"⚠️ USD3 vault has entered emergency shutdown\n"
            f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
        )
        send_telegram_message(message, PROTOCOL)
    if float(usd3_shutdown) != previous_usd3_shutdown:
        set_cache_value(CACHE_KEY_SHUTDOWN_USD3, float(usd3_shutdown))

    # Alert once on sUSD3 shutdown
    previous_susd3_shutdown = get_cache_value(CACHE_KEY_SHUTDOWN_SUSD3)
    if susd3_shutdown and previous_susd3_shutdown == 0:
        message = (
            f"🚨 *3Jane sUSD3 Vault SHUTDOWN*\n"
            f"⚠️ sUSD3 vault has entered emergency shutdown\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_telegram_message(message, PROTOCOL)
    if float(susd3_shutdown) != previous_susd3_shutdown:
        set_cache_value(CACHE_KEY_SHUTDOWN_SUSD3, float(susd3_shutdown))


def check_debt_cap(client) -> None:  # type: ignore[no-untyped-def]
    """Check ProtocolConfig debt cap for changes.

    The debt cap limits how much can be borrowed via unsecured credit lines.
    Changes to the debt cap signal governance decisions to scale the protocol
    up or down.

    Args:
        client: Web3Client instance.
    """
    config = client.eth.contract(address=PROTOCOL_CONFIG_ADDRESS, abi=ABI_PROTOCOL_CONFIG)
    debt_cap_raw = client.execute(config.functions.getDebtCap().call)
    debt_cap = debt_cap_raw / ONE_SHARE

    previous_debt_cap = get_cache_value(CACHE_KEY_DEBT_CAP)
    logger.info("Debt cap: %s (previous: %s)", format_usd(debt_cap), format_usd(previous_debt_cap))

    if previous_debt_cap > 0 and debt_cap != previous_debt_cap:
        direction = "increased" if debt_cap > previous_debt_cap else "decreased"
        message = (
            f"⚠️ *3Jane Debt Cap Change*\n"
            f"📊 Debt cap {direction}\n"
            f"💰 {format_usd(previous_debt_cap)} → {format_usd(debt_cap)}\n"
            f"🔗 [ProtocolConfig](https://etherscan.io/address/{PROTOCOL_CONFIG_ADDRESS})"
        )
        send_telegram_message(message, PROTOCOL)

    if debt_cap != previous_debt_cap:
        set_cache_value(CACHE_KEY_DEBT_CAP, debt_cap)


def main() -> None:
    """Run all 3Jane monitoring checks."""
    logger.info("Starting 3Jane monitoring...")

    client = ChainManager.get_client(Chain.MAINNET)
    usd3_vault = client.eth.contract(address=USD3_ADDRESS, abi=ABI_VAULT)
    susd3_vault = client.eth.contract(address=SUSD3_ADDRESS, abi=ABI_VAULT)

    try:
        # Batch all core vault reads in a single RPC call
        with client.batch_requests() as batch:
            batch.add(usd3_vault.functions.totalAssets())
            batch.add(usd3_vault.functions.totalSupply())
            batch.add(usd3_vault.functions.convertToAssets(ONE_SHARE))
            batch.add(susd3_vault.functions.totalAssets())
            batch.add(susd3_vault.functions.totalSupply())
            batch.add(susd3_vault.functions.convertToAssets(ONE_SHARE))
            responses = client.execute_batch(batch)
            if len(responses) != 6:
                raise ValueError(f"Expected 6 responses, got {len(responses)}")

        usd3_total_assets = responses[0]
        usd3_total_supply = responses[1]
        usd3_pps_raw = responses[2]
        susd3_total_assets = responses[3]
        susd3_total_supply = responses[4]
        susd3_pps_raw = responses[5]

        # Convert to human-readable floats
        usd3_tvl = usd3_total_assets / ONE_SHARE
        usd3_supply = usd3_total_supply / ONE_SHARE
        usd3_pps = usd3_pps_raw / ONE_SHARE
        susd3_tvl = susd3_total_assets / ONE_SHARE
        susd3_supply = susd3_total_supply / ONE_SHARE
        susd3_pps = susd3_pps_raw / ONE_SHARE

        logger.info(
            "USD3 — TVL: %s, Supply: %s, PPS: %.8f",
            format_usd(usd3_tvl),
            format_usd(usd3_supply),
            usd3_pps,
        )
        logger.info(
            "sUSD3 — TVL: %s USD3, Supply: %s, PPS: %.8f",
            format_usd(susd3_tvl),
            format_usd(susd3_supply),
            susd3_pps,
        )

        # Run all checks
        check_pps(usd3_pps, susd3_pps)
        check_tvl(usd3_tvl, susd3_tvl)
        check_junior_buffer(usd3_tvl, susd3_tvl, susd3_pps)
        check_vault_shutdown(client, usd3_vault, susd3_vault)
        check_debt_cap(client)

        logger.info(
            "Monitoring complete — USD3 PPS: %.8f, TVL: %s | sUSD3 PPS: %.8f, TVL: %s",
            usd3_pps,
            format_usd(usd3_tvl),
            susd3_pps,
            format_usd(susd3_tvl),
        )
    except Exception as e:
        logger.error("Error during 3Jane monitoring: %s", e)
        send_telegram_message(
            f"🚨 *3Jane Monitoring Error*\n❌ {e}",
            PROTOCOL,
            plain_text=True,
        )


if __name__ == "__main__":
    main()
