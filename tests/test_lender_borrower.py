from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from protocols.yearn.lender_borrower import (
    CHECK_LTV,
    CHECK_RATES_AND_COVERAGE,
    MAX_BPS,
    RAY_TO_WAD,
    STRATEGIES,
    WAD,
    AaveMarketConfig,
    Evaluation,
    RateSample,
    StrategySnapshot,
    _record_alert_sent,
    _record_error_sent,
    _should_send_alert,
    _should_send_error,
    aave_borrow_apr_wad,
    accrue_market,
    average_spread,
    calculate_instantaneous_borrow_rate,
    calculate_warning_ltv,
    evaluate_snapshot,
    prune_rate_samples,
    validate_borrow_price_round,
)
from utils.chainlink import RoundData

CONFIG = STRATEGIES[0]


def _snapshot(**changes: Any) -> StrategySnapshot:
    base = StrategySnapshot(
        timestamp=1_000_000,
        strategy_name=CONFIG.name,
        collateral_symbol="vbWBTC",
        collateral_decimals=8,
        borrow_symbol="vbUSDC",
        borrow_decimals=6,
        collateral=15 * 10**8,
        debt=600_000 * 10**6,
        lent=599_900 * 10**6,
        idle_borrow_token=100 * 10**6,
        current_ltv_wad=58 * WAD // 100,
        target_ltv_wad=602 * WAD // 1000,
        warning_ltv_wad=688 * WAD // 1000,
        liquidation_ltv_wad=86 * WAD // 100,
        collateral_price_usd_e8=80_000 * 10**8,
        borrow_price_usd_e8=10**8,
        lender_apr_wad=3 * WAD // 100,
        borrow_apr_wad=35 * WAD // 1000,
    )
    return replace(base, **changes)


def test_calculate_warning_ltv_matches_strategy_formula() -> None:
    assert calculate_warning_ltv(86 * WAD // 100, 8_000) == 688 * WAD // 1_000


def test_aave_borrow_apr_converts_ray_to_wad() -> None:
    reserve_data = (0, 0, 0, 0, 39_300_000_000_000_000_000_000_000)
    assert aave_borrow_apr_wad(reserve_data) == reserve_data[4] // RAY_TO_WAD


def test_accumulator_config_targets_underlying_borrower() -> None:
    config = STRATEGIES[1]
    assert config.address == "0x13f6Cb609959a43c3bE29407766A683b42e26D28"
    assert config.monitored_address == "0x41cfE42D221a591C6308Dcea419015Ba8570B380"
    assert isinstance(config.market, AaveMarketConfig)


def test_accrue_market_adds_expected_interest() -> None:
    market = (2_000_000, 2_000_000, 1_000_000, 1_000_000, 100, 0)
    accrued = accrue_market(market, 10**14, 200)
    assert accrued[0] > market[0]
    assert accrued[2] > market[2]
    assert accrued[0] - market[0] == accrued[2] - market[2]
    assert accrued[1] == market[1]
    assert accrued[3] == market[3]


def test_instantaneous_borrow_rate_inverts_irm_window_average() -> None:
    market = (10_000_000, 10_000_000, 9_500_000, 9_500_000, 100, 0)
    start_rate = 1_000_000_000
    adaptation_factor_wad = 12 * WAD // 10
    average_rate = start_rate * (WAD + adaptation_factor_wad) ** 2 // (4 * WAD**2)
    expected_end_rate = start_rate * adaptation_factor_wad**2 // WAD**2

    actual = calculate_instantaneous_borrow_rate(average_rate, start_rate, market)
    assert abs(actual - expected_end_rate) <= 2


def test_borrow_price_round_rejects_stale_or_invalid_data() -> None:
    now = 100_000
    validate_borrow_price_round(RoundData(1, 10**8, now - 10, now - 10, 1), now, 60)

    with pytest.raises(ValueError, match="invalid price"):
        validate_borrow_price_round(RoundData(1, 0, now - 10, now - 10, 1), now, 60)
    with pytest.raises(ValueError, match="stale"):
        validate_borrow_price_round(RoundData(1, 10**8, now - 61, now - 61, 1), now, 60)


def test_rate_samples_are_pruned_and_deduplicated() -> None:
    now = 100_000
    samples = [
        RateSample(now - 90_000, 1),
        RateSample(now - 10, 2),
        RateSample(now - 10, 3),
        RateSample(now + 1, 4),
    ]
    assert prune_rate_samples(samples, now, 24) == [RateSample(now - 10, 3)]


def test_average_spread_requires_minimum_samples() -> None:
    samples = [RateSample(i, -WAD // 100) for i in range(3)]
    assert average_spread(samples, 4) is None
    samples.append(RateSample(4, -2 * WAD // 100))
    assert average_spread(samples, 4) == -5 * WAD // 400


def test_healthy_snapshot_has_no_issues() -> None:
    snapshot = _snapshot()
    samples = [RateSample(snapshot.timestamp - i, snapshot.spread_wad) for i in range(4)]
    evaluation = evaluate_snapshot(CONFIG, snapshot, samples)
    assert evaluation.issue_codes == ()


def test_ltv_above_warning_alerts() -> None:
    snapshot = _snapshot(current_ltv_wad=69 * WAD // 100)
    evaluation = evaluate_snapshot(CONFIG, snapshot, [])
    assert "ltv" in evaluation.issue_codes


def test_ltv_mode_does_not_evaluate_rates_or_coverage() -> None:
    snapshot = _snapshot(
        current_ltv_wad=69 * WAD // 100,
        lent=0,
        idle_borrow_token=0,
        lender_apr_wad=None,
        borrow_apr_wad=None,
    )
    evaluation = evaluate_snapshot(CONFIG, snapshot, [], CHECK_LTV)
    assert evaluation.issue_codes == ("ltv",)


def test_debt_coverage_requires_relative_and_absolute_thresholds() -> None:
    debt = 600_000 * 10**6
    # $500 is above the absolute threshold but below 10 bps of $600k debt.
    below_relative = _snapshot(debt=debt, lent=debt - 500 * 10**6, idle_borrow_token=0)
    assert "debt_coverage" not in evaluate_snapshot(CONFIG, below_relative, []).issue_codes

    at_both = _snapshot(debt=debt, lent=debt - 600 * 10**6, idle_borrow_token=0)
    assert at_both.deficit_bps == 10
    assert "debt_coverage" in evaluate_snapshot(CONFIG, at_both, []).issue_codes


def test_negative_average_spread_alerts_after_three_samples() -> None:
    snapshot = _snapshot(lender_apr_wad=2 * WAD // 100, borrow_apr_wad=4 * WAD // 100)
    samples = [RateSample(snapshot.timestamp - i, snapshot.spread_wad) for i in range(2)]
    assert "net_spread" not in evaluate_snapshot(CONFIG, snapshot, samples).issue_codes

    samples.append(RateSample(snapshot.timestamp - 3, snapshot.spread_wad))
    evaluation = evaluate_snapshot(CONFIG, snapshot, samples)
    assert evaluation.average_spread_wad == -2 * WAD // 100
    assert "net_spread" in evaluation.issue_codes


def test_zero_lender_apr_is_reported_as_unavailable_rate_data() -> None:
    snapshot = _snapshot(lender_apr_wad=None)
    evaluation = evaluate_snapshot(CONFIG, snapshot, [], CHECK_RATES_AND_COVERAGE)
    assert evaluation.issue_codes == ("rate_data",)


def test_rates_and_coverage_mode_does_not_evaluate_ltv() -> None:
    debt = 600_000 * 10**6
    snapshot = _snapshot(
        current_ltv_wad=69 * WAD // 100,
        debt=debt,
        lent=debt - 600 * 10**6,
        idle_borrow_token=0,
    )
    evaluation = evaluate_snapshot(CONFIG, snapshot, [], CHECK_RATES_AND_COVERAGE)
    assert evaluation.issue_codes == ("debt_coverage",)


def test_deficit_bps_uses_debt_denominator() -> None:
    snapshot = _snapshot(debt=100_000, lent=99_900, idle_borrow_token=0)
    assert snapshot.deficit_bps == 10
    assert snapshot.deficit * MAX_BPS // snapshot.debt == 10


def test_alerts_are_deduplicated_and_reminded_daily() -> None:
    now = 1_000_000
    evaluation = Evaluation(("ltv",), ("LTV is high",), None, 0)
    assert _should_send_alert(CONFIG, evaluation, now)

    _record_alert_sent(CONFIG, evaluation, now)
    assert not _should_send_alert(CONFIG, evaluation, now + 60)
    assert _should_send_alert(CONFIG, evaluation, now + 24 * 60 * 60)


def test_new_issue_fingerprint_alerts_immediately() -> None:
    now = 1_000_000
    ltv = Evaluation(("ltv",), ("LTV is high",), None, 0)
    coverage = Evaluation(("debt_coverage",), ("Coverage is low",), None, 0)
    _record_alert_sent(CONFIG, ltv, now)
    assert _should_send_alert(CONFIG, coverage, now + 60)


def test_monitor_errors_are_deduplicated_and_reminded_daily() -> None:
    now = 1_000_000
    assert _should_send_error(CONFIG, CHECK_LTV, "ValueError", now)

    _record_error_sent(CONFIG, CHECK_LTV, "ValueError", now)
    assert not _should_send_error(CONFIG, CHECK_LTV, "ValueError", now + 60)
    assert _should_send_error(CONFIG, CHECK_LTV, "ValueError", now + 24 * 60 * 60)
    assert _should_send_error(CONFIG, CHECK_LTV, "TimeoutError", now + 60)
