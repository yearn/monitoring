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


def test_junior_buffer_uses_backing_over_deployed_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(7_504_000, 37_776_000)

    assert alerts == []


def test_junior_buffer_alert_describes_deployed_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_junior_buffer(5_000_000, 40_000_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.HIGH
    assert "12.50% of deployed credit" in alerts[0].message
    assert "sUSD3 backing: $5.00M | Deployed: $40.00M" in alerts[0].message


def test_usd3_oc_does_not_alert_above_high_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_usd3_oc(11_000_000, 100_000_000)

    assert alerts == []


def test_usd3_oc_alerts_high_below_target(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
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
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(3_500_000)

    assert len(alerts) == 1
    assert alerts[0].severity == module.AlertSeverity.MEDIUM
    assert "Available withdraw limit: $3.50M" in alerts[0].message
    assert "threshold $4.00M" in alerts[0].message


def test_withdraw_limit_no_alert_at_or_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_3jane_module()
    alerts: list = []
    monkeypatch.setattr(module, "send_alert", alerts.append)

    module.check_withdraw_limit(module.WITHDRAW_LIMIT_THRESHOLD)
    module.check_withdraw_limit(4_548_324)

    assert alerts == []


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


def test_parse_envio_borrower_default_watch_rows_dedupes_and_requires_alert_bucket() -> None:
    module = load_3jane_module()
    market_id = "0x" + "12" * 32
    borrower = "0x00000000000000000000000000000000000000a1"

    parsed = module.parse_envio_borrower_default_watch_rows(
        [
            {
                "marketId": market_id,
                "borrower": borrower,
                "credit": str(2_000_000 * module.ONE_SHARE),
                "amountDue": str(250_000 * module.ONE_SHARE),
                "cycleId": "4",
                "cycleEnd": "1700000000",
                "endingBalance": str(1_000_000 * module.ONE_SHARE),
                "defaultAt": str(1_700_000_000 + 30 * module.SECONDS_PER_DAY),
                "secondsToDefault": str(6 * module.SECONDS_PER_DAY),
                "secondsSinceDefault": "0",
                "repaymentStatus": "Delinquent",
                "defaultBucket": "7d",
                "settled": False,
            },
            {
                "marketId": market_id.upper(),
                "borrower": borrower,
                "amountDue": str(250_000 * module.ONE_SHARE),
                "defaultBucket": "7d",
                "settled": "false",
            },
            {"marketId": market_id, "borrower": borrower, "amountDue": "1", "defaultBucket": "7d", "settled": True},
            {"marketId": market_id, "borrower": borrower, "amountDue": "1", "settled": False},
            {"marketId": market_id, "borrower": borrower, "amountDue": "0", "defaultBucket": "7d"},
            {
                "marketId": market_id,
                "borrower": borrower,
                "amountDue": "1",
                "repaymentStatus": "GracePeriod",
                "defaultBucket": "7d",
            },
            {"marketId": "bad", "borrower": borrower, "amountDue": "1", "defaultBucket": "7d"},
            {"marketId": market_id, "borrower": "not-an-address", "amountDue": "1", "defaultBucket": "7d"},
        ]
    )

    assert parsed == [
        module.BorrowerRepaymentSnapshot(
            market_id=market_id,
            borrower=module.Web3.to_checksum_address(borrower),
            cycle_id=4,
            cycle_end=1_700_000_000,
            amount_due_raw=250_000 * module.ONE_SHARE,
            ending_balance_raw=1_000_000 * module.ONE_SHARE,
            credit_raw=2_000_000 * module.ONE_SHARE,
            repayment_status="Delinquent",
            default_at=1_700_000_000 + 30 * module.SECONDS_PER_DAY,
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
