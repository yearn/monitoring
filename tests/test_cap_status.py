from collections.abc import Sequence
from types import SimpleNamespace

import pytest

import protocols.cap.status as status
from utils.alert import Alert


def stub_cache(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Replace status cache reads and writes with an in-memory mapping."""
    cache: dict[str, str] = {}
    monkeypatch.setattr(
        status,
        "get_last_value_for_key_from_file",
        lambda _filename, key: cache.get(key, 0),
    )
    monkeypatch.setattr(
        status,
        "write_last_value_to_file",
        lambda _filename, key, value: cache.__setitem__(key, str(value)),
    )
    return cache


def make_status_client(responses: Sequence[int | None]) -> tuple[SimpleNamespace, list[object]]:
    """Build a batch-capable fake client and capture its submitted calls."""
    added_calls: list[object] = []

    class Batch:
        def __enter__(self) -> "Batch":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def add(self, call: object) -> None:
            added_calls.append(call)

        def execute(self) -> list[int | None]:
            return list(responses)

    functions = SimpleNamespace(
        balanceOf=lambda _owner: "balanceOf",
        totalAssets=lambda: "totalAssets",
        lockedProfit=lambda: "lockedProfit",
        convertToAssets=lambda _shares: "convertToAssets",
    )
    contract = SimpleNamespace(functions=functions)
    client = SimpleNamespace(
        eth=SimpleNamespace(contract=lambda **_kwargs: contract),
        batch_requests=Batch,
    )
    return client, added_calls


def test_stcusd_backing_deficit_sends_one_critical_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    cache = stub_cache(monkeypatch)
    monkeypatch.setattr(status, "send_alert", alerts.append)
    state = status.StcUsdState(
        cusd_balance=109 * status.ONE_STCUSD,
        total_assets=100 * status.ONE_STCUSD,
        locked_profit=10 * status.ONE_STCUSD,
        assets_per_share=status.ONE_STCUSD,
    )

    status.check_stcusd_backing(state)
    status.check_stcusd_backing(state)

    assert len(alerts) == 1
    assert alerts[0].severity == status.AlertSeverity.CRITICAL
    assert "Shortfall: 1.000000 cUSD" in alerts[0].message
    assert cache[status.CACHE_KEY_STCUSD_BACKING_DEFICIT] == "1"


def test_stcusd_backing_recovery_rearms_next_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(status, "send_alert", alerts.append)
    deficient = status.StcUsdState(109, 100, 10, 1)
    healthy = status.StcUsdState(110, 100, 10, 1)

    status.check_stcusd_backing(deficient)
    status.check_stcusd_backing(healthy)
    status.check_stcusd_backing(deficient)

    assert len(alerts) == 2


def test_stcusd_assets_per_share_decrease_is_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    cache = stub_cache(monkeypatch)
    monkeypatch.setattr(status, "send_alert", alerts.append)

    status.check_stcusd_assets_per_share(1_100_000_000_000_000_000)
    status.check_stcusd_assets_per_share(1_090_000_000_000_000_000)

    assert len(alerts) == 1
    assert alerts[0].severity == status.AlertSeverity.CRITICAL
    assert "Decrease: 0.010000 cUSD per stcUSD" in alerts[0].message
    assert cache[status.CACHE_KEY_STCUSD_ASSETS_PER_SHARE] == "1090000000000000000"


def test_stcusd_assets_per_share_increase_does_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(status, "send_alert", alerts.append)

    status.check_stcusd_assets_per_share(1_000_000_000_000_000_000)
    status.check_stcusd_assets_per_share(1_010_000_000_000_000_000)

    assert alerts == []


def test_load_status_batches_all_calls() -> None:
    responses = [120, 100, 10, 1_050_000_000_000_000_000]
    client, added_calls = make_status_client(responses)

    stcusd_state = status.load_status(client)

    assert added_calls == ["balanceOf", "totalAssets", "lockedProfit", "convertToAssets"]
    assert stcusd_state == status.StcUsdState(120, 100, 10, 1_050_000_000_000_000_000)


def test_load_status_rejects_missing_numeric_response() -> None:
    client, _added_calls = make_status_client([120, None, 10, 1_050_000_000_000_000_000])

    with pytest.raises(RuntimeError, match="stcUSD totalAssets"):
        status.load_status(client)


def test_main_checks_all_status_values(monkeypatch: pytest.MonkeyPatch) -> None:
    state = status.StcUsdState(120, 100, 10, 1_050_000_000_000_000_000)
    observed: list[object] = []

    monkeypatch.setattr(status.ChainManager, "get_client", lambda _chain: object())
    monkeypatch.setattr(status, "load_status", lambda _client: state)
    monkeypatch.setattr(status, "check_stcusd_backing", lambda value: observed.append(("backing", value)))
    monkeypatch.setattr(
        status,
        "check_stcusd_assets_per_share",
        lambda value: observed.append(("assets_per_share", value)),
    )

    status.main()

    assert observed == [
        ("backing", state),
        ("assets_per_share", state.assets_per_share),
    ]
