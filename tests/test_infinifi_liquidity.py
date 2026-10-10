from unittest.mock import MagicMock

import pytest

from protocols.infinifi import main as infinifi
from utils.alert import AlertSeverity


@pytest.fixture
def reserve_state(monkeypatch):
    state = {"values": {}, "stale": False, "alerts": []}
    monkeypatch.setattr(
        infinifi,
        "get_fresh_last_value_for_key_from_file",
        lambda _filename, key, _age: 0 if state["stale"] else state["values"].get(key, 0),
    )
    monkeypatch.setattr(
        infinifi,
        "write_last_value_with_timestamp_to_file",
        lambda _filename, key, value: state["values"].__setitem__(key, value),
    )
    monkeypatch.setattr(infinifi, "send_alert", state["alerts"].append)
    return state


def test_infinifi_tvl_growth_triggers_breach_with_unchanged_reserves(reserve_state):
    infinifi.check_liquid_reserves(8_000_000, 70_000_000)
    assert reserve_state["alerts"] == []

    infinifi.check_liquid_reserves(8_000_000, 100_000_000)
    infinifi.check_liquid_reserves(8_000_000, 100_000_000)

    assert len(reserve_state["alerts"]) == 1
    alert = reserve_state["alerts"][0]
    assert alert.severity == AlertSeverity.HIGH
    assert "10% of TVL ($10,000,000.00)" in alert.message
    assert "Liquid ratio: 8.00%" in alert.message


def test_infinifi_recovery_at_boundary_and_monitoring_gap_rearm_breach(reserve_state):
    infinifi.check_liquid_reserves(9_999_999.99, 100_000_000)
    infinifi.check_liquid_reserves(10_000_000, 100_000_000)
    infinifi.check_liquid_reserves(9_999_999.99, 100_000_000)
    assert len(reserve_state["alerts"]) == 2

    reserve_state["stale"] = True
    infinifi.check_liquid_reserves(9_999_999.99, 100_000_000)
    assert len(reserve_state["alerts"]) == 3


@pytest.mark.parametrize(
    ("reserves", "backing"),
    [(float("nan"), 100), (-1, 100), (10, 0), (10, -1), (10, float("inf")), (10, float("nan"))],
)
def test_infinifi_invalid_data_does_not_change_breach_state(reserve_state, reserves, backing):
    infinifi.check_liquid_reserves(reserves, backing)
    assert reserve_state["alerts"] == []
    assert reserve_state["values"] == {}


@pytest.mark.parametrize("reserves", [0, None, "invalid", "missing"])
def test_infinifi_main_distinguishes_zero_reserves_from_unavailable_data(monkeypatch, reserve_state, reserves):
    client = MagicMock()
    client.eth.block_number = 23_456_789
    client.get_contract.return_value.functions.totalSupply.return_value.call.return_value = 100_000_000 * 10**18
    asset_stats = {"totalTVLAssetNormalized": 100_000_000}
    if reserves != "missing":
        asset_stats["totalLiquidAssetNormalized"] = reserves
    monkeypatch.setattr(infinifi.ChainManager, "get_client", lambda _chain: client)
    monkeypatch.setattr(
        infinifi,
        "fetch_api_data",
        lambda: {"code": "OK", "data": {"stats": {"asset": asset_stats}}},
    )
    monkeypatch.setattr(infinifi, "send_error_message", lambda *_args: pytest.fail("Unexpected monitoring error"))

    infinifi.main()

    alerts = reserve_state["alerts"]
    assert len(alerts) == (1 if reserves == 0 else 0)
    if reserves == 0:
        assert "Liquid ratio: 0.00%" in alerts[0].message
