import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

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


def test_junior_buffer_does_not_alert_in_design_range(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # 19.86% sits in the steady-state design range; no alert.
    module.check_junior_buffer(7_504_000, 37_776_000)

    assert alerts == []


def test_junior_buffer_does_not_alert_on_steady_state_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # Small steady-state drift within the design range does not alert.
    module.check_junior_buffer(7_504_000, 37_776_000)  # 19.86% baseline
    module.check_junior_buffer(7_300_000, 37_776_000)  # 19.32% — 0.54pp drop, below 3pp

    assert alerts == []


def test_junior_buffer_alerts_on_structural_floor_breach(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(3_000_000, 40_000_000)  # 7.5% < 8% floor

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.LOW
    assert "Junior Buffer Drifting" in alerts[0].message
    assert "7.50%" in alerts[0].message
    assert "structural floor" in alerts[0].message
    assert "sUSD3 backing: $3.00M | Deployed: $40.00M" in alerts[0].message


def test_junior_buffer_alerts_on_deterioration_from_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(6_000_000, 40_000_000)  # 15% — primes baseline
    module.check_junior_buffer(2_500_000, 40_000_000)  # 6.25% — both floor breach and 8.75pp drop

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.LOW
    assert "Junior Buffer Drifting" in alerts[0].message
    # Floor check takes precedence in the reason line.
    assert "structural floor" in alerts[0].message


def test_junior_buffer_alerts_on_drop_above_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(8_000_000, 40_000_000)  # 20% baseline
    module.check_junior_buffer(4_500_000, 40_000_000)  # 11.25% — 8.75pp drop, above 8% floor

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.LOW
    assert "leverage drift" in alerts[0].message
    assert "20.00% → 11.25%" in alerts[0].message
    assert "-8.75pp" in alerts[0].message


def test_junior_buffer_silent_on_small_drop_below_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(8_000_000, 40_000_000)  # 20% baseline
    module.check_junior_buffer(7_500_000, 40_000_000)  # 18.75% — 1.25pp drop, below 3pp threshold

    assert alerts == []


def test_junior_buffer_silent_at_floor_with_no_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """First-run check at the design value must not alert (no baseline to drop from)."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(4_000_000, 40_000_000)  # 10% — design value, no prior baseline

    assert alerts == []


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


def test_junior_buffer_zero_deployed_credit_clears_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(6_000_000, 40_000_000)  # 15% primes baseline (no alert, in design range)
    module.check_junior_buffer(2_000_000, 40_000_000)  # 5% floor breach → alert
    module.check_junior_buffer(0, 0)  # book unwound → clears alert and baseline
    module.check_junior_buffer(7_000_000, 40_000_000)  # 17.5% — no baseline, no alert
    module.check_junior_buffer(2_000_000, 40_000_000)  # 5% again → fresh alert

    assert len(alerts) == 2
    assert cache[module.CACHE_KEY_JUNIOR_BUFFER_ALERTED] == str(2_000_000 / 40_000_000)
    assert cache[module.CACHE_KEY_JUNIOR_BUFFER_BASELINE] == str(2_000_000 / 40_000_000)


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


def test_junior_buffer_dedupes_same_deterioration(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(8_000_000, 40_000_000)  # 20% baseline
    module.check_junior_buffer(4_500_000, 40_000_000)  # 11.25% drop → alert
    module.check_junior_buffer(4_500_000, 40_000_000)  # same → silent
    module.check_junior_buffer(2_500_000, 40_000_000)  # 6.25% further drop (floor breach) → alert

    assert len(alerts) == 2
    assert "20.00% → 11.25%" in alerts[0].message
    assert "structural floor" in alerts[1].message


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


# ----------------------------------------------------------------------
# At-risk exposure aggregation
# ----------------------------------------------------------------------


def _borrower(
    module: ModuleType,
    *,
    borrower: str,
    ending_balance: int,
    amount_due: int,
    repayment_status: str,
    default_bucket: str,
    cycle_id: int = 1,
) -> Any:
    return module.BorrowerRepaymentSnapshot(
        market_id="0x" + "11" * 32,
        borrower=borrower,
        cycle_id=cycle_id,
        cycle_end=1_700_000_000,
        amount_due_raw=amount_due * module.ONE_SHARE,
        ending_balance_raw=ending_balance * module.ONE_SHARE,
        credit_raw=(ending_balance * 2) * module.ONE_SHARE,
        default_started=repayment_status == "Default",
        repayment_status=repayment_status,
        default_at=1_700_000_000 + 30 * module.SECONDS_PER_DAY,
        seconds_to_default=2 * module.SECONDS_PER_DAY,
        seconds_since_default=0,
        default_bucket=default_bucket,
    )


def test_compute_at_risk_exposure_empty_input() -> None:
    module = load_3jane_module()
    at_risk = module.compute_at_risk_exposure([])

    assert at_risk == module.AtRiskExposure(0.0, 0.0, 0.0, 0.0, 0.0, module.ZERO_ADDRESS, 0)


def test_compute_at_risk_exposure_weights_by_bucket() -> None:
    module = load_3jane_module()
    borrower_a = "0x00000000000000000000000000000000000000A1"
    borrower_b = "0x00000000000000000000000000000000000000A2"
    borrower_c = "0x00000000000000000000000000000000000000A3"
    snapshots = [
        _borrower(
            module,
            borrower=borrower_a,
            ending_balance=1_000_000,
            amount_due=100_000,
            repayment_status="Default",
            default_bucket="default",  # weight 1.0
        ),
        _borrower(
            module,
            borrower=borrower_b,
            ending_balance=2_000_000,
            amount_due=200_000,
            repayment_status="Delinquent",
            default_bucket="3d",  # weight 0.7
        ),
        _borrower(
            module,
            borrower=borrower_c,
            ending_balance=500_000,
            amount_due=50_000,
            repayment_status="Delinquent",
            default_bucket="14d",  # weight 0.3
        ),
    ]

    at_risk = module.compute_at_risk_exposure(snapshots)

    # Weighted: 1M*1.0 + 2M*0.7 + 0.5M*0.3 = 1M + 1.4M + 0.15M = 2.55M
    assert at_risk.total_weighted == pytest.approx(2_550_000)
    assert at_risk.total_raw == pytest.approx(3_500_000)
    assert at_risk.default_exposure == pytest.approx(1_000_000)
    assert at_risk.delinquent_exposure == pytest.approx(2_500_000)
    assert at_risk.largest_borrower_exposure == pytest.approx(2_000_000)
    assert at_risk.largest_borrower_address == module.Web3.to_checksum_address(borrower_b)
    assert at_risk.count == 3


def test_compute_at_risk_exposure_falls_back_to_amount_due() -> None:
    module = load_3jane_module()
    borrower = "0x00000000000000000000000000000000000000A1"
    snapshots = [
        _borrower(
            module,
            borrower=borrower,
            ending_balance=0,  # not yet indexed
            amount_due=250_000,
            repayment_status="Delinquent",
            default_bucket="7d",  # weight 0.5
        ),
    ]

    at_risk = module.compute_at_risk_exposure(snapshots)

    assert at_risk.total_weighted == pytest.approx(125_000)  # 250k * 0.5
    assert at_risk.total_raw == pytest.approx(250_000)
    assert at_risk.largest_borrower_exposure == pytest.approx(250_000)


def test_compute_at_risk_exposure_ignores_zero_exposure() -> None:
    module = load_3jane_module()
    borrower = "0x00000000000000000000000000000000000000A1"
    snapshots = [
        _borrower(
            module,
            borrower=borrower,
            ending_balance=0,
            amount_due=0,  # no exposure → should be ignored
            repayment_status="Default",
            default_bucket="default",
        ),
    ]

    at_risk = module.compute_at_risk_exposure(snapshots)

    # Zero exposure is filtered out of all monetary fields, but count is preserved.
    assert at_risk.total_weighted == 0.0
    assert at_risk.total_raw == 0.0
    assert at_risk.default_exposure == 0.0
    assert at_risk.count == 1
    assert at_risk.largest_borrower_address == module.ZERO_ADDRESS


# ----------------------------------------------------------------------
# Junior / senior coverage checks
# ----------------------------------------------------------------------


def _empty_at_risk(module: ModuleType) -> Any:
    return module.AtRiskExposure(0.0, 0.0, 0.0, 0.0, 0.0, module.ZERO_ADDRESS, 0)


def test_junior_coverage_no_alert_when_no_at_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # Prime an alert first so we can verify it gets cleared by the at_risk<=0 path.
    module.check_junior_coverage(1_000_000, _at_risk(module, weighted=5_000_000))
    assert len(alerts) == 1
    assert cache[module.CACHE_KEY_JUNIOR_COVERAGE_ALERTED] != "-1"

    module.check_junior_coverage(1_000_000, _empty_at_risk(module))

    assert alerts == [alerts[0]]  # no new alert, just the primed one
    assert cache[module.CACHE_KEY_JUNIOR_COVERAGE_ALERTED] == "-1"


def test_junior_coverage_alerts_high_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # sUSD3 backing 3M, at-risk 2M weighted -> 1.5x (< 2.0x) -> HIGH
    module.check_junior_coverage(3_000_000, _at_risk(module, weighted=2_000_000))

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "Junior Tranche Coverage Low" in alerts[0].message
    assert "1.50x" in alerts[0].message
    assert "sUSD3 backing: $3.00M" in alerts[0].message


def test_junior_coverage_silent_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # 2.0x exactly is at threshold (not below) -> silent.
    module.check_junior_coverage(4_000_000, _at_risk(module, weighted=2_000_000))
    # Well above threshold -> silent.
    module.check_junior_coverage(10_000_000, _at_risk(module, weighted=2_000_000))

    assert alerts == []


def test_junior_coverage_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_coverage(3_000_000, _at_risk(module, weighted=2_000_000))  # 1.5x → alert
    module.check_junior_coverage(3_000_000, _at_risk(module, weighted=2_000_000))  # same → silent
    module.check_junior_coverage(4_000_000, _at_risk(module, weighted=4_000_000))  # 1.0x → alert
    # Recovery above threshold should re-arm
    module.check_junior_coverage(10_000_000, _at_risk(module, weighted=2_000_000))  # 5x → clears
    module.check_junior_coverage(3_500_000, _at_risk(module, weighted=2_000_000))  # 1.75x → alert again

    assert len(alerts) == 3
    assert "1.50x" in alerts[0].message
    assert "1.00x" in alerts[1].message
    assert "1.75x" in alerts[2].message


def test_senior_coverage_no_alert_when_no_at_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # Prime an alert first
    module.check_senior_coverage(1_000_000, 1_000_000, _at_risk(module, weighted=4_000_000))  # 0.5x → CRITICAL
    assert len(alerts) == 1
    assert cache[module.CACHE_KEY_SENIOR_COVERAGE_ALERTED] != "-1"

    module.check_senior_coverage(1_000_000, 1_000_000, _empty_at_risk(module))

    assert alerts == [alerts[0]]  # no new alert
    assert cache[module.CACHE_KEY_SENIOR_COVERAGE_ALERTED] == "-1"


def test_senior_coverage_alerts_high_above_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # insurance 1M + sUSD3 2M = 3M, at-risk 2.5M -> 1.2x (< 1.5x) -> HIGH (>= 1.0x)
    module.check_senior_coverage(1_000_000, 2_000_000, _at_risk(module, weighted=2_500_000))

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "Senior Coverage Low" in alerts[0].message
    assert "1.20x" in alerts[0].message
    assert "Insurance: $1.00M | sUSD3: $2.00M" in alerts[0].message


def test_senior_coverage_alerts_critical_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # insurance 1M + sUSD3 1M = 2M, at-risk 5M -> 0.4x (< 1.0x) -> CRITICAL
    module.check_senior_coverage(1_000_000, 1_000_000, _at_risk(module, weighted=5_000_000))

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.CRITICAL
    assert "Senior Coverage CRITICAL" in alerts[0].message
    assert "0.40x" in alerts[0].message
    assert "First-loss stack: $2.00M" in alerts[0].message


def test_senior_coverage_silent_above_high(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # 1.5x exactly is at threshold (not below) -> silent
    module.check_senior_coverage(1_500_000, 1_500_000, _at_risk(module, weighted=2_000_000))
    # 2.0x is comfortably above -> silent
    module.check_senior_coverage(2_000_000, 2_000_000, _at_risk(module, weighted=2_000_000))

    assert alerts == []


def test_senior_coverage_escalates_high_to_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_senior_coverage(1_000_000, 2_000_000, _at_risk(module, weighted=2_500_000))  # 1.2x → HIGH
    module.check_senior_coverage(1_000_000, 1_000_000, _at_risk(module, weighted=5_000_000))  # 0.4x → CRITICAL

    assert len(alerts) == 2
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert alerts[1].severity == module.AlertSeverity.CRITICAL


def test_senior_coverage_rearms_after_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_senior_coverage(1_000_000, 1_000_000, _at_risk(module, weighted=4_000_000))  # 0.5x → CRITICAL
    module.check_senior_coverage(2_000_000, 2_000_000, _at_risk(module, weighted=2_000_000))  # 2.0x → re-arms
    module.check_senior_coverage(1_500_000, 1_500_000, _at_risk(module, weighted=2_500_000))  # 1.2x → alert again

    assert len(alerts) == 2
    assert alerts[1].severity == module.AlertSeverity.HIGH


def test_junior_and_senior_use_independent_cache_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A HIGH junior alert must not suppress an independent senior alert."""
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_coverage(3_000_000, _at_risk(module, weighted=2_000_000))  # 1.5x → HIGH
    # Senior coverage is at 0.5x — independent of the junior alert.
    module.check_senior_coverage(500_000, 500_000, _at_risk(module, weighted=2_000_000))  # 0.5x → CRITICAL

    assert len(alerts) == 2
    assert cache[module.CACHE_KEY_JUNIOR_COVERAGE_ALERTED] != "-1"
    assert cache[module.CACHE_KEY_SENIOR_COVERAGE_ALERTED] != "-1"


def test_coverage_skip_when_envio_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `load_borrower_default_watch_snapshots_from_envio` returns None, main() must skip coverage."""
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)
    monkeypatch.setattr(module, "load_borrower_default_watch_snapshots_from_envio", lambda: None)

    # Simulate the `main()` wiring: pass `None` through directly.
    snapshots = module.load_borrower_default_watch_snapshots_from_envio()
    at_risk = module.compute_at_risk_exposure(snapshots or [])
    module.check_junior_coverage(3_000_000, at_risk)
    module.check_senior_coverage(1_000_000, 2_000_000, at_risk)
    # Default-watch should also silently skip on None.
    module.check_borrower_default_watch(snapshots)

    assert alerts == []
    assert cache.get(module.CACHE_KEY_JUNIOR_COVERAGE_ALERTED, "-1") == "-1"
    assert cache.get(module.CACHE_KEY_SENIOR_COVERAGE_ALERTED, "-1") == "-1"


def test_at_risk_breakdown_includes_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    at_risk = module.AtRiskExposure(
        total_weighted=2_550_000,
        total_raw=3_500_000,
        default_exposure=1_000_000,
        delinquent_exposure=2_500_000,
        largest_borrower_exposure=2_000_000,
        largest_borrower_address="0x00000000000000000000000000000000000000A2",
        count=3,
    )
    module.check_junior_coverage(3_000_000, at_risk)

    assert len(alerts) == 1
    assert "At-risk (weighted): $2.55M" in alerts[0].message
    assert "Unweighted: $3.50M (3 borrowers)" in alerts[0].message
    assert "Default: $1.00M" in alerts[0].message
    assert "Delinquent: $2.50M" in alerts[0].message


def _at_risk(module: ModuleType, *, weighted: float, count: int = 1) -> Any:
    return module.AtRiskExposure(
        total_weighted=weighted,
        total_raw=weighted,
        default_exposure=weighted,
        delinquent_exposure=0.0,
        largest_borrower_exposure=weighted,
        largest_borrower_address=module.ZERO_ADDRESS,
        count=count,
    )
