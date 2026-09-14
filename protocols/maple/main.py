"""
Maple Finance Syrup pool monitoring script.

Monitors:
- PPS (Price Per Share) via convertToAssets(1e6) — alerts on any decrease
- TVL (Total Value Locked) via totalAssets() — alerts on >15% change
- Unrealized losses on loan managers — alerts on any non-zero value
- Strategy allocations — tracks DeFi allocation changes
- Withdrawal queue vs liquid funds — alerts when pending exit value > 80% of liquid funds
- Pool liquidity — cash and withdrawal queue depth
- Loan collateral risk — weighted risk score based on collateral asset types
- Collateralization ratio (via syrupGlobals) — alerts when combined ratio drops below 135%
- Pool Delegate Cover — alerts when delegate cover balance drops to zero
"""

from dataclasses import dataclass

from protocols.maple.collateral import check_collateral_risk
from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    HOURLY_CACHE_STALE_AFTER_SECONDS,
    cache_key_is_stale,
    cache_path,
    get_fresh_last_value_for_key_from_file,
    get_last_value_for_key_from_file,
    write_last_value_to_file,
    write_last_value_with_timestamp_to_file,
)
from utils.chains import Chain
from utils.formatting import format_usd
from utils.logger import get_logger
from utils.telegram import send_error_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "maple"
logger = get_logger(PROTOCOL)

CACHE_FILENAME = cache_path("cache-id.txt")

# --- ABIs ---
ABI_POOL = load_abi("protocols/maple/abi/SyrupUSDCPool.json")
ABI_WITHDRAWAL_MANAGER = load_abi("protocols/maple/abi/WithdrawalManagerQueue.json")
ABI_LOAN_MANAGER = load_abi("protocols/maple/abi/LoanManager.json")
ABI_STRATEGY = load_abi("protocols/maple/abi/Strategy.json")

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


@dataclass
class PoolConfig:
    """Configuration for a Syrup pool monitor."""

    name: str
    pool_address: str
    asset_address: str
    asset_decimals: int
    withdrawal_manager: str | None = None
    fixed_term_loan_manager: str | None = None
    open_term_loan_manager: str | None = None
    strategies: tuple[str, ...] = ()
    delegate_cover: str | None = None
    pps_cache_key: str = ""
    tvl_cache_key: str = ""
    cover_cache_key: str = ""

    @property
    def one_share(self) -> int:
        return 10**self.asset_decimals


SYRUP_USDC = PoolConfig(
    name="syrupUSDC",
    pool_address="0x80ac24aA929eaF5013f6436cdA2a7ba190f5Cc0b",
    asset_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    asset_decimals=6,
    withdrawal_manager="0x1bc47a0Dd0FdaB96E9eF982fdf1F34DC6207cfE3",
    fixed_term_loan_manager="0x4A1c3F0D9aD0b3f9dA085bEBfc22dEA54263371b",
    open_term_loan_manager="0x6ACEb4cAbA81Fa6a8065059f3A944fb066A10fAc",
    strategies=(
        "0x560B3A85Af1cEF113BB60105d0Cf21e1d05F91d4",
        "0x859C9980931fa0A63765fD8EF2e29918Af5b038C",
    ),
    delegate_cover="0x9e62FE15d0E99cE2b30CE0D256e9Ab7b6893AfF5",
    pps_cache_key="MAPLE_PPS",
    tvl_cache_key="MAPLE_TVL",
    cover_cache_key="MAPLE_DELEGATE_COVER",
)

SYRUP_USDG = PoolConfig(
    name="syrupUSDG",
    pool_address="0x87b65C4aAFFA76881f9E96F3e7ED945ddFC3Cd7A",
    asset_address="0xe343167631d89B6Ffc58B88d6b7fB0228795491D",
    asset_decimals=6,
    withdrawal_manager="0xAf63C06970086d535F338565D77c5fA3bDC5fD79",
    strategies=("0x7bE9A1FA4CD69F7a077692d4AFA52bD09531920A",),
    delegate_cover="0xFdc1b5A10f4da87b459dfc3bF1313b33a2F6bfA9",
    pps_cache_key="MAPLE_USDG_PPS",
    tvl_cache_key="MAPLE_USDG_TVL",
    cover_cache_key="MAPLE_USDG_DELEGATE_COVER",
)

POOLS: tuple[PoolConfig, ...] = (SYRUP_USDC, SYRUP_USDG)


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


def get_fresh_cache_value(key: str) -> float:
    """Read an hourly baseline, returning zero when it is stale."""
    val = get_fresh_last_value_for_key_from_file(CACHE_FILENAME, key, HOURLY_CACHE_STALE_AFTER_SECONDS)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def set_fresh_cache_value(key: str, value: float) -> None:
    """Write an hourly baseline and its observation timestamp."""
    write_last_value_with_timestamp_to_file(CACHE_FILENAME, key, value)


def check_pps(client, pool, pool_config: PoolConfig, block_number: int) -> float:
    """Check Price Per Share and alert on decrease."""
    one_share = pool_config.one_share
    pps = client.execute(pool.functions.convertToAssets(one_share).call, block_identifier=block_number)
    pps_float = pps / one_share

    previous_pps = get_cache_value(pool_config.pps_cache_key)
    logger.info("%s PPS: %.8f (previous: %.8f)", pool_config.name, pps_float, previous_pps)

    if previous_pps > 0 and pps_float < previous_pps:
        decrease_pct = (previous_pps - pps_float) / previous_pps * 100
        message = (
            f"🚨 *Maple {pool_config.name} PPS Decrease*\n"
            f"📉 PPS dropped from {previous_pps:.8f} to {pps_float:.8f}\n"
            f"📊 Decrease: {decrease_pct:.4f}%\n"
            f"⚠️ This may indicate loan impairment or loss\n"
            f"🔗 [{pool_config.name} Pool](https://etherscan.io/address/{pool_config.pool_address})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    if pps_float != previous_pps:
        set_cache_value(pool_config.pps_cache_key, pps_float)
    return pps_float


def check_tvl(client, pool, pool_config: PoolConfig, block_number: int) -> float:
    """Check Total Value Locked and alert on large changes."""
    one_share = pool_config.one_share
    total_assets = client.execute(pool.functions.totalAssets().call, block_identifier=block_number)
    tvl_usd = total_assets / one_share

    previous_tvl = get_fresh_cache_value(pool_config.tvl_cache_key)
    logger.info("%s TVL: %s (previous: %s)", pool_config.name, format_usd(tvl_usd), format_usd(previous_tvl))

    if previous_tvl > 0:
        change_pct = abs(tvl_usd - previous_tvl) / previous_tvl
        if change_pct >= TVL_CHANGE_THRESHOLD:
            direction = "increased" if tvl_usd > previous_tvl else "decreased"
            message = (
                f"🚨 *Maple {pool_config.name} TVL Change*\n"
                f"💰 TVL {direction} by {change_pct:.2%}\n"
                f"📊 {format_usd(previous_tvl)} → {format_usd(tvl_usd)}\n"
                f"🔗 [{pool_config.name} Pool](https://etherscan.io/address/{pool_config.pool_address})"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    set_fresh_cache_value(pool_config.tvl_cache_key, tvl_usd)
    return tvl_usd


def check_unrealized_losses(client, pool_config: PoolConfig, block_number: int) -> float:
    """Check unrealized losses on both loan managers.

    Returns:
        Total loans outstanding (AUM) across both loan managers in USD.
    """
    if not pool_config.fixed_term_loan_manager or not pool_config.open_term_loan_manager:
        return 0.0

    fixed_lm = client.eth.contract(address=pool_config.fixed_term_loan_manager, abi=ABI_LOAN_MANAGER)
    open_lm = client.eth.contract(address=pool_config.open_term_loan_manager, abi=ABI_LOAN_MANAGER)

    with client.batch_requests() as batch:
        batch.add(fixed_lm.functions.unrealizedLosses().call(block_identifier=block_number))
        batch.add(open_lm.functions.unrealizedLosses().call(block_identifier=block_number))
        batch.add(fixed_lm.functions.assetsUnderManagement().call(block_identifier=block_number))
        batch.add(open_lm.functions.assetsUnderManagement().call(block_identifier=block_number))

        responses = client.execute_batch(batch)
        if len(responses) != 4:
            raise ValueError(f"Expected 4 responses, got {len(responses)}")

    one_share = pool_config.one_share
    fixed_losses = responses[0] / one_share
    open_losses = responses[1] / one_share
    fixed_aum = responses[2] / one_share
    open_aum = responses[3] / one_share

    logger.info(
        "%s loan managers — Fixed: AUM=%s, Losses=%s | Open: AUM=%s, Losses=%s",
        pool_config.name,
        format_usd(fixed_aum),
        format_usd(fixed_losses),
        format_usd(open_aum),
        format_usd(open_losses),
    )

    total_losses = fixed_losses + open_losses
    if total_losses > 0:
        message = (
            f"🚨 *Maple {pool_config.name} Unrealized Losses Detected*\n"
            f"📊 Fixed Term: {format_usd(fixed_losses)} (AUM: {format_usd(fixed_aum)})\n"
            f"📊 Open Term: {format_usd(open_losses)} (AUM: {format_usd(open_aum)})\n"
            f"⚠️ Loan impairment may be in progress\n"
            f"🔗 [FixedTermLM](https://etherscan.io/address/{pool_config.fixed_term_loan_manager})\n"
            f"🔗 [OpenTermLM](https://etherscan.io/address/{pool_config.open_term_loan_manager})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    return fixed_aum + open_aum


def check_strategy_and_withdrawal_queue(client, pool, pool_config: PoolConfig, tvl: float, block_number: int) -> None:
    """Check strategy allocations and alert on withdrawal queue size.

    Alerts when pending exit value exceeds 80% of liquid funds (strategies) or 1% of TVL.
    """
    one_share = pool_config.one_share
    strategy_assets: list[float] = []
    strategy_contracts: list = []

    for strategy_address in pool_config.strategies:
        strategy = client.eth.contract(address=strategy_address, abi=ABI_STRATEGY)
        strategy_contracts.append(strategy)

    if strategy_contracts:
        with client.batch_requests() as batch:
            for strategy in strategy_contracts:
                batch.add(strategy.functions.assetsUnderManagement().call(block_identifier=block_number))
            responses = client.execute_batch(batch)
            if len(responses) != len(strategy_contracts):
                raise ValueError(f"Expected {len(strategy_contracts)} responses, got {len(responses)}")
            strategy_assets = [r / one_share for r in responses]

    liquid_funds = sum(strategy_assets)

    pending_assets = 0.0
    if pool_config.withdrawal_manager:
        wm = client.eth.contract(address=pool_config.withdrawal_manager, abi=ABI_WITHDRAWAL_MANAGER)
        pending_shares = client.execute(wm.functions.totalShares().call, block_identifier=block_number)

        if pending_shares > 0:
            pending_assets_raw = client.execute(
                pool.functions.convertToExitAssets(pending_shares).call,
                block_identifier=block_number,
            )
            pending_assets = pending_assets_raw / one_share

    strategy_labels = [f"Strategy {i + 1}: {format_usd(a)}" for i, a in enumerate(strategy_assets)]
    logger.info(
        "%s strategy allocations — %s | Withdrawal queue: %s (liquid: %s)",
        pool_config.name,
        ", ".join(strategy_labels) if strategy_labels else "none",
        format_usd(pending_assets),
        format_usd(liquid_funds),
    )

    if tvl > 0 and pending_assets / tvl > WITHDRAWAL_QUEUE_TVL_THRESHOLD:
        tvl_ratio = pending_assets / tvl
        message = (
            f"*Maple {pool_config.name} Withdrawal Queue Alert*\n"
            f"📊 Pending withdrawals: {format_usd(pending_assets)} ({tvl_ratio:.2%} of TVL)\n"
            f"🪣 Liquid funds: {format_usd(liquid_funds)}"
            f"💰 TVL: {format_usd(tvl)}\n"
            f"🔗 [WithdrawalManager](https://etherscan.io/address/{pool_config.withdrawal_manager})"
        )
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


def check_pool_liquidity(client, pool, pool_config: PoolConfig, block_number: int) -> None:
    """Check pool cash vs pending withdrawal value.

    Alerts when pending withdrawal exit value exceeds available cash (delegate cannot satisfy
    the queue from idle cash and would need to pull from strategies/loans). Queue size
    is fetched only when alerting, to add context to the message.
    """
    one_share = pool_config.one_share
    asset = client.eth.contract(address=pool_config.asset_address, abi=ABI_ERC20_BALANCE)

    if pool_config.withdrawal_manager:
        wm = client.eth.contract(address=pool_config.withdrawal_manager, abi=ABI_WITHDRAWAL_MANAGER)
        with client.batch_requests() as batch:
            batch.add(asset.functions.balanceOf(pool_config.pool_address).call(block_identifier=block_number))
            batch.add(wm.functions.totalShares().call(block_identifier=block_number))

            responses = client.execute_batch(batch)
            if len(responses) != 2:
                raise ValueError(f"Expected 2 responses, got {len(responses)}")
    else:
        cash_balance_raw = client.execute(
            asset.functions.balanceOf(pool_config.pool_address).call,
            block_identifier=block_number,
        )
        responses = [cash_balance_raw, 0]

    cash_balance = responses[0] / one_share
    pending_shares = responses[1]

    pending_assets = 0.0
    if pending_shares > 0:
        pending_assets_raw = client.execute(
            pool.functions.convertToExitAssets(pending_shares).call,
            block_identifier=block_number,
        )
        pending_assets = pending_assets_raw / one_share

    logger.info(
        "%s pool liquidity — Cash: %s, Pending: %s",
        pool_config.name,
        format_usd(cash_balance),
        format_usd(pending_assets),
    )

    if pending_assets > cash_balance and pool_config.withdrawal_manager:
        wm = client.eth.contract(address=pool_config.withdrawal_manager, abi=ABI_WITHDRAWAL_MANAGER)
        next_request_id, last_request_id = client.execute(
            wm.functions.queue().call,
            block_identifier=block_number,
        )
        pending_requests = max(0, last_request_id - next_request_id + 1) if last_request_id >= next_request_id else 0
        message = (
            f"*Maple {pool_config.name} Pending Withdrawals Exceed Cash*\n"
            f"💵 Pending: {format_usd(pending_assets)} | Cash: {format_usd(cash_balance)}\n"
            f"📊 Queue depth: {pending_requests} pending requests\n"
            f"🔗 [WithdrawalManager](https://etherscan.io/address/{pool_config.withdrawal_manager})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def check_delegate_cover(client, pool_config: PoolConfig, block_number: int) -> None:
    """Check Pool Delegate Cover balance and alert on changes.

    The Pool Delegate Cover is "skin in the game" — asset deposited by the pool delegate
    that gets slashed first in case of loan defaults. A zero or decreasing balance
    reduces delegate accountability.
    """
    if not pool_config.delegate_cover:
        return

    one_share = pool_config.one_share
    asset = client.eth.contract(address=pool_config.asset_address, abi=ABI_ERC20_BALANCE)
    cover_balance = client.execute(
        asset.functions.balanceOf(pool_config.delegate_cover).call,
        block_identifier=block_number,
    )
    cover_usd = cover_balance / one_share

    previous_cover = get_cache_value(pool_config.cover_cache_key)
    cover_cache_is_stale = cache_key_is_stale(
        CACHE_FILENAME, pool_config.cover_cache_key, HOURLY_CACHE_STALE_AFTER_SECONDS
    )
    logger.info(
        "%s Pool Delegate Cover: %s (previous: %s)", pool_config.name, format_usd(cover_usd), format_usd(previous_cover)
    )

    if cover_usd == 0:
        if previous_cover > 0 or cover_cache_is_stale:
            previous_line = (
                f"📊 Cover balance dropped from {format_usd(previous_cover)} to $0\n"
                if previous_cover > 0
                else "📊 Cover balance is $0 after a stale or missing observation baseline\n"
            )
            message = (
                f"🚨 *Maple {pool_config.name} Pool Delegate Cover Empty*\n"
                f"{previous_line}"
                f"⚠️ No delegate skin-in-the-game — reduced accountability for loan defaults\n"
                f"🔗 [PoolDelegateCover](https://etherscan.io/address/{pool_config.delegate_cover})"
            )
            send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    elif previous_cover > 0 and cover_usd < previous_cover:
        decrease_pct = (previous_cover - cover_usd) / previous_cover * 100
        message = (
            f"⚠️ *Maple {pool_config.name} Pool Delegate Cover Decrease*\n"
            f"📊 Cover: {format_usd(previous_cover)} → {format_usd(cover_usd)} (-{decrease_pct:.1f}%)\n"
            f"🔗 [PoolDelegateCover](https://etherscan.io/address/{pool_config.delegate_cover})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    set_fresh_cache_value(pool_config.cover_cache_key, cover_usd)


def _check_pool(client, pool_config: PoolConfig, block_number: int) -> None:
    """Run all on-chain checks for a single pool, isolated from other pools."""
    pool = client.eth.contract(address=pool_config.pool_address, abi=ABI_POOL)
    pps = check_pps(client, pool, pool_config, block_number)
    tvl = check_tvl(client, pool, pool_config, block_number)
    check_unrealized_losses(client, pool_config, block_number)
    check_strategy_and_withdrawal_queue(client, pool, pool_config, tvl, block_number)
    check_pool_liquidity(client, pool, pool_config, block_number)
    check_delegate_cover(client, pool_config, block_number)

    logger.info(
        "%s monitoring complete — PPS: %.8f, TVL: %s",
        pool_config.name,
        pps,
        format_usd(tvl),
    )


def main() -> None:
    logger.info("Starting Maple Syrup monitoring...")

    client = ChainManager.get_client(Chain.MAINNET)

    try:
        block_number = int(client.eth.block_number)
    except Exception as e:
        logger.error("Error fetching block number: %s", e)
        send_error_message("Maple monitoring failed: could not fetch block number", PROTOCOL)
        return

    for pool_config in POOLS:
        try:
            _check_pool(client, pool_config, block_number)
        except Exception as e:
            logger.error("Error during %s monitoring: %s", pool_config.name, e)
            send_error_message(f"Maple {pool_config.name} monitoring failed", PROTOCOL)

    try:
        check_collateral_risk()
    except Exception as e:
        logger.error("Error during Maple collateral risk check: %s", e)
        send_error_message("Maple collateral risk check failed", PROTOCOL)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
