from typing import Any
from unittest.mock import MagicMock

import pytest

from protocols.infinifi import main as infinifi
from utils.alert import AlertSeverity


@pytest.fixture
def reserve_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"values": {}, "stale": False, "alerts": [], "errors": []}
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
    monkeypatch.setattr(infinifi, "send_error_message", lambda message, _protocol: state["errors"].append(message))
    return state


def run_main(monkeypatch: pytest.MonkeyPatch, asset_stats: dict[str, Any]) -> None:
    client = MagicMock()
    client.eth.block_number = 23_456_789
    client.get_contract.return_value.functions.totalSupply.return_value.call.return_value = 100_000_000 * 10**18
    monkeypatch.setattr(infinifi.ChainManager, "get_client", lambda _chain: client)
    monkeypatch.setattr(
        infinifi,
        "fetch_api_data",
        lambda: {"code": "OK", "data": {"stats": {"asset": asset_stats}}},
    )
    infinifi.main()


def test_infinifi_tvl_growth_triggers_breach_with_unchanged_reserves(reserve_state: dict[str, Any]) -> None:
    infinifi.check_liquid_reserves(8_000_000, 70_000_000)
    assert reserve_state["alerts"] == []

    infinifi.check_liquid_reserves(8_000_000, 100_000_000)
    infinifi.check_liquid_reserves(8_000_000, 100_000_000)

    assert len(reserve_state["alerts"]) == 1
    alert = reserve_state["alerts"][0]
    assert alert.severity == AlertSeverity.HIGH
    assert "greater of 10% of TVL and $4,000,000 floor ($10,000,000.00)" in alert.message
    assert "Liquid ratio: 8.00%" in alert.message


def test_infinifi_floor_applies_when_ten_percent_of_tvl_is_smaller(reserve_state: dict[str, Any]) -> None:
    infinifi.check_liquid_reserves(4_000_000, 30_000_000)
    assert reserve_state["alerts"] == []

    infinifi.check_liquid_reserves(3_999_999.99, 30_000_000)

    assert [alert.severity for alert in reserve_state["alerts"]] == [AlertSeverity.HIGH]
    assert "($4,000,000.00)" in reserve_state["alerts"][0].message


def test_infinifi_escalates_to_critical_and_rearms_after_partial_recovery(reserve_state: dict[str, Any]) -> None:
    infinifi.check_liquid_reserves(9_000_000, 100_000_000)
    infinifi.check_liquid_reserves(2_999_999.99, 100_000_000)
    infinifi.check_liquid_reserves(1_000_000, 100_000_000)
    infinifi.check_liquid_reserves(3_000_000, 100_000_000)
    infinifi.check_liquid_reserves(1_000_000, 100_000_000)

    alerts = reserve_state["alerts"]
    assert [alert.severity for alert in alerts] == [
        AlertSeverity.HIGH,
        AlertSeverity.CRITICAL,
        AlertSeverity.CRITICAL,
    ]
    assert "3% of TVL ($3,000,000.00)" in alerts[1].message
    assert "Liquid ratio: 3.00%" in alerts[1].message


def test_infinifi_recovery_at_boundary_and_monitoring_gap_rearm_breach(reserve_state: dict[str, Any]) -> None:
    infinifi.check_liquid_reserves(9_999_999.99, 100_000_000)
    infinifi.check_liquid_reserves(10_000_000, 100_000_000)
    infinifi.check_liquid_reserves(9_999_999.99, 100_000_000)
    assert len(reserve_state["alerts"]) == 2

    reserve_state["stale"] = True
    infinifi.check_liquid_reserves(9_999_999.99, 100_000_000)
    assert len(reserve_state["alerts"]) == 3


def test_infinifi_missing_tvl_falls_back_to_floor(reserve_state: dict[str, Any]) -> None:
    infinifi.check_liquid_reserves(4_000_000, 0)
    assert reserve_state["alerts"] == []

    infinifi.check_liquid_reserves(0, 0)

    assert [alert.severity for alert in reserve_state["alerts"]] == [AlertSeverity.HIGH]
    assert "Total backing: unavailable" in reserve_state["alerts"][0].message


@pytest.mark.parametrize("reserves", [None, "invalid", "missing", float("nan"), float("inf"), -1])
def test_infinifi_main_reports_unavailable_reserves(
    monkeypatch: pytest.MonkeyPatch, reserve_state: dict[str, Any], reserves: Any
) -> None:
    asset_stats: dict[str, Any] = {"totalTVLAssetNormalized": 100_000_000, "pendingRedemptionsAssetNormalized": 5}
    if reserves != "missing":
        asset_stats["totalLiquidAssetNormalized"] = reserves

    run_main(monkeypatch, asset_stats)

    assert reserve_state["alerts"] == []
    assert reserve_state["errors"] == ["Infinifi API liquid reserves missing or invalid; reserve check skipped"]
    assert f"{infinifi.PROTOCOL}_liquid_reserves_breach" not in reserve_state["values"]
    assert f"{infinifi.PROTOCOL}_redemption_pressure_breach" not in reserve_state["values"]


@pytest.mark.parametrize("tvl", ["missing", None, 0, float("nan"), float("inf")])
def test_infinifi_main_reports_unavailable_tvl_and_uses_floor(
    monkeypatch: pytest.MonkeyPatch, reserve_state: dict[str, Any], tvl: Any
) -> None:
    asset_stats: dict[str, Any] = {"totalLiquidAssetNormalized": 3_000_000}
    if tvl != "missing":
        asset_stats["totalTVLAssetNormalized"] = tvl

    run_main(monkeypatch, asset_stats)

    assert reserve_state["errors"] == ["Infinifi API TVL missing or invalid; reserve check limited to $4,000,000 floor"]
    assert [alert.severity for alert in reserve_state["alerts"]] == [AlertSeverity.HIGH]


def test_infinifi_main_zero_reserves_alert_critical_and_redemption_pressure(
    monkeypatch: pytest.MonkeyPatch, reserve_state: dict[str, Any]
) -> None:
    run_main(
        monkeypatch,
        {
            "totalLiquidAssetNormalized": 0,
            "totalTVLAssetNormalized": 100_000_000,
            "pendingRedemptionsAssetNormalized": 5_000_000,
        },
    )

    alerts = reserve_state["alerts"]
    assert reserve_state["errors"] == []
    assert [alert.severity for alert in alerts] == [AlertSeverity.CRITICAL, AlertSeverity.HIGH]
    assert "Liquid ratio: 0.00%" in alerts[0].message
    assert "unbounded (no liquid reserves)" in alerts[1].message


def test_infinifi_main_zero_reserves_without_pending_redemptions_clears_pressure(
    monkeypatch: pytest.MonkeyPatch, reserve_state: dict[str, Any]
) -> None:
    run_main(monkeypatch, {"totalLiquidAssetNormalized": 0, "totalTVLAssetNormalized": 100_000_000})

    assert [alert.severity for alert in reserve_state["alerts"]] == [AlertSeverity.CRITICAL]
    assert reserve_state["values"][f"{infinifi.PROTOCOL}_redemption_pressure_breach"] == 0
