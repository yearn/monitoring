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
