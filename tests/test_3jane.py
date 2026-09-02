import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from utils import paths, store


def load_3jane_module() -> ModuleType:
    path = Path(__file__).parents[1] / "protocols" / "3jane" / "main.py"
    spec = importlib.util.spec_from_file_location("three_jane", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub_cache(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> dict[str, str]:
    """Replace the module's cache reads/writes with an in-memory dict."""
    cache: dict[str, str] = {}
    monkeypatch.setattr(module, "get_last_value_for_key_from_file", lambda _filename, key: cache.get(key, 0))
    monkeypatch.setattr(module, "set_cache_value", lambda key, value: cache.__setitem__(key, str(value)))
    return cache


def test_junior_buffer_uses_backing_over_deployed_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(7_504_000, 37_776_000)

    assert alerts == []


def test_junior_buffer_alert_describes_deployed_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(5_000_000, 40_000_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "12.50% of deployed credit" in alerts[0].message
    assert "sUSD3 backing: $5.00M | Deployed: $40.00M" in alerts[0].message


def test_usd3_oc_does_not_alert_above_high_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(11_000_000, 100_000_000)

    assert alerts == []


def test_usd3_oc_alerts_high_below_target(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(9_000_000, 100_000_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "USD3 OC: 109.89% (1.0989x; 9.89% excess)" in alerts[0].message
    assert "Senior at-risk: $91.00M" in alerts[0].message
    assert "Threshold: 111% OC" in alerts[0].message


def test_usd3_oc_alerts_critical_below_critical_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(5_000_000, 100_000_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.CRITICAL
    assert "USD3 OC: 105.26% (1.0526x; 5.26% excess)" in alerts[0].message
    assert "Threshold: 106% OC" in alerts[0].message


def test_insurance_fund_alerts_on_large_share_outflow(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cached: list[tuple[str, int | float]] = []
    monkeypatch.setattr(module, "set_cache_value", lambda key, value: cached.append((key, value)))
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_insurance_fund(900_000_000_000, 850_000_000_000, 1_000_000, 58_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.MEDIUM
    assert "Outflow: $58.00K" in alerts[0].message
    assert cached == [(module.CACHE_KEY_INSURANCE_FUND_SHARES, 850_000_000_000)]


def test_insurance_fund_ignores_yield_and_small_outflows(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    monkeypatch.setattr(module, "set_cache_value", lambda _key, _value: None)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_insurance_fund(900_000_000_000, 901_000_000_000, 1_050_000, 0)
    module.check_insurance_fund(900_000_000_000, 899_000_000_000, 1_048_000, 1_200)

    assert alerts == []


def test_withdraw_limit_alerts_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(3_500_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.MEDIUM
    assert "Available withdraw limit: $3.50M" in alerts[0].message
    assert "threshold $4.00M" in alerts[0].message


def test_withdraw_limit_no_alert_at_or_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(module.WITHDRAW_LIMIT_THRESHOLD)
    module.check_withdraw_limit(4_548_324)

    assert alerts == []


def test_withdraw_limit_dedupes_until_value_drops_further(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(3_500_000)  # breach → alert
    module.check_withdraw_limit(3_500_000)  # same value → silent
    module.check_withdraw_limit(3_800_000)  # partial recovery, still breached → silent
    module.check_withdraw_limit(3_200_000)  # dropped below cached → alert

    assert len(alerts) == 2
    assert "Available withdraw limit: $3.50M" in alerts[0].message
    assert "Available withdraw limit: $3.20M" in alerts[1].message


def test_withdraw_limit_rearms_after_recovery_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(3_500_000)  # breach → alert
    module.check_withdraw_limit(4_500_000)  # recovered → clears cache
    module.check_withdraw_limit(3_900_000)  # new breach above old cached value → alert

    assert len(alerts) == 2
    assert "Available withdraw limit: $3.90M" in alerts[1].message


def test_usd3_oc_dedupes_but_realerts_on_drop_to_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(9_000_000, 100_000_000)  # OC 1.0989 → HIGH alert
    module.check_usd3_oc(9_000_000, 100_000_000)  # same value → silent
    module.check_usd3_oc(5_000_000, 100_000_000)  # OC 1.0526 → CRITICAL alert

    assert len(alerts) == 2
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert alerts[1].severity == module.AlertSeverity.CRITICAL


def test_usd3_oc_full_coverage_rearms_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(9_000_000, 100_000_000)  # OC 1.0989 → HIGH alert
    module.check_usd3_oc(100_000_000, 100_000_000)  # fully covered → clears cache
    module.check_usd3_oc(9_500_000, 100_000_000)  # OC 1.1050, above old cached → alert

    assert len(alerts) == 2
    assert alerts[1].severity == module.AlertSeverity.HIGH


def test_withdraw_limit_retries_when_send_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)

    def failing_send(_alert) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(module, "send_alert", failing_send)
    with pytest.raises(RuntimeError):
        module.check_withdraw_limit(3_500_000)  # breach, but delivery fails → not cached

    monkeypatch.setattr(module, "send_alert", alerts.append)
    module.check_withdraw_limit(3_500_000)  # same value retries → alert
    module.check_withdraw_limit(3_500_000)  # now cached → silent

    assert len(alerts) == 1
    assert "Available withdraw limit: $3.50M" in alerts[0].message


def test_junior_buffer_zero_deployed_credit_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(4_000_000, 40_000_000)  # 10% → alert
    module.check_junior_buffer(0, 0)  # book unwound → clears cache
    module.check_junior_buffer(4_800_000, 40_000_000)  # 12%, above old cached 10% → alert

    assert len(alerts) == 2
    assert "12.00% of deployed credit" in alerts[1].message


def test_usd3_oc_zero_deployed_credit_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(9_000_000, 100_000_000)  # OC 1.0989 → alert
    module.check_usd3_oc(0, 0)  # book unwound → clears cache
    module.check_usd3_oc(9_500_000, 100_000_000)  # OC 1.1050, above old cached → alert

    assert len(alerts) == 2
    assert alerts[1].severity == module.AlertSeverity.HIGH


def test_junior_buffer_dedupes_same_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(5_000_000, 40_000_000)  # 12.5% → alert
    module.check_junior_buffer(5_000_000, 40_000_000)  # same → silent
    module.check_junior_buffer(4_000_000, 40_000_000)  # 10% → alert

    assert len(alerts) == 2
    assert "12.50% of deployed credit" in alerts[0].message
    assert "10.00% of deployed credit" in alerts[1].message


def test_insurance_shares_round_trip_exactly_through_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = load_3jane_module()
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)
    monkeypatch.setattr(module, "CACHE_FILENAME", str(tmp_path / "cache-id.txt"))
    monkeypatch.delenv("CACHE_DIR", raising=False)
    monkeypatch.delenv("CACHE_BACKEND", raising=False)
    raw_shares = 9_007_199_254_740_993  # Larger than the exact integer range of float.

    module.set_cache_value(module.CACHE_KEY_INSURANCE_FUND_SHARES, raw_shares)

    assert module.get_cache_int(module.CACHE_KEY_INSURANCE_FUND_SHARES) == raw_shares
    assert store.state_get("cache-id.txt", module.CACHE_KEY_INSURANCE_FUND_SHARES) == str(raw_shares)

    store.state_set("cache-id.txt", module.CACHE_KEY_INSURANCE_FUND_SHARES, "868288861448.0")
    assert module.get_cache_int(module.CACHE_KEY_INSURANCE_FUND_SHARES) == 868_288_861_448


def test_parse_envio_borrower_default_watch_rows_computes_bucket_and_dedupes() -> None:
    module = load_3jane_module()
    market_id = "0x" + "12" * 32
    borrower = "0x00000000000000000000000000000000000000a1"
    cycle_end = 1_700_000_000
    default_at = cycle_end + 30 * module.SECONDS_PER_DAY
    now = default_at - 6 * module.SECONDS_PER_DAY

    parsed = module.parse_envio_borrower_default_watch_rows(
        [
            {
                "marketId": market_id,
                "borrower": borrower,
                "credit": str(2_000_000 * module.ONE_SHARE),
                "amountDue": str(250_000 * module.ONE_SHARE),
                "cycleId": "4",
                "cycleEnd": str(cycle_end),
                "endingBalance": str(1_000_000 * module.ONE_SHARE),
                "gracePeriod": str(7 * module.SECONDS_PER_DAY),
                "delinquencyPeriod": str(23 * module.SECONDS_PER_DAY),
                "defaultStarted": False,
                "settled": False,
            },
            {
                "marketId": market_id.upper(),
                "borrower": borrower,
                "amountDue": str(250_000 * module.ONE_SHARE),
                "cycleEnd": str(cycle_end),
                "settled": "false",
            },
            {"marketId": market_id, "borrower": borrower, "amountDue": "1", "settled": True},
            {"marketId": market_id, "borrower": borrower, "amountDue": "1", "settled": False},
            {"marketId": market_id, "borrower": borrower, "amountDue": "0", "cycleEnd": str(cycle_end)},
            {"marketId": "bad", "borrower": borrower, "amountDue": "1", "cycleEnd": str(cycle_end)},
            {"marketId": market_id, "borrower": "not-an-address", "amountDue": "1", "cycleEnd": str(cycle_end)},
        ],
        now,
    )

    assert parsed == [
        module.BorrowerRepaymentSnapshot(
            market_id=market_id,
            borrower=module.Web3.to_checksum_address(borrower),
            cycle_id=4,
            cycle_end=cycle_end,
            amount_due_raw=250_000 * module.ONE_SHARE,
            ending_balance_raw=1_000_000 * module.ONE_SHARE,
            credit_raw=2_000_000 * module.ONE_SHARE,
            default_started=False,
            repayment_status="Delinquent",
            default_at=default_at,
            seconds_to_default=6 * module.SECONDS_PER_DAY,
            seconds_since_default=0,
            default_bucket="7d",
        )
    ]


def test_borrower_default_watch_snapshot_without_envio_bucket_does_not_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_3jane_module()
    alerts: list = []
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_borrower_default_watch_snapshot(
        module.BorrowerRepaymentSnapshot(
            market_id="0x" + "34" * 32,
            borrower="0x00000000000000000000000000000000000000A1",
            cycle_id=4,
            cycle_end=1_700_000_000,
            amount_due_raw=250_000 * module.ONE_SHARE,
            ending_balance_raw=1_000_000 * module.ONE_SHARE,
            credit_raw=2_000_000 * module.ONE_SHARE,
            default_started=False,
            repayment_status="GracePeriod",
            default_at=1_700_000_000 + 30 * module.SECONDS_PER_DAY,
            seconds_to_default=23 * module.SECONDS_PER_DAY,
            seconds_since_default=0,
            default_bucket=None,
        )
    )
    assert alerts == []


def test_borrower_default_watch_alert_is_medium_and_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache: dict[str, str] = {}
    monkeypatch.setattr(module, "send_alert", alerts.append)
    monkeypatch.setattr(
        module,
        "get_last_value_for_key_from_file",
        lambda _filename, key: cache.get(key, 0),
    )
    monkeypatch.setattr(
        module,
        "write_last_value_to_file",
        lambda _filename, key, value: cache.__setitem__(key, str(value)),
    )

    snapshot = module.BorrowerRepaymentSnapshot(
        market_id="0x" + "34" * 32,
        borrower="0x00000000000000000000000000000000000000A1",
        cycle_id=4,
        cycle_end=1_700_000_000,
        amount_due_raw=250_000 * module.ONE_SHARE,
        ending_balance_raw=1_000_000 * module.ONE_SHARE,
        credit_raw=2_000_000 * module.ONE_SHARE,
        default_started=False,
        repayment_status="Delinquent",
        default_at=1_700_000_000 + 30 * module.SECONDS_PER_DAY,
        seconds_to_default=6 * module.SECONDS_PER_DAY,
        seconds_since_default=0,
        default_bucket="7d",
    )

    module.check_borrower_default_watch_snapshot(snapshot)
    module.check_borrower_default_watch_snapshot(snapshot)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.MEDIUM
    assert "3Jane Borrower Default Watch" in alerts[0].message
    assert "Status: Delinquent (7d)" in alerts[0].message
    assert "Ending balance" in alerts[0].message
    assert len(cache) == 1


def test_borrower_default_watch_alert_shows_time_since_default(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache: dict[str, str] = {}
    monkeypatch.setattr(module, "send_alert", alerts.append)
    monkeypatch.setattr(
        module,
        "get_last_value_for_key_from_file",
        lambda _filename, key: cache.get(key, 0),
    )
    monkeypatch.setattr(
        module,
        "write_last_value_to_file",
        lambda _filename, key, value: cache.__setitem__(key, str(value)),
    )

    snapshot = module.BorrowerRepaymentSnapshot(
        market_id="0x" + "56" * 32,
        borrower="0x00000000000000000000000000000000000000A2",
        cycle_id=5,
        cycle_end=1_700_000_000,
        amount_due_raw=100_000 * module.ONE_SHARE,
        ending_balance_raw=900_000 * module.ONE_SHARE,
        credit_raw=2_000_000 * module.ONE_SHARE,
        default_started=True,
        repayment_status="Default",
        default_at=1_700_000_000 + 30 * module.SECONDS_PER_DAY,
        seconds_to_default=-2 * module.SECONDS_PER_DAY,
        seconds_since_default=2 * module.SECONDS_PER_DAY + 90 * 60,
        default_bucket="default",
    )

    module.check_borrower_default_watch_snapshot(snapshot)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.MEDIUM
    assert "Status: Default (default)" in alerts[0].message
    assert "Defaulted at:" in alerts[0].message
    assert "2d 1h ago" in alerts[0].message


def test_parse_envio_borrower_default_watch_rows_skips_grace_period() -> None:
    module = load_3jane_module()
    market_id = "0x" + "78" * 32
    borrower = "0x00000000000000000000000000000000000000a3"
    cycle_end = 1_700_000_000

    parsed = module.parse_envio_borrower_default_watch_rows(
        [
            {
                "marketId": market_id,
                "borrower": borrower,
                "amountDue": str(250_000 * module.ONE_SHARE),
                "cycleEnd": str(cycle_end),
                "gracePeriod": str(7 * module.SECONDS_PER_DAY),
                "delinquencyPeriod": str(23 * module.SECONDS_PER_DAY),
            },
        ],
        cycle_end + 3 * module.SECONDS_PER_DAY,
    )

    assert parsed == []


def test_parse_envio_borrower_default_watch_rows_default_started_forces_default() -> None:
    module = load_3jane_module()
    market_id = "0x" + "9a" * 32
    borrower = "0x00000000000000000000000000000000000000a4"
    cycle_end = 1_700_000_000
    default_at = cycle_end + 30 * module.SECONDS_PER_DAY

    parsed = module.parse_envio_borrower_default_watch_rows(
        [
            {
                "marketId": market_id,
                "borrower": borrower,
                "amountDue": str(250_000 * module.ONE_SHARE),
                "cycleId": "8",
                "cycleEnd": str(cycle_end),
                "defaultStarted": True,
            },
        ],
        default_at - module.SECONDS_PER_DAY,
    )

    assert len(parsed) == 1
    assert parsed[0].repayment_status == "Default"
    assert parsed[0].default_bucket == "default"
    assert parsed[0].seconds_since_default == 0


# --- Accountable Proof of Solvency ---


def make_accountable_report(
    module: ModuleType,
    ratio: str,
    age_seconds: int = 300,
    ts_ms: int = 1_785_490_814_726,
):
    """Build a report whose totals produce the requested collateral ratio."""
    from decimal import Decimal

    from utils.accountable import AccountableReport

    supply = Decimal("75000000")
    reserves = supply * Decimal(ratio)
    return AccountableReport(
        dfid="100000026",
        collateralization=reserves / supply,
        reported_collateralization=round(reserves / supply, 6),
        net=reserves - supply,
        total_reserves=reserves,
        total_supply=supply,
        verifiability=Decimal("100"),
        ts_ms=ts_ms,
        report_age_seconds=age_seconds,
        report_interval="live",
        report_cadence_seconds=15 * 60,
        sources=(),
    )


def test_accountable_healthy_ratio_does_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_accountable_collateral(make_accountable_report(module, "1.20"))

    assert alerts == []


def test_accountable_high_threshold_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_accountable_collateral(make_accountable_report(module, "0.98"))

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "98.0000%" in alerts[0].message


def test_accountable_critical_requires_two_consecutive_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single sub-95% reading is more likely a stale refresh than insolvency."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    first_report = make_accountable_report(module, "0.94")
    second_report = make_accountable_report(module, "0.94", ts_ms=first_report.ts_ms + 1)

    module.check_accountable_collateral(first_report)
    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH

    module.check_accountable_collateral(second_report)
    assert len(alerts) == 2
    assert alerts[1].severity == module.AlertSeverity.CRITICAL
    assert "95% critical threshold" in alerts[1].message


def test_accountable_frozen_report_does_not_confirm_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    report = make_accountable_report(module, "0.94")

    module.check_accountable_collateral(report)
    module.check_accountable_collateral(report)

    assert [alert.severity for alert in alerts] == [module.AlertSeverity.HIGH]
    assert cache[module.CACHE_KEY_ACCOUNTABLE_CRITICAL_STREAK] == "1"


def test_accountable_critical_streak_resets_on_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_accountable_collateral(make_accountable_report(module, "0.94"))
    module.check_accountable_collateral(make_accountable_report(module, "1.20"))
    module.check_accountable_collateral(make_accountable_report(module, "0.94"))

    # The recovery reset the streak, so the second breach is unconfirmed again
    # and never escalates — two non-consecutive dips must not reach CRITICAL.
    assert [alert.severity for alert in alerts] == [module.AlertSeverity.HIGH, module.AlertSeverity.HIGH]


def test_accountable_does_not_realert_while_below_high(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_accountable_collateral(make_accountable_report(module, "0.98"))
    module.check_accountable_collateral(make_accountable_report(module, "0.981"))
    module.check_accountable_collateral(make_accountable_report(module, "0.982"))

    assert len(alerts) == 1


def test_accountable_recovery_rearms_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_accountable_collateral(make_accountable_report(module, "0.98"))
    module.check_accountable_collateral(make_accountable_report(module, "1.20"))
    module.check_accountable_collateral(make_accountable_report(module, "0.98"))

    assert len(alerts) == 2
    assert all(alert.severity == module.AlertSeverity.HIGH for alert in alerts)


def test_accountable_alerts_never_trigger_emergency_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL here must not zero market caps while the margin is basis points."""
    from utils.dispatch import DISPATCHABLE_PROTOCOLS

    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    first_report = make_accountable_report(module, "0.94")
    second_report = make_accountable_report(module, "0.94", ts_ms=first_report.ts_ms + 1)

    module.check_accountable_collateral(first_report)
    module.check_accountable_collateral(second_report)

    assert alerts[-1].severity == module.AlertSeverity.CRITICAL
    assert alerts[-1].protocol == module.ACCOUNTABLE_ALERT_PROTOCOL
    assert alerts[-1].protocol not in DISPATCHABLE_PROTOCOLS
    # Still routed to the normal 3Jane Telegram channel.
    assert alerts[-1].channel == module.PROTOCOL
    assert module.PROTOCOL in DISPATCHABLE_PROTOCOLS


def test_accountable_unavailable_run_breaks_critical_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    first_ts = 1_785_490_814_726

    module.check_accountable_collateral(make_accountable_report(module, "0.94", ts_ms=first_ts))
    module.check_accountable_availability("connection refused")
    module.check_accountable_collateral(make_accountable_report(module, "0.94", ts_ms=first_ts + 1))

    assert [alert.severity for alert in alerts] == [
        module.AlertSeverity.HIGH,  # Initial ratio warning.
        module.AlertSeverity.HIGH,  # Feed unavailable after retries.
    ]
    assert cache[module.CACHE_KEY_ACCOUNTABLE_CRITICAL_STREAK] == "1"

    module.check_accountable_collateral(make_accountable_report(module, "0.94", ts_ms=first_ts + 2))
    assert [alert.severity for alert in alerts] == [
        module.AlertSeverity.HIGH,
        module.AlertSeverity.HIGH,
        module.AlertSeverity.CRITICAL,
    ]


def test_accountable_staleness_alerts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    report = make_accountable_report(module, "1.20")

    module.check_accountable_staleness(report, "stale sources: Slope (99h)")
    module.check_accountable_staleness(report, "stale sources: Slope (100h)")

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.MEDIUM
    assert "Slope" in alerts[0].message


def test_accountable_availability_alerts_after_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_accountable_availability("connection refused")
    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "after all retry attempts" in alerts[0].message

    # Stays quiet while the outage persists.
    module.check_accountable_availability("connection refused")
    assert len(alerts) == 1


def test_accountable_stale_report_still_evaluates_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A low collateralization reading matters even when its inputs have aged."""
    from utils.accountable import AccountableFetchResult, AccountableStatus

    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    report = make_accountable_report(module, "0.98")
    monkeypatch.setattr(
        module,
        "fetch_report",
        lambda _config: AccountableFetchResult(AccountableStatus.STALE, report, "stale sources: Slope (99h)"),
    )

    module.check_accountable_solvency()

    severities = [alert.severity for alert in alerts]
    assert module.AlertSeverity.MEDIUM in severities  # staleness
    assert module.AlertSeverity.HIGH in severities  # low-ratio reading


def test_accountable_recovery_clears_health_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    from utils.accountable import AccountableFetchResult, AccountableStatus

    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    monkeypatch.setattr(
        module,
        "fetch_report",
        lambda _config: AccountableFetchResult(AccountableStatus.UNAVAILABLE, None, "boom"),
    )

    module.check_accountable_solvency()
    assert len(alerts) == 1
    assert cache[module.CACHE_KEY_ACCOUNTABLE_HEALTH_ALERTED] == "1"

    monkeypatch.setattr(
        module,
        "fetch_report",
        lambda _config: AccountableFetchResult(AccountableStatus.OK, make_accountable_report(module, "1.20")),
    )
    module.check_accountable_solvency()

    assert cache[module.CACHE_KEY_ACCOUNTABLE_HEALTH_ALERTED] == "0"
    assert cache[module.CACHE_KEY_ACCOUNTABLE_FAILURE_STREAK] == "0"


def test_accountable_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The onchain checks must survive any Accountable-side explosion."""
    module = load_3jane_module()
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", lambda _alert: None)

    def boom(_config):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(module, "fetch_report", boom)

    module.check_accountable_solvency()  # must not raise
