"""
3Jane USD3/sUSD3 monitoring script.

3Jane is a credit-based money market on Ethereum built as a modified Morpho Blue fork.
USD3 is the senior tranche ERC-4626 vault backed by USDC deposits.
sUSD3 is the junior (first-loss) tranche created by staking USD3.

Monitors:
- PPS (Price Per Share) for USD3 and sUSD3 — alerts on any decrease
- TVL (Total Value Locked) via totalAssets() — alerts on >15% change
- Junior tranche buffer — alerts when sUSD3 coverage drops below threshold
- Insurance fund — alerts on waUSDC outflows of at least $50k
- Withdraw liquidity — alerts when USD3 availableWithdrawLimit falls below $4M
- Vault shutdown status — alerts once if either vault enters emergency shutdown
- Debt cap changes — alerts when ProtocolConfig debt cap is modified
- Nominal sUSD3 backing floor — alerts on change and when floor > sUSD3 backing
- Protocol-wide pause — alerts once when ProtocolConfig IS_PAUSED flips to true
"""

from web3 import Web3

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_path, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.formatting import format_usd
from utils.logger import get_logger
from utils.telegram import escape_markdown
from utils.web3_wrapper import ChainManager

PROTOCOL = "3jane"
logger = get_logger(PROTOCOL)

CACHE_FILENAME = cache_path("cache-id.txt")

# --- ABIs ---
ABI_VAULT = load_abi("protocols/3jane/abi/ERC4626Vault.json")
ABI_PROTOCOL_CONFIG = load_abi("protocols/3jane/abi/ProtocolConfig.json")

# --- Contract Addresses ---
USD3_ADDRESS = "0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc"
SUSD3_ADDRESS = "0xf689555121e529Ff0463e191F9Bd9d1E496164a7"
PROTOCOL_CONFIG_ADDRESS = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"
WAUSDC_ADDRESS = "0xD4fa2D31b7968E448877f69A96DE69f5de8cD23E"
INSURANCE_FUND_ADDRESS = "0x4507B5B23340D248457d955a211C8B0634D29935"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# USDC has 6 decimals, USD3 and sUSD3 inherit this
DECIMALS = 6
ONE_SHARE = 10**DECIMALS
RATE_SCALE = 10**18

# --- Cache Keys ---
CACHE_KEY_USD3_PPS = "3JANE_USD3_PPS"
CACHE_KEY_SUSD3_PPS = "3JANE_SUSD3_PPS"
CACHE_KEY_USD3_TVL = "3JANE_USD3_TVL"
CACHE_KEY_SUSD3_TVL = "3JANE_SUSD3_TVL"
CACHE_KEY_SHUTDOWN_USD3 = "3JANE_SHUTDOWN_USD3"
CACHE_KEY_SHUTDOWN_SUSD3 = "3JANE_SHUTDOWN_SUSD3"
CACHE_KEY_DEBT_CAP = "3JANE_DEBT_CAP"
CACHE_KEY_NOMINAL_FLOOR = "3JANE_NOMINAL_FLOOR"
CACHE_KEY_FLOOR_BREACH = "3JANE_FLOOR_BREACH"
CACHE_KEY_IS_PAUSED = "3JANE_IS_PAUSED"
CACHE_KEY_INSURANCE_FUND_SHARES = "3JANE_INSURANCE_FUND_SHARES"

# --- ProtocolConfig keys (keccak256 of the string label) ---
CFG_KEY_SUSD3_NOMINAL_BACKING_FLOOR = Web3.keccak(text="SUSD3_NOMINAL_BACKING_FLOOR")
CFG_KEY_IS_PAUSED = Web3.keccak(text="IS_PAUSED")

# --- Thresholds ---
TVL_CHANGE_THRESHOLD = 0.15  # 15% TVL change alert
JUNIOR_BUFFER_THRESHOLD = 0.15  # Alert when sUSD3 backing < 15% of deployed credit
INSURANCE_FUND_OUTFLOW_THRESHOLD = 50_000  # USDC
WITHDRAW_LIMIT_THRESHOLD = 4_000_000  # USDC, alert when USD3 availableWithdrawLimit falls below


def get_cache_value(key: str) -> float:
    """Read a cached float value, returns 0.0 if not found."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def get_cache_int(key: str) -> int:
    """Read an integer cache value without passing it through float."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return int(val)
    except (ValueError, TypeError):
        # Accept values written by the previous implementation as "123.0".
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return 0


def set_cache_value(key: str, value: int | float) -> None:
    """Write a numeric value to cache."""
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
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))

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
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

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
            send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))

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
            send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))

    if susd3_tvl != previous_susd3_tvl:
        set_cache_value(CACHE_KEY_SUSD3_TVL, susd3_tvl)


def check_junior_buffer(susd3_backing: float, deployed_credit: float) -> None:
    """Check if sUSD3 junior tranche provides adequate first-loss coverage.

    The sUSD3 junior tranche absorbs losses before the senior USD3 tranche.
    A thin buffer means USD3 holders are closer to bearing losses directly.
    This matches the protocol's backing metric: sUSD3 backing value divided by
    deployed credit. The caller supplies both values converted to USDC.

    Args:
        susd3_backing: USD3 held by sUSD3, valued in USDC.
        deployed_credit: Borrowed waUSDC in the credit market, converted to USDC.
    """
    if deployed_credit <= 0:
        return

    buffer_ratio = susd3_backing / deployed_credit
    logger.info(
        "Junior buffer ratio: %.2f%% (sUSD3 backing: %s / deployed credit: %s)",
        buffer_ratio * 100,
        format_usd(susd3_backing),
        format_usd(deployed_credit),
    )

    if buffer_ratio < JUNIOR_BUFFER_THRESHOLD:
        message = (
            f"⚠️ *3Jane Junior Buffer Low*\n"
            f"📊 sUSD3 buffer: {buffer_ratio:.2%} of deployed credit\n"
            f"💰 sUSD3 backing: {format_usd(susd3_backing)} | Deployed: {format_usd(deployed_credit)}\n"
            f"⚠️ First-loss coverage is thin — USD3 holders at higher risk\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_insurance_fund(
    previous_shares: int,
    current_shares: int,
    current_assets: float,
    outflow_assets: float,
) -> None:
    """Alert when the insurance fund loses at least $50k of waUSDC shares.

    The raw share balance is cached so normal waUSDC appreciation cannot hide an
    outflow. The caller values both the current balance and share delta in USDC.
    """
    logger.info(
        "Insurance fund — balance: %s USDC, shares: %d (previous: %d)",
        format_usd(current_assets),
        current_shares,
        previous_shares,
    )

    if outflow_assets >= INSURANCE_FUND_OUTFLOW_THRESHOLD:
        message = (
            f"🚨 *3Jane Insurance Fund Outflow*\n"
            f"📉 Outflow: {format_usd(outflow_assets)}\n"
            f"💰 Remaining balance: {format_usd(current_assets)}\n"
            f"⚠️ First-loss insurance available for debt settlement decreased\n"
            f"🔗 [Insurance Fund](https://etherscan.io/address/{INSURANCE_FUND_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    if current_shares != previous_shares:
        set_cache_value(CACHE_KEY_INSURANCE_FUND_SHARES, current_shares)


def check_withdraw_limit(withdraw_limit: float) -> None:
    """Alert when USD3 withdraw liquidity drops below the safety threshold.

    availableWithdrawLimit is the USDC the USD3 vault can immediately honor for
    withdrawals. When it falls below the threshold, withdrawals may queue or
    stall, signalling a liquidity squeeze on the senior tranche.

    Args:
        withdraw_limit: USD3 availableWithdrawLimit in USDC.
    """
    logger.info("USD3 available withdraw limit: %s", format_usd(withdraw_limit))

    if withdraw_limit < WITHDRAW_LIMIT_THRESHOLD:
        message = (
            f"🚨 *3Jane USD3 Withdraw Liquidity Low*\n"
            f"📉 Available withdraw limit: {format_usd(withdraw_limit)} "
            f"(threshold {format_usd(WITHDRAW_LIMIT_THRESHOLD)})\n"
            f"⚠️ Senior-tranche withdrawals may queue or stall\n"
            f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


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
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))
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
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))
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
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))

    if debt_cap != previous_debt_cap:
        set_cache_value(CACHE_KEY_DEBT_CAP, debt_cap)


def check_nominal_backing_floor(nominal_floor: float, susd3_backing: float) -> None:
    """Check ProtocolConfig SUSD3_NOMINAL_BACKING_FLOOR.

    The nominal floor is an absolute USDC amount of sUSD3 backing the protocol
    requires (in addition to the ratio-based floor). When set above current
    sUSD3 backing valued in USDC, sUSD3 redemptions can be blocked.

    Sends two distinct alerts:
    - Any change to the floor value (governance lever).
    - Transition from "floor <= backing" to "floor > backing" (active breach).

    Args:
        nominal_floor: Current SUSD3_NOMINAL_BACKING_FLOOR in USDC.
        susd3_backing: Current sUSD3 backing value in USDC.
    """
    # --- Alert on any change (treat first-run as a non-alert init) ---
    raw_previous = get_last_value_for_key_from_file(CACHE_FILENAME, CACHE_KEY_NOMINAL_FLOOR)
    first_run = not isinstance(raw_previous, str)
    previous_floor = float(raw_previous) if isinstance(raw_previous, str) else 0.0

    logger.info(
        "sUSD3 nominal backing floor: %s (previous: %s)",
        format_usd(nominal_floor),
        format_usd(previous_floor),
    )

    if not first_run and nominal_floor != previous_floor:
        direction = "increased" if nominal_floor > previous_floor else "decreased"
        message = (
            f"⚠️ *3Jane sUSD3 Nominal Backing Floor Change*\n"
            f"📊 Floor {direction}\n"
            f"💰 {format_usd(previous_floor)} → {format_usd(nominal_floor)}\n"
            f"ℹ️ Withdrawals blocked while sUSD3 backing < floor\n"
            f"🔗 [ProtocolConfig](https://etherscan.io/address/{PROTOCOL_CONFIG_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    if nominal_floor != previous_floor or first_run:
        set_cache_value(CACHE_KEY_NOMINAL_FLOOR, nominal_floor)

    # --- Alert-once on breach transition (floor > backing) ---
    breach = nominal_floor > susd3_backing and nominal_floor > 0
    previous_breach = get_cache_value(CACHE_KEY_FLOOR_BREACH)
    if breach and previous_breach == 0:
        shortfall = nominal_floor - susd3_backing
        message = (
            f"🚨 *3Jane sUSD3 Backing Below Nominal Floor*\n"
            f"📊 Floor: {format_usd(nominal_floor)} | sUSD3 backing: {format_usd(susd3_backing)}\n"
            f"💰 Shortfall: {format_usd(shortfall)}\n"
            f"⚠️ sUSD3 redemptions may be blocked until backing recovers\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    if float(breach) != previous_breach:
        set_cache_value(CACHE_KEY_FLOOR_BREACH, float(breach))


def check_protocol_paused(is_paused: bool) -> None:
    """Check ProtocolConfig IS_PAUSED flag.

    Separate from per-vault isShutdown(). A protocol-wide pause stops the
    underlying credit market regardless of vault shutdown state.

    Args:
        is_paused: Current IS_PAUSED value from ProtocolConfig.
    """
    logger.info("Protocol IS_PAUSED: %s", is_paused)

    previous_paused = get_cache_value(CACHE_KEY_IS_PAUSED)
    if is_paused and previous_paused == 0:
        message = (
            f"🚨 *3Jane Protocol PAUSED*\n"
            f"⚠️ ProtocolConfig IS_PAUSED flipped to true\n"
            f"🔗 [ProtocolConfig](https://etherscan.io/address/{PROTOCOL_CONFIG_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))
    if float(is_paused) != previous_paused:
        set_cache_value(CACHE_KEY_IS_PAUSED, float(is_paused))


def main() -> None:
    """Run all 3Jane monitoring checks."""
    logger.info("Starting 3Jane monitoring...")

    client = ChainManager.get_client(Chain.MAINNET)
    usd3_vault = client.eth.contract(address=USD3_ADDRESS, abi=ABI_VAULT)
    susd3_vault = client.eth.contract(address=SUSD3_ADDRESS, abi=ABI_VAULT)
    wausdc_vault = client.eth.contract(address=WAUSDC_ADDRESS, abi=ABI_VAULT)
    protocol_config = client.eth.contract(address=PROTOCOL_CONFIG_ADDRESS, abi=ABI_PROTOCOL_CONFIG)

    try:
        # Batch all core vault reads in a single RPC call
        with client.batch_requests() as batch:
            batch.add(usd3_vault.functions.totalAssets())
            batch.add(usd3_vault.functions.totalSupply())
            batch.add(usd3_vault.functions.convertToAssets(ONE_SHARE))
            batch.add(susd3_vault.functions.totalAssets())
            batch.add(susd3_vault.functions.totalSupply())
            batch.add(susd3_vault.functions.convertToAssets(ONE_SHARE))
            batch.add(usd3_vault.functions.balanceOf(SUSD3_ADDRESS))
            batch.add(usd3_vault.functions.getMarketLiquidity())
            batch.add(protocol_config.functions.config(CFG_KEY_SUSD3_NOMINAL_BACKING_FLOOR))
            batch.add(protocol_config.functions.config(CFG_KEY_IS_PAUSED))
            batch.add(wausdc_vault.functions.balanceOf(INSURANCE_FUND_ADDRESS))
            batch.add(usd3_vault.functions.availableWithdrawLimit(ZERO_ADDRESS))
            responses = client.execute_batch(batch)
            if len(responses) != 12:
                raise ValueError(f"Expected 12 responses, got {len(responses)}")

        usd3_total_assets = responses[0]
        usd3_total_supply = responses[1]
        usd3_pps_raw = responses[2]
        susd3_total_assets = responses[3]
        susd3_total_supply = responses[4]
        susd3_pps_raw = responses[5]
        susd3_usd3_balance = responses[6]
        market_liquidity = responses[7]
        nominal_floor_raw = responses[8]
        is_paused = bool(responses[9])
        insurance_fund_shares = responses[10]
        withdraw_limit_raw = responses[11]

        if len(market_liquidity) != 4:
            raise ValueError(f"Expected 4 market liquidity values, got {len(market_liquidity)}")
        total_borrow_wausdc = market_liquidity[2]
        previous_insurance_shares = get_cache_int(CACHE_KEY_INSURANCE_FUND_SHARES)
        insurance_outflow_shares = max(previous_insurance_shares - insurance_fund_shares, 0)

        # Value the USD3 shares held by sUSD3 and fetch one high-precision waUSDC
        # conversion rate. All waUSDC values below use that same rate and block.
        with client.batch_requests() as batch:
            batch.add(usd3_vault.functions.convertToAssets(susd3_usd3_balance))
            batch.add(wausdc_vault.functions.convertToAssets(RATE_SCALE))
            backing_responses = client.execute_batch(batch)
            if len(backing_responses) != 2:
                raise ValueError(f"Expected 2 backing responses, got {len(backing_responses)}")

        susd3_backing_raw = backing_responses[0]
        wausdc_assets_per_scale = backing_responses[1]
        deployed_credit_raw = total_borrow_wausdc * wausdc_assets_per_scale // RATE_SCALE
        insurance_fund_assets_raw = insurance_fund_shares * wausdc_assets_per_scale // RATE_SCALE
        insurance_outflow_assets_raw = insurance_outflow_shares * wausdc_assets_per_scale // RATE_SCALE

        # Convert to human-readable floats
        usd3_tvl = usd3_total_assets / ONE_SHARE
        usd3_supply = usd3_total_supply / ONE_SHARE
        usd3_pps = usd3_pps_raw / ONE_SHARE
        susd3_tvl = susd3_total_assets / ONE_SHARE
        susd3_supply = susd3_total_supply / ONE_SHARE
        susd3_pps = susd3_pps_raw / ONE_SHARE
        susd3_backing = susd3_backing_raw / ONE_SHARE
        deployed_credit = deployed_credit_raw / ONE_SHARE
        insurance_fund_assets = insurance_fund_assets_raw / ONE_SHARE
        insurance_outflow_assets = insurance_outflow_assets_raw / ONE_SHARE
        withdraw_limit = withdraw_limit_raw / ONE_SHARE
        nominal_floor = nominal_floor_raw / ONE_SHARE

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
        logger.info(
            "Junior backing — sUSD3: %s USDC, deployed credit: %s USDC",
            format_usd(susd3_backing),
            format_usd(deployed_credit),
        )

        # Run all checks
        check_pps(usd3_pps, susd3_pps)
        check_tvl(usd3_tvl, susd3_tvl)
        check_junior_buffer(susd3_backing, deployed_credit)
        check_insurance_fund(
            previous_insurance_shares,
            insurance_fund_shares,
            insurance_fund_assets,
            insurance_outflow_assets,
        )
        check_withdraw_limit(withdraw_limit)
        check_vault_shutdown(client, usd3_vault, susd3_vault)
        check_debt_cap(client)
        check_nominal_backing_floor(nominal_floor, susd3_backing)
        check_protocol_paused(is_paused)

        logger.info(
            "Monitoring complete — USD3 PPS: %.8f, TVL: %s | sUSD3 PPS: %.8f, TVL: %s",
            usd3_pps,
            format_usd(usd3_tvl),
            susd3_pps,
            format_usd(susd3_tvl),
        )
    except Exception as e:
        logger.error("Error during 3Jane monitoring: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"🚨 *3Jane Monitoring Error*\n❌ {escape_markdown(str(e))}", PROTOCOL))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
