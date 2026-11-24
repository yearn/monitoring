import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock

import pytest

from utils import paths, store
from utils.alert import Alert
from utils.defillama_verified import DefiLlamaVerifiedError, parse_report
from utils.telegram import TelegramError


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
    monkeypatch.setattr(module, "get_fresh_cache_value", lambda key: float(cache.get(key, 0)))
    monkeypatch.setattr(module, "set_fresh_cache_value", lambda key, value: cache.__setitem__(key, str(value)))
    monkeypatch.setattr(
        module,
        "get_fresh_last_value_for_key_from_file",
        lambda _filename, key, _stale_after: cache.get(key, 0),
    )
    return cache


def test_usd3_protection_does_not_alert_when_oc_and_buffer_are_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert "Junior buffer: 9.00% of deployed credit" in alerts[0].message
    assert "Senior at-risk: $91.00M" in alerts[0].message
    assert "Threshold: 111% OC / 9.91% junior buffer" in alerts[0].message


def test_usd3_oc_alerts_critical_below_critical_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(5_000_000, 100_000_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.CRITICAL
    assert "USD3 OC: 105.26% (1.0526x; 5.26% excess)" in alerts[0].message
    assert "Junior buffer: 5.00% of deployed credit" in alerts[0].message
    assert "Threshold: 106% OC / 5.66% junior buffer" in alerts[0].message


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
    module.check_withdraw_limit(3_200_000)  # dropped, but less than the re-alert step → silent
    module.check_withdraw_limit(2_400_000)  # dropped more than the step below cached → alert

    assert len(alerts) == 2
    assert "Available withdraw limit: $3.50M" in alerts[0].message
    assert "Available withdraw limit: $2.40M" in alerts[1].message


def test_withdraw_limit_does_not_realert_while_draining_dust(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully utilized market drifting from dollars to zero alerts once, not every run."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    for limit in (26.57, 0.80, 0.0):
        module.check_withdraw_limit(limit)

    assert len(alerts) == 1
    assert "Available withdraw limit: $26.57" in alerts[0].message


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


def test_usd3_oc_slow_drift_does_not_realert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-step OC erosion (e.g. interest accrual) stays quiet; a full step re-alerts."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(7_500_000, 116_120_000)  # OC ~1.0690 → HIGH alert
    module.check_usd3_oc(7_500_000, 116_125_000)  # OC drifts down by <0.01pp → silent
    module.check_usd3_oc(6_900_000, 116_125_000)  # OC ~1.0632, >0.5pp below alerted → alert

    assert len(alerts) == 2
    assert all(alert.severity == module.AlertSeverity.HIGH for alert in alerts)


def test_usd3_oc_escalation_to_critical_bypasses_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crossing into CRITICAL alerts even when the drop is smaller than the re-alert step."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(5_700_000, 100_000_000)  # OC ~1.0604 → HIGH alert
    module.check_usd3_oc(5_600_000, 100_000_000)  # OC ~1.0593 (<0.5pp lower) crosses 106% → CRITICAL

    assert [alert.severity for alert in alerts] == [module.AlertSeverity.HIGH, module.AlertSeverity.CRITICAL]


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
        7 * module.SECONDS_PER_DAY,
        23 * module.SECONDS_PER_DAY,
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


def test_borrower_default_watch_dedupe_survives_config_period_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A governance change to the grace/delinquency periods must not re-send milestones."""
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
        market_id="0x" + "78" * 32,
        borrower="0x00000000000000000000000000000000000000A3",
        cycle_id=6,
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

    # Governance extends DELINQUENCY_PERIOD by a day: same borrower, same cycle,
    # same milestone, only default_at moves.
    module.check_borrower_default_watch_snapshot(
        replace(
            snapshot,
            default_at=snapshot.default_at + module.SECONDS_PER_DAY,
            seconds_to_default=snapshot.seconds_to_default + module.SECONDS_PER_DAY,
        )
    )

    assert len(alerts) == 1
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
        7 * module.SECONDS_PER_DAY,
        23 * module.SECONDS_PER_DAY,
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
        7 * module.SECONDS_PER_DAY,
        23 * module.SECONDS_PER_DAY,
        default_at - module.SECONDS_PER_DAY,
    )

    assert len(parsed) == 1
    assert parsed[0].repayment_status == "Default"
    assert parsed[0].default_bucket == "default"
    assert parsed[0].seconds_since_default == 0


def test_borrower_watch_uses_live_onchain_timing_instead_of_envio_values() -> None:
    module = load_3jane_module()
    market_id = "0x" + "ab" * 32
    borrower = "0x00000000000000000000000000000000000000a5"
    cycle_end = 1_700_000_000
    grace_period = module.SECONDS_PER_DAY
    delinquency_period = 97 * module.SECONDS_PER_DAY

    parsed = module.parse_envio_borrower_default_watch_rows(
        [
            {
                "marketId": market_id,
                "borrower": borrower,
                "amountDue": str(100_000 * module.ONE_SHARE),
                "cycleId": "9",
                "cycleEnd": str(cycle_end),
                "gracePeriod": str(7 * module.SECONDS_PER_DAY),
                "delinquencyPeriod": str(23 * module.SECONDS_PER_DAY),
                "defaultStarted": False,
                "settled": False,
            }
        ],
        grace_period,
        delinquency_period,
        cycle_end + 40 * module.SECONDS_PER_DAY,
    )

    assert len(parsed) == 1
    assert parsed[0].repayment_status == "Delinquent"
    assert parsed[0].default_bucket == "delinquent"
    assert parsed[0].default_at == cycle_end + 98 * module.SECONDS_PER_DAY
    assert parsed[0].seconds_to_default == 58 * module.SECONDS_PER_DAY


def test_debt_cap_alert_displays_wausdc_shares_and_usdc_equivalent(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    cache[module.CACHE_KEY_DEBT_CAP] = "60000000"
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_debt_cap(
        63_000_000 * module.ONE_SHARE,
        int(1.05 * module.RATE_SCALE),
    )

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.LOW
    assert "waUSDC shares: 60.00M → 63.00M" in alerts[0].message
    assert "USDC equivalent at current rate: $63.00M → $66.15M" in alerts[0].message


def test_pps_rounding_drop_alerts_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = {module.CACHE_KEY_USD3_PPS: 1.05, module.CACHE_KEY_SUSD3_PPS: 1.10}
    monkeypatch.setattr(module, "get_cache_value", lambda key: cache.get(key, 0.0))
    monkeypatch.setattr(module, "set_cache_value", cache.__setitem__)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # A sub-basis-point float dip on both tranches: still alerts, but must not move funds.
    module.check_pps(1.05 * (1 - 1e-7), 1.10 * (1 - 1e-7))

    assert [alert.dispatch for alert in alerts] == [False, False]


def test_pps_material_drop_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = {module.CACHE_KEY_USD3_PPS: 1.05, module.CACHE_KEY_SUSD3_PPS: 1.10}
    monkeypatch.setattr(module, "get_cache_value", lambda key: cache.get(key, 0.0))
    monkeypatch.setattr(module, "set_cache_value", cache.__setitem__)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_pps(1.04, 1.09)

    assert [alert.dispatch for alert in alerts] == [True, True]


def test_pps_small_steps_dispatch_once_cumulative_drop_crosses_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    cache = {module.CACHE_KEY_USD3_PPS: 1.0, module.CACHE_KEY_SUSD3_PPS: 1.0}
    monkeypatch.setattr(module, "get_cache_value", lambda key: cache.get(key, 0.0))
    monkeypatch.setattr(module, "set_cache_value", cache.__setitem__)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    # 0.004% per run: no single step reaches the 0.01% gate, the third run's
    # cumulative 0.012% drawdown does.
    pps = 1.0
    for _ in range(3):
        pps *= 1 - 0.00004
        module.check_pps(pps, 1.0)

    assert [alert.dispatch for alert in alerts] == [False, False, True]

    # After a dispatch-eligible alert the next small step is measured from the new level.
    module.check_pps(pps * (1 - 0.00004), 1.0)
    assert alerts[-1].dispatch is False


def test_susd3_tvl_alert_is_denominated_in_usd3(monkeypatch: pytest.MonkeyPatch) -> None:
    """sUSD3 totalAssets is in USD3, so the alert must not render it as dollars."""
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    cache[module.CACHE_KEY_USD3_TVL] = "116000000"
    cache[module.CACHE_KEY_SUSD3_TVL] = "7500000"
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_tvl(116_000_000, 6_300_000)

    assert len(alerts) == 1
    assert "7.50M → 6.30M USD3" in alerts[0].message
    assert "$7.50M" not in alerts[0].message


def test_vault_shutdown_alerts_once_per_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_vault_shutdown(False, False)
    module.check_vault_shutdown(True, False)  # USD3 shuts down → alert
    module.check_vault_shutdown(True, False)  # still shut down → silent
    module.check_vault_shutdown(True, True)  # sUSD3 shuts down → alert

    assert [("sUSD3" in alert.message) for alert in alerts] == [False, True]
    assert all(alert.severity == module.AlertSeverity.CRITICAL for alert in alerts)


def test_usd3_oc_recovery_out_of_critical_rearms_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL at 105.9%, back to 106.6%, then 105.8%: the fresh crossing alerts despite a sub-step drop."""
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    for oc in (1.059, 1.066, 1.058):
        # deployed / (deployed - backing) == oc  =>  backing = deployed * (1 - 1 / oc)
        module.check_usd3_oc(100_000_000 * (1 - 1 / oc), 100_000_000)

    assert [alert.severity for alert in alerts] == [module.AlertSeverity.CRITICAL, module.AlertSeverity.CRITICAL]
    assert float(cache[module.CACHE_KEY_USD3_OC_ALERTED]) == pytest.approx(1.058)


def test_usd3_oc_recovery_does_not_send_extra_high(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL at 105.9%, back to 110.9%, then 110.3%: no HIGH while OC beats the alerted level."""
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    for oc in (1.059, 1.109, 1.103, 1.061):
        module.check_usd3_oc(100_000_000 * (1 - 1 / oc), 100_000_000)
    assert [alert.severity for alert in alerts] == [module.AlertSeverity.CRITICAL]

    # The recovery still re-armed CRITICAL.
    module.check_usd3_oc(100_000_000 * (1 - 1 / 1.0595), 100_000_000)
    assert [alert.severity for alert in alerts] == [module.AlertSeverity.CRITICAL, module.AlertSeverity.CRITICAL]
    assert float(cache[module.CACHE_KEY_USD3_OC_ALERTED]) == pytest.approx(1.0595)


def test_usd3_oc_flapping_around_critical_does_not_realert(monkeypatch: pytest.MonkeyPatch) -> None:
    """OC oscillating within one step of 106% alerts CRITICAL once, not on every crossing."""
    module = load_3jane_module()
    alerts: list = []
    cache = stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    for oc in (1.0599, 1.0605, 1.0597, 1.0640, 1.0595):
        module.check_usd3_oc(100_000_000 * (1 - 1 / oc), 100_000_000)

    assert [alert.severity for alert in alerts] == [module.AlertSeverity.CRITICAL]
    assert float(cache[module.CACHE_KEY_USD3_OC_ALERTED]) == pytest.approx(1.0599)


def test_withdraw_limit_drop_of_exactly_one_step_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alerted at $1M then drained to $0 is exactly one step and must alert."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(1_000_000)
    module.check_withdraw_limit(0)

    assert len(alerts) == 2
    assert "Available withdraw limit: $0.00" in alerts[1].message


def test_withdraw_limit_draining_below_one_step_alerts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alerted at $500k then drained to $0 alerts on the drain, and dust around $0 stays quiet."""
    module = load_3jane_module()
    alerts: list = []
    stub_cache(monkeypatch, module)
    monkeypatch.setattr(module, "send_alert", alerts.append)

    for limit in (500_000, 0, 15_000, 0):
        module.check_withdraw_limit(limit)

    assert len(alerts) == 2
    assert "Available withdraw limit: $500.00K" in alerts[0].message
    assert "Available withdraw limit: $0.00" in alerts[1].message


def test_read_vault_shutdown_uses_its_own_pinned_block_batch() -> None:
    """Shutdown flags come from a dedicated batch at the core reads' block."""
    module = load_3jane_module()
    seen_blocks: list[int] = []

    class FakeCall:
        def __init__(self, result: bool) -> None:
            self.result = result

        def call(self, block_identifier: int) -> bool:
            seen_blocks.append(block_identifier)
            return self.result

    class FakeVault:
        def __init__(self, shutdown: bool) -> None:
            self.functions = type("F", (), {"isShutdown": lambda _self: FakeCall(shutdown)})()

    class FakeBatch:
        def __init__(self) -> None:
            self.items: list[bool] = []

        def __enter__(self) -> "FakeBatch":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def add(self, item: bool) -> None:
            self.items.append(item)

    class FakeClient:
        def batch_requests(self) -> FakeBatch:
            return FakeBatch()

        def execute_batch(self, batch: FakeBatch) -> list[bool]:
            return batch.items

    assert module.read_vault_shutdown(FakeClient(), FakeVault(True), FakeVault(False), 123) == (True, False)
    assert seen_blocks == [123, 123]


@pytest.mark.parametrize("failure", [DefiLlamaVerifiedError("HTTP 403"), RuntimeError("unexpected failure")])
def test_main_preserves_onchain_and_envio_checks_on_verified_failure(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Run the core and Envio checks even if the Verified check fails."""
    module = load_3jane_module()
    stub_cache(monkeypatch, module)
    client = MagicMock()
    client.eth.block_number = 100
    client.execute_batch.side_effect = [
        [1, 1, 1, 1, 1, 1, 1, [0, 0, 1, 0], 1, False, 1, 1, 3600, 86400, 1],
        [1, module.RATE_SCALE],
    ]
    monkeypatch.setattr(module.ChainManager, "get_client", Mock(return_value=client))
    monkeypatch.setattr(module, "read_vault_shutdown", Mock(return_value=(False, False)))

    core_checks = (
        "check_pps",
        "check_tvl",
        "check_usd3_oc",
        "check_insurance_fund",
        "check_withdraw_limit",
        "check_vault_shutdown",
        "check_debt_cap",
        "check_nominal_backing_floor",
        "check_protocol_paused",
    )
    for name in core_checks:
        monkeypatch.setattr(module, name, Mock())
    borrower_check = Mock()
    fetch = Mock(side_effect=failure)
    monkeypatch.setattr(module, "check_borrower_default_watch", borrower_check)
    monkeypatch.setattr(module, "fetch_report", fetch)
    alerts = Mock()
    monkeypatch.setattr(module, "send_alert", alerts)

    module.main()

    fetch.assert_called_once_with(module.VERIFIED_FEED)
    borrower_check.assert_called_once_with(3600, 86400)
    for name in core_checks:
        getattr(module, name).assert_called_once()
    alerts.assert_not_called()


def test_verified_availability_escalates_and_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persist and escalate consecutive failures, then re-arm on a valid response."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    alerts: list[Alert] = []
    monkeypatch.setattr(module, "send_alert", alerts.append)
    path = Path(__file__).with_name("fixtures") / "defillama_verified_3jane.json"
    now = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
    report = parse_report(json.loads(path.read_text()), module.VERIFIED_FEED, now)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(module, "datetime", clock)
    failure = DefiLlamaVerifiedError("HTTP 403 (Cloudflare challenge)")
    fetch = Mock(side_effect=[failure] * 8 + [report] + [failure] * 2)
    monkeypatch.setattr(module, "fetch_report", fetch)

    for _ in range(8):
        module.check_defillama_verified()
    assert [(alert.severity.value, alert.protocol) for alert in alerts] == [
        ("LOW", "3jane"),
        ("MEDIUM", "3jane"),
    ]
    assert "2 consecutive runs" in alerts[0].message
    assert "6 consecutive runs" in alerts[1].message
    assert "Cloudflare challenge" in alerts[1].message

    module.check_defillama_verified()
    assert cache[module.CACHE_KEY_VERIFIED_FAILURE_STREAK] == "0"
    module.check_defillama_verified()
    module.check_defillama_verified()
    assert [alert.severity.value for alert in alerts] == ["LOW", "MEDIUM", "LOW"]


def test_verified_issues_dedupe_and_rearm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deduplicate repeated source issues and alert again after recovery."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    alerts: list[Alert] = []
    monkeypatch.setattr(module, "send_alert", alerts.append)
    path = Path(__file__).with_name("fixtures") / "defillama_verified_3jane.json"
    now = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
    report = parse_report(json.loads(path.read_text()), module.VERIFIED_FEED, now)
    degraded = replace(report, up_to_date=False, balances={})
    cache[module.CACHE_KEY_VERIFIED_FAILURE_STREAK] = "5"
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(module, "datetime", clock)
    fetch = Mock(side_effect=[degraded, degraded, report, degraded])
    monkeypatch.setattr(module, "fetch_report", fetch)

    for _ in range(4):
        module.check_defillama_verified()

    assert [(alert.severity.value, alert.protocol) for alert in alerts] == [
        ("MEDIUM", "3jane"),
        ("MEDIUM", "3jane"),
    ]
    assert "Erebor Bank" in alerts[0].message
    assert "out of date" in alerts[0].message
    assert cache[module.CACHE_KEY_VERIFIED_FAILURE_STREAK] == "0"


def test_main_checks_verified_even_when_onchain_reads_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """An RPC failure must not skip the offchain DefiLlama Verified check."""
    module = load_3jane_module()
    stub_cache(monkeypatch, module)
    client = MagicMock()
    client.eth.block_number = 100
    client.execute_batch.side_effect = RuntimeError("rpc down")
    monkeypatch.setattr(module.ChainManager, "get_client", Mock(return_value=client))
    fetch = Mock(side_effect=DefiLlamaVerifiedError("HTTP 403"))
    monkeypatch.setattr(module, "fetch_report", fetch)
    alerts: list[Alert] = []
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.main()

    fetch.assert_called_once_with(module.VERIFIED_FEED)
    assert [alert.severity for alert in alerts] == [module.AlertSeverity.LOW]
    assert "Monitoring Error" in alerts[0].message


def test_verified_unexpected_error_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-feed exception escalates through the outage streak instead of being swallowed."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    alerts: list[Alert] = []
    monkeypatch.setattr(module, "send_alert", alerts.append)
    monkeypatch.setattr(module, "fetch_report", Mock(side_effect=TypeError("unexpected field type")))

    module.check_defillama_verified()
    module.check_defillama_verified()

    assert cache[module.CACHE_KEY_VERIFIED_FAILURE_STREAK] == "2"
    assert [alert.severity for alert in alerts] == [module.AlertSeverity.LOW]
    assert "TypeError" in alerts[0].message


def test_verified_outage_alert_send_failure_counts_poll_once_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Telegram failure on the sixth failed poll must not double-count it, and MEDIUM retries next poll."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    sent: list[Alert] = []
    medium_attempts: list[int] = []

    def flaky_send(alert: Alert) -> bool:
        if alert.severity == module.AlertSeverity.MEDIUM and not medium_attempts:
            medium_attempts.append(1)
            raise TelegramError("telegram down")
        sent.append(alert)
        return True

    monkeypatch.setattr(module, "send_alert", flaky_send)
    monkeypatch.setattr(module, "fetch_report", Mock(side_effect=DefiLlamaVerifiedError("HTTP 403")))

    for _ in range(6):
        module.check_defillama_verified()
    assert cache[module.CACHE_KEY_VERIFIED_FAILURE_STREAK] == "6"
    assert [alert.severity.value for alert in sent] == ["LOW"]

    module.check_defillama_verified()
    module.check_defillama_verified()
    assert cache[module.CACHE_KEY_VERIFIED_FAILURE_STREAK] == "8"
    assert [alert.severity.value for alert in sent] == ["LOW", "MEDIUM"]
    assert "7 consecutive runs" in sent[1].message


def test_verified_issue_alert_send_failure_is_not_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed source-issue alert retries next run without counting toward the outage streak."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    path = Path(__file__).with_name("fixtures") / "defillama_verified_3jane.json"
    now = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
    report = parse_report(json.loads(path.read_text()), module.VERIFIED_FEED, now)
    degraded = replace(report, up_to_date=False)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(module, "datetime", clock)
    monkeypatch.setattr(module, "fetch_report", Mock(return_value=degraded))
    send = Mock(side_effect=[TelegramError("telegram down"), True])
    monkeypatch.setattr(module, "send_alert", send)

    module.check_defillama_verified()
    assert cache.get(module.CACHE_KEY_VERIFIED_FAILURE_STREAK, 0) in (0, "0")
    assert cache.get(module.CACHE_KEY_VERIFIED_ISSUES, 0) in (0, "")

    module.check_defillama_verified()
    assert send.call_count == 2
    assert "out of date" in send.call_args.args[0].message
    assert cache[module.CACHE_KEY_VERIFIED_ISSUES]


def test_verified_report_dates_history_tracks_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Store each originator's distinct reportDates, capped, so the real cadence can be measured."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    path = Path(__file__).with_name("fixtures") / "defillama_verified_3jane.json"
    now = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
    report = parse_report(json.loads(path.read_text()), module.VERIFIED_FEED, now)
    gigfi = ("Warehouse Loans", "GigFi")

    module.record_verified_report_dates(report)
    module.record_verified_report_dates(report)  # same dates: no new entries
    history = json.loads(cache[module.CACHE_KEY_VERIFIED_REPORT_DATES])
    assert history["GigFi (Warehouse Loans)"] == ["2026-11-13"]
    assert history["Settle (Forward Flows)"] == ["2026-11-11"]
    assert len(history) == 4

    for offset in range(1, module.VERIFIED_DATE_HISTORY_LENGTH + 2):
        loans = dict(report.loans)
        loans[gigfi] = replace(loans[gigfi], report_date=loans[gigfi].report_date + timedelta(days=7 * offset))
        module.record_verified_report_dates(replace(report, loans=loans))

    days = json.loads(cache[module.CACHE_KEY_VERIFIED_REPORT_DATES])["GigFi (Warehouse Loans)"]
    assert len(days) == module.VERIFIED_DATE_HISTORY_LENGTH
    assert days[-1] == "2027-02-12"


def test_verified_report_dates_history_ignores_corrupt_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed stored history is replaced rather than failing the Verified check."""
    module = load_3jane_module()
    cache = stub_cache(monkeypatch, module)
    cache[module.CACHE_KEY_VERIFIED_REPORT_DATES] = "{not json"
    path = Path(__file__).with_name("fixtures") / "defillama_verified_3jane.json"
    now = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
    report = parse_report(json.loads(path.read_text()), module.VERIFIED_FEED, now)

    module.record_verified_report_dates(report)

    assert json.loads(cache[module.CACHE_KEY_VERIFIED_REPORT_DATES])["Slope (Forward Flows)"] == ["2026-11-19"]
