#!/usr/bin/env python3
"""Monitor Yearn lender-borrower strategy liquidation and spread risk."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from math import isqrt
from typing import Any

from web3 import Web3

from utils import store
from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chainlink import CHAINLINK_ABI, RoundData
from utils.chains import Chain
from utils.logger import get_logger
from utils.telegram import CURATION_CHANNEL, resolve_channel
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "yearn"
logger = get_logger("yearn.lender_borrower")

WAD = 10**18
USD_SCALE = 10**8
MAX_BPS = 10_000
ORACLE_PRICE_SCALE = 10**36
SECONDS_PER_YEAR = 31_556_952
MORPHO_SECONDS_PER_YEAR = 365 * 24 * 60 * 60
MORPHO_TARGET_UTILIZATION_WAD = 9 * WAD // 10
MORPHO_CURVE_STEEPNESS_WAD = 4 * WAD
MORPHO_MIN_RATE_AT_TARGET = (WAD // 1_000) // MORPHO_SECONDS_PER_YEAR
MORPHO_MAX_RATE_AT_TARGET = (2 * WAD) // MORPHO_SECONDS_PER_YEAR
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1

YEARN_APR_ORACLE = "0x1981AD9F44F2EA9aDd2dC4AD7D075c102C70aF92"
RATE_STATE_NAMESPACE = "yearn_lender_borrower_rates"
ALERT_STATE_NAMESPACE = "yearn_lender_borrower_alerts"
ERROR_STATE_NAMESPACE = "yearn_lender_borrower_errors"
ALERT_REMINDER_SECONDS = 24 * 60 * 60
CHECK_LTV = "ltv"
CHECK_RATES_AND_COVERAGE = "rates-and-coverage"
CHECK_ALL = "all"

STRATEGY_ABI = load_abi("protocols/yearn/abi/LenderBorrower.json")
MORPHO_ABI = load_abi("protocols/yearn/abi/MorphoCore.json")
IRM_ABI = load_abi("protocols/yearn/abi/MorphoIrm.json")
MORPHO_ORACLE_ABI = load_abi("protocols/yearn/abi/MorphoOracle.json")
APR_ORACLE_ABI = load_abi("protocols/yearn/abi/AprOracle.json")


@dataclass(frozen=True)
class StrategyConfig:
    """Static monitoring policy for one lender-borrower strategy."""

    name: str
    chain: Chain
    address: str
    collateral_address: str
    collateral_symbol: str
    collateral_decimals: int
    borrow_address: str
    borrow_symbol: str
    borrow_decimals: int
    lender_vault_address: str
    morpho_address: str
    market_id: str
    morpho_oracle_address: str
    morpho_irm_address: str
    liquidation_ltv_wad: int
    joc_url: str
    negative_spread_threshold_bps: int = 100
    rate_window_hours: int = 24
    minimum_rate_samples: int = 3
    deficit_threshold_bps: int = 10
    deficit_min_usd: int = 100
    borrow_price_max_age_seconds: int = 26 * 60 * 60

    @property
    def market_params(self) -> tuple[str, str, str, str, int]:
        """Return the immutable Morpho market parameters."""
        return (
            self.borrow_address,
            self.collateral_address,
            self.morpho_oracle_address,
            self.morpho_irm_address,
            self.liquidation_ltv_wad,
        )


@dataclass(frozen=True)
class RateSample:
    """One observed lender-minus-borrow APR sample."""

    timestamp: int
    spread_wad: int


@dataclass(frozen=True)
class StrategySnapshot:
    """On-chain state used by all lender-borrower checks."""

    timestamp: int
    strategy_name: str
    collateral_symbol: str
    collateral_decimals: int
    borrow_symbol: str
    borrow_decimals: int
    collateral: int
    debt: int
    lent: int
    idle_borrow_token: int
    current_ltv_wad: int
    target_ltv_wad: int
    warning_ltv_wad: int
    liquidation_ltv_wad: int
    collateral_price_usd_e8: int
    borrow_price_usd_e8: int
    lender_apr_wad: int | None
    borrow_apr_wad: int | None

    @property
    def available_borrow_token(self) -> int:
        """Return lent plus idle borrow tokens."""
        return self.lent + self.idle_borrow_token

    @property
    def deficit(self) -> int:
        """Return uncovered debt in borrow-token units."""
        return max(0, self.debt - self.available_borrow_token)

    @property
    def deficit_bps(self) -> int:
        """Return uncovered debt as integer basis points of debt."""
        return self.deficit * MAX_BPS // self.debt if self.debt else 0

    @property
    def deficit_usd_e8(self) -> int:
        """Return uncovered debt value in 1e8 USD units."""
        return int(self.deficit * self.borrow_price_usd_e8 // (10**self.borrow_decimals))

    @property
    def spread_wad(self) -> int:
        """Return lender APR minus borrow APR in WAD."""
        if self.lender_apr_wad is None or self.borrow_apr_wad is None:
            raise ValueError("Rate data was not loaded for this snapshot")
        return self.lender_apr_wad - self.borrow_apr_wad


@dataclass(frozen=True)
class Evaluation:
    """Policy result for one strategy snapshot."""

    issue_codes: tuple[str, ...]
    issue_messages: tuple[str, ...]
    average_spread_wad: int | None
    rate_sample_count: int


STRATEGIES = (
    StrategyConfig(
        name="Morpho vbWBTC/yvUSDC Lender Borrower",
        chain=Chain.KATANA,
        address="0x0432337365d89c0D73f1D0Cb263791F8f1B98D43",
        collateral_address="0x0913DA6Da4b42f538B445599b46Bb4622342Cf52",
        collateral_symbol="vbWBTC",
        collateral_decimals=8,
        borrow_address="0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36",
        borrow_symbol="vbUSDC",
        borrow_decimals=6,
        lender_vault_address="0x80c34BD3A3569E126e7055831036aa7b212cB159",
        morpho_address="0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc",
        market_id="0xcd2dc555dced7422a3144a4126286675449019366f83e9717be7c2deb3daae3e",
        morpho_oracle_address="0xB60F728BdcE5e3921C0E42c1a6F07A1313D0040e",
        morpho_irm_address="0x4F708C0ae7deD3d74736594C2109C2E3c065B428",
        liquidation_ltv_wad=860_000_000_000_000_000,
        joc_url="https://joc.yearn.dev/strategy/katana/0x0432337365d89c0D73f1D0Cb263791F8f1B98D43",
    ),
)


def calculate_warning_ltv(liquidation_ltv_wad: int, warning_multiplier_bps: int) -> int:
    """Reproduce BaseLenderBorrower._getWarningLTV()."""
    return liquidation_ltv_wad * warning_multiplier_bps // MAX_BPS


def calculate_target_ltv(liquidation_ltv_wad: int, target_multiplier_bps: int) -> int:
    """Reproduce BaseLenderBorrower._getTargetLTV()."""
    return liquidation_ltv_wad * target_multiplier_bps // MAX_BPS


def _divide_to_zero(numerator: int, denominator: int) -> int:
    """Divide integers with Solidity's signed truncation toward zero."""
    if denominator == 0:
        raise ZeroDivisionError("division by zero")
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    return sign * (abs(numerator) // abs(denominator))


def morpho_curve_coefficient_wad(market: tuple[int, ...]) -> int:
    """Return AdaptiveCurveIRM's utilization coefficient in WAD."""
    total_supply_assets = int(market[0])
    total_borrow_assets = int(market[2])
    utilization = total_borrow_assets * WAD // total_supply_assets if total_supply_assets else 0
    utilization = min(utilization, WAD)

    if utilization > MORPHO_TARGET_UTILIZATION_WAD:
        err = (utilization - MORPHO_TARGET_UTILIZATION_WAD) * WAD // (WAD - MORPHO_TARGET_UTILIZATION_WAD)
        coefficient = MORPHO_CURVE_STEEPNESS_WAD - WAD
    else:
        err = _divide_to_zero(
            (utilization - MORPHO_TARGET_UTILIZATION_WAD) * WAD,
            MORPHO_TARGET_UTILIZATION_WAD,
        )
        coefficient = WAD - WAD * WAD // MORPHO_CURVE_STEEPNESS_WAD
    return WAD + _divide_to_zero(coefficient * err, WAD)


def calculate_instantaneous_borrow_rate(
    average_rate_per_second: int,
    start_rate_per_second: int,
    market: tuple[int, ...],
) -> int:
    """Invert AdaptiveCurveIRM's window average to obtain its current end rate.

    The IRM averages start, midpoint, and end target rates using the trapezoidal
    rule. Since the curve is linear in the target rate, ``avg / start`` equals
    ``((1 + adaptation_factor) / 2) ** 2`` until a target-rate bound is hit.
    The final clamp mirrors the IRM's min/max target-rate bounds.
    """
    if average_rate_per_second <= 0 or start_rate_per_second <= 0:
        raise ValueError("Morpho IRM returned a non-positive borrow rate")

    root_wad = isqrt(average_rate_per_second * WAD**2 // start_rate_per_second)
    adaptation_factor_wad = 2 * root_wad - WAD
    if adaptation_factor_wad <= 0:
        raise ValueError("Morpho IRM average cannot be inverted safely")

    inferred_end_rate = start_rate_per_second * adaptation_factor_wad**2 // WAD**2
    curve_coefficient = morpho_curve_coefficient_wad(market)
    minimum_rate = MORPHO_MIN_RATE_AT_TARGET * curve_coefficient // WAD
    maximum_rate = MORPHO_MAX_RATE_AT_TARGET * curve_coefficient // WAD
    return min(max(inferred_end_rate, minimum_rate), maximum_rate)


def validate_borrow_price_round(round_data: RoundData, now: int, max_age_seconds: int) -> None:
    """Reject invalid, future-dated, or stale borrow-token USD prices."""
    if round_data.answer <= 0:
        raise ValueError("Borrow-token USD oracle returned an invalid price")
    if round_data.updated_at <= 0:
        raise ValueError("Borrow-token USD oracle returned no update timestamp")
    if round_data.updated_at > now:
        raise ValueError("Borrow-token USD oracle update timestamp is in the future")
    age = now - round_data.updated_at
    if age > max_age_seconds:
        raise ValueError(f"Borrow-token USD oracle is stale: age={age}s maximum={max_age_seconds}s")


def taylor_compounded(rate_per_second_wad: int, elapsed_seconds: int) -> int:
    """Reproduce Morpho's three-term compounded-interest approximation."""
    first_term = rate_per_second_wad * elapsed_seconds
    second_term = first_term * first_term // (2 * WAD)
    third_term = second_term * first_term // (3 * WAD)
    return first_term + second_term + third_term


def accrue_market(market: tuple[int, int, int, int, int, int], rate_per_second_wad: int, now: int) -> tuple[int, ...]:
    """Return Morpho market balances after expected interest accrual."""
    total_supply_assets, total_supply_shares, total_borrow_assets, total_borrow_shares, last_update, fee = market
    elapsed = max(0, now - last_update)
    if elapsed == 0 or total_borrow_assets == 0:
        return market

    interest = total_borrow_assets * taylor_compounded(rate_per_second_wad, elapsed) // WAD
    total_borrow_assets += interest
    total_supply_assets += interest

    if fee:
        fee_amount = interest * fee // WAD
        fee_shares = (
            fee_amount * (total_supply_shares + VIRTUAL_SHARES) // (total_supply_assets - fee_amount + VIRTUAL_ASSETS)
        )
        total_supply_shares += fee_shares

    return (
        total_supply_assets,
        total_supply_shares,
        total_borrow_assets,
        total_borrow_shares,
        last_update,
        fee,
    )


def prune_rate_samples(samples: list[RateSample], now: int, window_hours: int) -> list[RateSample]:
    """Keep unique samples within the configured rolling window."""
    cutoff = now - window_hours * 60 * 60
    by_timestamp = {sample.timestamp: sample for sample in samples if cutoff <= sample.timestamp <= now}
    return sorted(by_timestamp.values(), key=lambda sample: sample.timestamp)


def average_spread(samples: list[RateSample], minimum_samples: int) -> int | None:
    """Return the arithmetic mean spread once enough observations exist."""
    if len(samples) < minimum_samples:
        return None
    return sum(sample.spread_wad for sample in samples) // len(samples)


def evaluate_snapshot(
    config: StrategyConfig,
    snapshot: StrategySnapshot,
    rate_samples: list[RateSample],
    checks: str = CHECK_ALL,
) -> Evaluation:
    """Evaluate LTV, rolling spread, and debt-coverage policy."""
    issue_codes: list[str] = []
    issue_messages: list[str] = []

    if checks in (CHECK_LTV, CHECK_ALL):
        if snapshot.debt > 0 and snapshot.collateral == 0:
            issue_codes.append("debt_without_collateral")
            issue_messages.append("Debt is non-zero while collateral is zero")
        elif snapshot.current_ltv_wad > snapshot.warning_ltv_wad:
            issue_codes.append("ltv")
            issue_messages.append(
                f"LTV {_format_percent(snapshot.current_ltv_wad)} is above warning "
                f"{_format_percent(snapshot.warning_ltv_wad)}"
            )

    if checks in (CHECK_RATES_AND_COVERAGE, CHECK_ALL):
        deficit_threshold_usd_e8 = config.deficit_min_usd * USD_SCALE
        if snapshot.deficit_bps >= config.deficit_threshold_bps and snapshot.deficit_usd_e8 >= deficit_threshold_usd_e8:
            issue_codes.append("debt_coverage")
            issue_messages.append(
                f"Debt deficit is {_format_token(snapshot.deficit, snapshot.borrow_decimals)} "
                f"{snapshot.borrow_symbol} ({snapshot.deficit_bps} bps)"
            )

    avg_spread_wad = None
    if checks in (CHECK_RATES_AND_COVERAGE, CHECK_ALL):
        if snapshot.debt and (snapshot.lender_apr_wad is None or snapshot.borrow_apr_wad is None):
            issue_codes.append("rate_data")
            issue_messages.append("Current lender or borrow APR is unavailable; no rate sample was recorded")
        avg_spread_wad = average_spread(rate_samples, config.minimum_rate_samples) if snapshot.debt else None
        threshold_wad = config.negative_spread_threshold_bps * WAD // MAX_BPS
        if avg_spread_wad is not None and avg_spread_wad < -threshold_wad:
            issue_codes.append("net_spread")
            issue_messages.append(
                f"{config.rate_window_hours}h average net spread {_format_percent(avg_spread_wad)} is below "
                f"-{config.negative_spread_threshold_bps / 100:.2f}%"
            )

    return Evaluation(tuple(issue_codes), tuple(issue_messages), avg_spread_wad, len(rate_samples))


def _read_snapshot(config: StrategyConfig, *, include_rates: bool) -> StrategySnapshot:
    client = ChainManager.get_client(config.chain)
    strategy_address = Web3.to_checksum_address(config.address)
    strategy = client.get_contract(strategy_address, STRATEGY_ABI)

    # Static addresses and Morpho market parameters live in StrategyConfig. The
    # remaining values are position state or management-settable configuration.
    with client.batch_requests() as batch:
        for call in (
            strategy.functions.borrowUsdOracle(),
            strategy.functions.balanceOfCollateral(),
            strategy.functions.balanceOfDebt(),
            strategy.functions.balanceOfLentAssets(),
            strategy.functions.balanceOfBorrowToken(),
            strategy.functions.getCurrentLTV(),
            strategy.functions.warningLTVMultiplier(),
            strategy.functions.targetLTVMultiplier(),
        ):
            batch.add(call)
        values = client.execute_batch(batch)

    (
        borrow_usd_oracle_address,
        collateral,
        debt,
        lent,
        idle_borrow_token,
        current_ltv_wad,
        warning_multiplier_bps,
        target_multiplier_bps,
    ) = values

    lender_vault_address = Web3.to_checksum_address(config.lender_vault_address)
    morpho_address = Web3.to_checksum_address(config.morpho_address)
    borrow_usd_oracle_address = Web3.to_checksum_address(borrow_usd_oracle_address)
    market_params = config.market_params
    liquidation_ltv_wad = market_params[4]

    block = client.execute(client.eth.get_block, "latest")
    block_number = int(block["number"])
    block_timestamp = int(block["timestamp"])
    morpho_oracle = client.get_contract(market_params[2], MORPHO_ORACLE_ABI)
    price_feed = client.get_contract(borrow_usd_oracle_address, CHAINLINK_ABI)

    with client.batch_requests() as batch:
        for call in (
            morpho_oracle.functions.price(),
            price_feed.functions.decimals(),
            price_feed.functions.latestRoundData(),
        ):
            batch.add(call)
        aux = client.execute_batch(batch)

    (
        oracle_price,
        price_feed_decimals,
        price_round,
    ) = aux
    borrow_price_round = RoundData.from_tuple(price_round)
    validate_borrow_price_round(
        borrow_price_round,
        block_timestamp,
        config.borrow_price_max_age_seconds,
    )
    borrow_price_answer = borrow_price_round.answer

    borrow_price_usd_e8 = borrow_price_answer * USD_SCALE // (10 ** int(price_feed_decimals))
    collateral_price_borrow_wad = (
        int(oracle_price)
        * (10**config.collateral_decimals)
        * WAD
        // (ORACLE_PRICE_SCALE * (10**config.borrow_decimals))
    )
    collateral_price_usd_e8 = collateral_price_borrow_wad * borrow_price_usd_e8 // WAD

    lender_apr_wad: int | None = None
    borrow_apr_wad: int | None = None
    if include_rates:
        morpho = client.get_contract(morpho_address, MORPHO_ABI)
        apr_oracle = client.get_contract(Web3.to_checksum_address(YEARN_APR_ORACLE), APR_ORACLE_ABI)
        with client.batch_requests() as batch:
            batch.add(morpho.functions.market(config.market_id))
            batch.add(apr_oracle.functions.getStrategyApr(lender_vault_address, 0))
            market_raw, lender_apr_raw = client.execute_batch(batch)

        market_values = tuple(int(value) for value in market_raw)
        if len(market_values) != 6:
            raise ValueError(f"Morpho market returned {len(market_values)} values instead of 6")
        market = (
            market_values[0],
            market_values[1],
            market_values[2],
            market_values[3],
            market_values[4],
            market_values[5],
        )
        irm = client.get_contract(market_params[3], IRM_ABI)
        average_rate = _call_irm(client, irm, market_params, market, block_number)
        current_timestamp_market = market[:4] + (block_timestamp, market[5])
        start_rate = _call_irm(client, irm, market_params, current_timestamp_market, block_number)
        borrow_rate_per_second = calculate_instantaneous_borrow_rate(average_rate, start_rate, market)
        lender_apr_wad = int(lender_apr_raw) or None
        borrow_apr_wad = borrow_rate_per_second * SECONDS_PER_YEAR

    return StrategySnapshot(
        timestamp=block_timestamp,
        strategy_name=config.name,
        collateral_symbol=config.collateral_symbol,
        collateral_decimals=config.collateral_decimals,
        borrow_symbol=config.borrow_symbol,
        borrow_decimals=config.borrow_decimals,
        collateral=int(collateral),
        debt=int(debt),
        lent=int(lent),
        idle_borrow_token=int(idle_borrow_token),
        current_ltv_wad=int(current_ltv_wad),
        target_ltv_wad=calculate_target_ltv(int(liquidation_ltv_wad), int(target_multiplier_bps)),
        warning_ltv_wad=calculate_warning_ltv(int(liquidation_ltv_wad), int(warning_multiplier_bps)),
        liquidation_ltv_wad=int(liquidation_ltv_wad),
        collateral_price_usd_e8=collateral_price_usd_e8,
        borrow_price_usd_e8=borrow_price_usd_e8,
        lender_apr_wad=lender_apr_wad,
        borrow_apr_wad=borrow_apr_wad,
    )


def _call_irm(
    client: Web3Client,
    irm: Any,
    market_params: tuple[str, str, str, str, int],
    market: tuple[int, ...],
    block_identifier: int,
) -> int:
    """Call Morpho's view IRM with the supplied market state."""
    return int(
        client.execute(
            irm.functions.borrowRateView(market_params, market).call,
            block_identifier=block_identifier,
        )
    )


def _load_rate_samples(config: StrategyConfig) -> list[RateSample]:
    raw = store.state_get(RATE_STATE_NAMESPACE, config.address.lower())
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
        return [RateSample(timestamp=int(row["timestamp"]), spread_wad=int(row["spread_wad"])) for row in decoded]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Ignoring malformed rate history for %s", config.address)
        return []


def _update_rate_samples(config: StrategyConfig, snapshot: StrategySnapshot, *, persist: bool) -> list[RateSample]:
    samples = _load_rate_samples(config) if persist else []
    if snapshot.debt and snapshot.lender_apr_wad is not None and snapshot.borrow_apr_wad is not None:
        samples.append(RateSample(snapshot.timestamp, snapshot.spread_wad))
    samples = prune_rate_samples(samples, snapshot.timestamp, config.rate_window_hours)
    if persist:
        store.state_set(
            RATE_STATE_NAMESPACE,
            config.address.lower(),
            json.dumps([{"timestamp": sample.timestamp, "spread_wad": sample.spread_wad} for sample in samples]),
        )
    return samples


def _state_key(config: StrategyConfig, checks: str) -> str:
    return f"{config.address.lower()}:{checks}"


def _should_send_alert(config: StrategyConfig, evaluation: Evaluation, now: int, checks: str = CHECK_ALL) -> bool:
    key = _state_key(config, checks)
    raw = store.state_get(ALERT_STATE_NAMESPACE, key)
    previous: dict[str, Any] = {}
    if raw:
        try:
            previous = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            previous = {}

    if not evaluation.issue_codes:
        if previous.get("fingerprint"):
            store.state_set(ALERT_STATE_NAMESPACE, key, json.dumps({"fingerprint": "", "last_alert": now}))
        return False

    fingerprint = "|".join(sorted(evaluation.issue_codes))
    last_alert = int(previous.get("last_alert", 0))
    return previous.get("fingerprint") != fingerprint or now - last_alert >= ALERT_REMINDER_SECONDS


def _record_alert_sent(config: StrategyConfig, evaluation: Evaluation, now: int, checks: str = CHECK_ALL) -> None:
    """Persist alert state only after Telegram delivery succeeds."""
    fingerprint = "|".join(sorted(evaluation.issue_codes))
    store.state_set(
        ALERT_STATE_NAMESPACE,
        _state_key(config, checks),
        json.dumps({"fingerprint": fingerprint, "last_alert": now}),
    )


def _should_send_error(config: StrategyConfig, checks: str, error_type: str, now: int) -> bool:
    """Return whether a monitor error is new or due for its daily reminder."""
    raw = store.state_get(ERROR_STATE_NAMESPACE, _state_key(config, checks))
    previous: dict[str, Any] = {}
    if raw:
        try:
            previous = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            previous = {}
    last_alert = int(previous.get("last_alert", 0))
    return previous.get("fingerprint") != error_type or now - last_alert >= ALERT_REMINDER_SECONDS


def _record_error_sent(config: StrategyConfig, checks: str, error_type: str, now: int) -> None:
    store.state_set(
        ERROR_STATE_NAMESPACE,
        _state_key(config, checks),
        json.dumps({"fingerprint": error_type, "last_alert": now}),
    )


def _clear_error_state(config: StrategyConfig, checks: str, now: int) -> None:
    key = _state_key(config, checks)
    if store.state_get(ERROR_STATE_NAMESPACE, key):
        store.state_set(ERROR_STATE_NAMESPACE, key, json.dumps({"fingerprint": "", "last_alert": now}))


def _format_percent(wad_value: int) -> str:
    return f"{Decimal(wad_value) * 100 / WAD:.2f}%"


def _format_token(raw_value: int, decimals: int) -> str:
    value = Decimal(raw_value) / (10**decimals)
    if abs(value) >= 1_000:
        return f"{value:,.2f}"
    return f"{value:,.4f}"


def _format_usd(e8_value: int) -> str:
    return f"${Decimal(e8_value) / USD_SCALE:,.2f}"


def _format_deficit_bps(snapshot: StrategySnapshot) -> str:
    if snapshot.debt == 0:
        return "0.00"
    return f"{Decimal(snapshot.deficit) * MAX_BPS / snapshot.debt:.2f}"


def build_summary(config: StrategyConfig, snapshot: StrategySnapshot, evaluation: Evaluation, checks: str) -> str:
    """Build the complete diagnostic summary used in logs and alerts."""
    lines = [config.name]
    if checks in (CHECK_LTV, CHECK_ALL):
        lines.extend(
            [
                f"LTV: {_format_percent(snapshot.current_ltv_wad)} "
                f"(target {_format_percent(snapshot.target_ltv_wad)}, warning {_format_percent(snapshot.warning_ltv_wad)}, "
                f"liquidation {_format_percent(snapshot.liquidation_ltv_wad)})",
                f"Prices: {snapshot.collateral_symbol} {_format_usd(snapshot.collateral_price_usd_e8)}, "
                f"{snapshot.borrow_symbol} {_format_usd(snapshot.borrow_price_usd_e8)}",
            ]
        )
    if checks in (CHECK_RATES_AND_COVERAGE, CHECK_ALL):
        average_spread = (
            _format_percent(evaluation.average_spread_wad)
            if evaluation.average_spread_wad is not None
            else "warming up"
        )
        if snapshot.lender_apr_wad is None or snapshot.borrow_apr_wad is None:
            current_rates = "Rates: unavailable; current sample skipped"
        else:
            current_rates = (
                f"Rates: lender {_format_percent(snapshot.lender_apr_wad)}, "
                f"borrow {_format_percent(snapshot.borrow_apr_wad)}, "
                f"instant spread {_format_percent(snapshot.spread_wad)}"
            )
        lines.extend(
            [
                f"Debt: {_format_token(snapshot.debt, snapshot.borrow_decimals)} {snapshot.borrow_symbol}",
                f"Lent + idle: {_format_token(snapshot.available_borrow_token, snapshot.borrow_decimals)} "
                f"{snapshot.borrow_symbol}",
                f"Deficit: {_format_token(snapshot.deficit, snapshot.borrow_decimals)} {snapshot.borrow_symbol} "
                f"({_format_deficit_bps(snapshot)} bps, {_format_usd(snapshot.deficit_usd_e8)})",
                f"{current_rates}, {config.rate_window_hours}h average {average_spread} "
                f"({evaluation.rate_sample_count}/{config.minimum_rate_samples} minimum samples)",
            ]
        )
    lines.append(config.joc_url)
    return "\n".join(lines)


def run_strategy(config: StrategyConfig, *, checks: str = CHECK_ALL, dry_run: bool = False) -> None:
    """Read, evaluate, persist, and possibly alert for one strategy."""
    include_rates = checks in (CHECK_RATES_AND_COVERAGE, CHECK_ALL)
    snapshot = _read_snapshot(config, include_rates=include_rates)
    samples = _update_rate_samples(config, snapshot, persist=not dry_run) if include_rates else []
    evaluation = evaluate_snapshot(config, snapshot, samples, checks)
    summary = build_summary(config, snapshot, evaluation, checks)
    logger.info("%s", summary.replace("\n", " | "))
    if not dry_run:
        _clear_error_state(config, checks, snapshot.timestamp)

    if not evaluation.issue_codes:
        if not dry_run:
            _should_send_alert(config, evaluation, snapshot.timestamp, checks)
        return

    message = "Lender Borrower Warning\n" + "\n".join(f"- {issue}" for issue in evaluation.issue_messages)
    message += "\n\n" + summary
    if dry_run:
        logger.warning("Dry run would alert: %s", message.replace("\n", " | "))
    elif _should_send_alert(config, evaluation, snapshot.timestamp, checks):
        send_alert(
            Alert(
                AlertSeverity.MEDIUM,
                message,
                PROTOCOL,
                channel=resolve_channel(CURATION_CHANNEL, PROTOCOL),
            ),
            plain_text=True,
        )
        _record_alert_sent(config, evaluation, snapshot.timestamp, checks)


def main() -> None:
    """Run the configured Yearn lender-borrower monitors."""
    parser = argparse.ArgumentParser(description="Monitor Yearn lender-borrower strategies")
    parser.add_argument(
        "--checks",
        choices=(CHECK_LTV, CHECK_RATES_AND_COVERAGE, CHECK_ALL),
        default=CHECK_ALL,
        help="Check group to run",
    )
    parser.add_argument("--dry-run", action="store_true", help="Read and evaluate without storing state or alerting")
    args = parser.parse_args()

    for config in STRATEGIES:
        try:
            run_strategy(config, checks=args.checks, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - isolate configured strategies from one another
            logger.exception("Failed to evaluate %s", config.name)
            if args.dry_run:
                raise
            now = int(time.time())
            error_type = type(exc).__name__
            if _should_send_error(config, args.checks, error_type, now):
                send_alert(
                    Alert(
                        AlertSeverity.MEDIUM,
                        f"Lender Borrower Monitor Error ({args.checks})\n"
                        f"{config.name}\n{error_type}: {exc}\n{config.joc_url}",
                        PROTOCOL,
                        channel=resolve_channel(CURATION_CHANNEL, PROTOCOL),
                    ),
                    plain_text=True,
                )
                _record_error_sent(config, args.checks, error_type, now)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
