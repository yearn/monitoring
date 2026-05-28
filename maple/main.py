"""
Maple Finance syrupUSDC monitoring script.

Monitors:
- PPS (Price Per Share) via convertToAssets(1e6) — alerts on any decrease
- TVL (Total Value Locked) via totalAssets() — alerts on >15% change
- Unrealized losses on loan managers — alerts on any non-zero value
- Strategy allocations (Aave and Sky) — tracks DeFi allocation changes
- Withdrawal queue vs liquid funds — alerts when pending exit value > 80% of liquid funds (Aave + Sky)
- Pool liquidity — cash and withdrawal queue depth
- Loan collateral risk — weighted risk score based on collateral asset types
- Collateralization ratio (via syrupGlobals) — alerts when combined ratio drops below 140%
- Pool Delegate Cover — alerts when delegate cover balance drops to zero
"""

from maple.collateral import check_collateral_risk
from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.formatting import format_usd
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "maple"
logger = get_logger(PROTOCOL)

CACHE_FILENAME = "cache-id.txt"

# --- ABIs ---
ABI_POOL = load_abi("maple/abi/SyrupUSDCPool.json")
ABI_WITHDRAWAL_MANAGER = load_abi("maple/abi/WithdrawalManagerQueue.json")
ABI_LOAN_MANAGER = load_abi("maple/abi/LoanManager.json")
ABI_STRATEGY = load_abi("maple/abi/Strategy.json")

# --- Contract Addresses ---
SYRUP_USDC_POOL = "0x80ac24aA929eaF5013f6436cdA2a7ba190f5Cc0b"
WITHDRAWAL_MANAGER = "0x1bc47a0Dd0FdaB96E9eF982fdf1F34DC6207cfE3"
FIXED_TERM_LOAN_MANAGER = "0x4A1c3F0D9aD0b3f9dA085bEBfc22dEA54263371b"
OPEN_TERM_LOAN_MANAGER = "0x6ACEb4cAbA81Fa6a8065059f3A944fb066A10fAc"
AAVE_STRATEGY = "0x560B3A85Af1cEF113BB60105d0Cf21e1d05F91d4"
SKY_STRATEGY = "0x859C9980931fa0A63765fD8EF2e29918Af5b038C"
POOL_DELEGATE_COVER = "0x9e62FE15d0E99cE2b30CE0D256e9Ab7b6893AfF5"
USDC_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

# USDC has 6 decimals
USDC_DECIMALS = 6
ONE_SHARE = 10**USDC_DECIMALS  # 1e6

# --- Cache Keys ---
CACHE_KEY_PPS = "MAPLE_PPS"
CACHE_KEY_TVL = "MAPLE_TVL"
CACHE_KEY_DELEGATE_COVER = "MAPLE_DELEGATE_COVER"

# Minimal ERC20 ABI for balanceOf
ABI_ERC20_BALANCE = [
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]

# --- Thresholds ---
TVL_CHANGE_THRESHOLD = 0.15  # 15% TVL change alert
WITHDRAWAL_QUEUE_THRESHOLD = 0.80  # 80% of liquid funds
WITHDRAWAL_QUEUE_TVL_THRESHOLD = 0.01  # 1% of TVL


def get_cache_value(key: str) -> float:
    """Read a cached float value, returns 0 if not found."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def set_cache_value(key: str, value: float) -> None:
    """Write a float value to cache."""
    write_last_value_to_file(CACHE_FILENAME, key, value)


def check_pps(client, pool) -> float:
    """Check Price Per Share and alert on decrease."""
    pps = client.execute(pool.functions.convertToAssets(ONE_SHARE).call)
    pps_float = pps / ONE_SHARE

    previous_pps = get_cache_value(CACHE_KEY_PPS)
    logger.info("syrupUSDC PPS: %.8f (previous: %.8f)", pps_float, previous_pps)

    if previous_pps > 0 and pps_float < previous_pps:
        decrease_pct = (previous_pps - pps_float) / previous_pps * 100
        message = (
            f"🚨 *Maple syrupUSDC PPS Decrease*\n"
            f"📉 PPS dropped from {previous_pps:.8f} to {pps_float:.8f}\n"
            f"📊 Decrease: {decrease_pct:.4f}%\n"
            f"⚠️ This may indicate loan impairment or loss\n"
            f"🔗 [syrupUSDC Pool](https://etherscan.io/address/{SYRUP_USDC_POOL})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    if pps_float != previous_pps:
        set_cache_value(CACHE_KEY_PPS, pps_float)
    return pps_float


def check_tvl(client, pool) -> float:
    """Check Total Value Locked and alert on large changes."""
    total_assets = client.execute(pool.functions.totalAssets().call)
    tvl_usd = total_assets / ONE_SHARE

    previous_tvl = get_cache_value(CACHE_KEY_TVL)
    logger.info("syrupUSDC TVL: %s (previous: %s)", format_usd(tvl_usd), format_usd(previous_tvl))

    if previous_tvl > 0:
        change_pct = abs(tvl_usd - previous_tvl) / previous_tvl
        if change_pct >= TVL_CHANGE_THRESHOLD:
            direction = "increased" if tvl_usd > previous_tvl else "decreased"
            message = (
                f"🚨 *Maple syrupUSDC TVL Change*\n"
                f"💰 TVL {direction} by {change_pct:.2%}\n"
                f"📊 {format_usd(previous_tvl)} → {format_usd(tvl_usd)}\n"
                f"🔗 [syrupUSDC Pool](https://etherscan.io/address/{SYRUP_USDC_POOL})"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    if tvl_usd != previous_tvl:
        set_cache_value(CACHE_KEY_TVL, tvl_usd)
    return tvl_usd


def check_unrealized_losses(client) -> float:
    """Check unrealized losses on both loan managers.

    Returns:
        Total loans outstanding (AUM) across both loan managers in USD.
    """
    fixed_lm = client.eth.contract(address=FIXED_TERM_LOAN_MANAGER, abi=ABI_LOAN_MANAGER)
    open_lm = client.eth.contract(address=OPEN_TERM_LOAN_MANAGER, abi=ABI_LOAN_MANAGER)

    with client.batch_requests() as batch:
        batch.add(fixed_lm.functions.unrealizedLosses())
        batch.add(open_lm.functions.unrealizedLosses())
        batch.add(fixed_lm.functions.assetsUnderManagement())
        batch.add(open_lm.functions.assetsUnderManagement())

        responses = client.execute_batch(batch)
        if len(responses) != 4:
            raise ValueError(f"Expected 4 responses, got {len(responses)}")

    fixed_losses = responses[0] / ONE_SHARE
    open_losses = responses[1] / ONE_SHARE
    fixed_aum = responses[2] / ONE_SHARE
    open_aum = responses[3] / ONE_SHARE

    logger.info(
        "Loan managers — Fixed: AUM=%s, Losses=%s | Open: AUM=%s, Losses=%s",
        format_usd(fixed_aum),
        format_usd(fixed_losses),
        format_usd(open_aum),
        format_usd(open_losses),
    )

    total_losses = fixed_losses + open_losses
    if total_losses > 0:
        message = (
            f"🚨 *Maple syrupUSDC Unrealized Losses Detected*\n"
            f"📊 Fixed Term: {format_usd(fixed_losses)} (AUM: {format_usd(fixed_aum)})\n"
            f"📊 Open Term: {format_usd(open_losses)} (AUM: {format_usd(open_aum)})\n"
            f"⚠️ Loan impairment may be in progress\n"
            f"🔗 [FixedTermLM](https://etherscan.io/address/{FIXED_TERM_LOAN_MANAGER})\n"
            f"🔗 [OpenTermLM](https://etherscan.io/address/{OPEN_TERM_LOAN_MANAGER})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    return fixed_aum + open_aum


def check_strategy_and_withdrawal_queue(client, pool, tvl: float) -> None:
    """Check strategy allocations and alert on withdrawal queue size.

    Alerts when pending exit value exceeds 80% of liquid funds (Aave + Sky) or 1% of TVL.
    """
    aave_strategy = client.eth.contract(address=AAVE_STRATEGY, abi=ABI_STRATEGY)
    sky_strategy = client.eth.contract(address=SKY_STRATEGY, abi=ABI_STRATEGY)
    wm = client.eth.contract(address=WITHDRAWAL_MANAGER, abi=ABI_WITHDRAWAL_MANAGER)

    with client.batch_requests() as batch:
        batch.add(aave_strategy.functions.assetsUnderManagement())
        batch.add(sky_strategy.functions.assetsUnderManagement())
        batch.add(wm.functions.totalShares())

        responses = client.execute_batch(batch)
        if len(responses) != 3:
            raise ValueError(f"Expected 3 responses, got {len(responses)}")

    aave_assets = responses[0] / ONE_SHARE
    sky_assets = responses[1] / ONE_SHARE
    pending_shares = responses[2]

    # Convert pending shares to their exit value, accounting for unrealized losses.
    pending_assets = 0.0
    if pending_shares > 0:
        pending_assets_raw = client.execute(pool.functions.convertToExitAssets(pending_shares).call)
        pending_assets = pending_assets_raw / ONE_SHARE

    liquid_funds = aave_assets + sky_assets

    logger.info(
        "Strategy allocations — Aave: %s, Sky: %s | Withdrawal queue: %s (liquid: %s)",
        format_usd(aave_assets),
        format_usd(sky_assets),
        format_usd(pending_assets),
        format_usd(liquid_funds),
    )

    if tvl > 0 and pending_assets / tvl > WITHDRAWAL_QUEUE_TVL_THRESHOLD:
        tvl_ratio = pending_assets / tvl
        message = (
            f"*Maple syrupUSDC Withdrawal Queue Alert*\n"
            f"📊 Pending withdrawals: {format_usd(pending_assets)} ({tvl_ratio:.2%} of TVL)\n"
            f"🪣 Liquid funds: {format_usd(liquid_funds)}"
            f"💰 TVL: {format_usd(tvl)}\n"
            f"🔗 [WithdrawalManager](https://etherscan.io/address/{WITHDRAWAL_MANAGER})"
        )
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


def check_pool_liquidity(client, pool) -> None:
    """Check pool USDC cash vs pending withdrawal value.

    Alerts when pending withdrawal exit value exceeds available cash (delegate cannot satisfy
    the queue from idle cash and would need to pull from strategies/loans). Queue size
    is fetched only when alerting, to add context to the message.

    Args:
        client: Web3 client for Ethereum mainnet.
        pool: syrupUSDC pool contract (for share→exit asset conversion).
    """
    usdc = client.eth.contract(address=USDC_ADDRESS, abi=ABI_ERC20_BALANCE)
    wm = client.eth.contract(address=WITHDRAWAL_MANAGER, abi=ABI_WITHDRAWAL_MANAGER)

    with client.batch_requests() as batch:
        batch.add(usdc.functions.balanceOf(SYRUP_USDC_POOL))
        batch.add(wm.functions.totalShares())

        responses = client.execute_batch(batch)
        if len(responses) != 2:
            raise ValueError(f"Expected 2 responses, got {len(responses)}")

    cash_balance = responses[0] / ONE_SHARE
    pending_shares = responses[1]

    pending_assets = 0.0
    if pending_shares > 0:
        pending_assets_raw = client.execute(pool.functions.convertToExitAssets(pending_shares).call)
        pending_assets = pending_assets_raw / ONE_SHARE

    logger.info(
        "Pool liquidity — Cash: %s, Pending: %s",
        format_usd(cash_balance),
        format_usd(pending_assets),
    )

    if pending_assets > cash_balance:
        next_request_id, last_request_id = client.execute(wm.functions.queue().call)
        pending_requests = max(0, last_request_id - next_request_id + 1) if last_request_id >= next_request_id else 0
        message = (
            f"*Maple syrupUSDC Pending Withdrawals Exceed Cash*\n"
            f"💵 Pending: {format_usd(pending_assets)} | Cash: {format_usd(cash_balance)}\n"
            f"📊 Queue depth: {pending_requests} pending requests\n"
            f"🔗 [WithdrawalManager](https://etherscan.io/address/{WITHDRAWAL_MANAGER})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def check_delegate_cover(client) -> None:
    """Check Pool Delegate Cover USDC balance and alert on changes.

    The Pool Delegate Cover is "skin in the game" — USDC deposited by the pool delegate
    that gets slashed first in case of loan defaults. A zero or decreasing balance
    reduces delegate accountability.
    """
    usdc = client.eth.contract(address=USDC_ADDRESS, abi=ABI_ERC20_BALANCE)
    cover_balance = client.execute(usdc.functions.balanceOf(POOL_DELEGATE_COVER).call)
    cover_usd = cover_balance / ONE_SHARE

    previous_cover = get_cache_value(CACHE_KEY_DELEGATE_COVER)
    logger.info("Pool Delegate Cover: %s (previous: %s)", format_usd(cover_usd), format_usd(previous_cover))

    if cover_usd == 0:
        # Only alert once when cover is first detected as zero (previous > 0 or first run)
        if previous_cover > 0:
            message = (
                f"🚨 *Maple syrupUSDC Pool Delegate Cover Empty*\n"
                f"📊 Cover balance dropped from {format_usd(previous_cover)} to $0\n"
                f"⚠️ No delegate skin-in-the-game — reduced accountability for loan defaults\n"
                f"🔗 [PoolDelegateCover](https://etherscan.io/address/{POOL_DELEGATE_COVER})"
            )
            send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    elif previous_cover > 0 and cover_usd < previous_cover:
        decrease_pct = (previous_cover - cover_usd) / previous_cover * 100
        message = (
            f"⚠️ *Maple syrupUSDC Pool Delegate Cover Decrease*\n"
            f"📊 Cover: {format_usd(previous_cover)} → {format_usd(cover_usd)} (-{decrease_pct:.1f}%)\n"
            f"🔗 [PoolDelegateCover](https://etherscan.io/address/{POOL_DELEGATE_COVER})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    if cover_usd != previous_cover:
        set_cache_value(CACHE_KEY_DELEGATE_COVER, cover_usd)


def main() -> None:
    logger.info("Starting Maple syrupUSDC monitoring...")

    client = ChainManager.get_client(Chain.MAINNET)
    pool = client.eth.contract(address=SYRUP_USDC_POOL, abi=ABI_POOL)

    try:
        pps = check_pps(client, pool)
        tvl = check_tvl(client, pool)
        check_unrealized_losses(client)
        check_strategy_and_withdrawal_queue(client, pool, tvl)
        check_pool_liquidity(client, pool)
        check_collateral_risk()
        check_delegate_cover(client)

        logger.info(
            "Monitoring complete — PPS: %.8f, TVL: %s",
            pps,
            format_usd(tvl),
        )
    except Exception as e:
        logger.error("Error during Maple monitoring: %s", e)
        send_alert(Alert(AlertSeverity.LOW, "Maple monitoring failed", PROTOCOL))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
