import importlib.util
from pathlib import Path
from types import ModuleType


def load_3jane_module() -> ModuleType:
    path = Path(__file__).parents[1] / "protocols" / "3jane" / "main.py"
    spec = importlib.util.spec_from_file_location("three_jane", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_junior_buffer_uses_backing_over_deployed_credit(monkeypatch) -> None:
    module = load_3jane_module()
    messages: list[str] = []
    monkeypatch.setattr(module, "send_telegram_message", lambda message, _protocol: messages.append(message))

    module.check_junior_buffer(7_504_000, 37_776_000)

    assert messages == []


def test_junior_buffer_alert_describes_deployed_credit(monkeypatch) -> None:
    module = load_3jane_module()
    messages: list[str] = []
    monkeypatch.setattr(module, "send_telegram_message", lambda message, _protocol: messages.append(message))

    module.check_junior_buffer(5_000_000, 40_000_000)

    assert len(messages) == 1
    assert "12.50% of deployed credit" in messages[0]
    assert "sUSD3 backing: $5.00M | Deployed: $40.00M" in messages[0]


def test_insurance_fund_alerts_on_large_share_outflow(monkeypatch) -> None:
    module = load_3jane_module()
    messages: list[str] = []
    cached: list[tuple[str, float]] = []
    monkeypatch.setattr(module, "set_cache_value", lambda key, value: cached.append((key, value)))
    monkeypatch.setattr(module, "send_telegram_message", lambda message, _protocol: messages.append(message))

    module.check_insurance_fund(900_000_000_000, 850_000_000_000, 1_000_000, 58_000)

    assert len(messages) == 1
    assert "Outflow: $58.00K" in messages[0]
    assert cached == [(module.CACHE_KEY_INSURANCE_FUND_SHARES, 850_000_000_000.0)]


def test_insurance_fund_ignores_yield_and_small_outflows(monkeypatch) -> None:
    module = load_3jane_module()
    messages: list[str] = []
    monkeypatch.setattr(module, "set_cache_value", lambda _key, _value: None)
    monkeypatch.setattr(module, "send_telegram_message", lambda message, _protocol: messages.append(message))

    module.check_insurance_fund(900_000_000_000, 901_000_000_000, 1_050_000, 0)
    module.check_insurance_fund(900_000_000_000, 899_000_000_000, 1_048_000, 1_200)

    assert messages == []
