from decimal import Decimal

from protocols.yearn import alert_small_parent_flows as monitor
from utils.alert import AlertSeverity
from utils.chains import Chain

VAULT = {
    "address": "0xParent",
    "name": "USDC yVault",
    "symbol": "yvUSDC",
    "decimals": 6,
    "asset_address": "0xAsset",
    "asset_symbol": "USDC",
    "asset_decimals": 6,
}


def make_event(
    *,
    flow_type: str = "deposit",
    assets: str = "5000",
    block_number: int = 100,
    log_index: int = 2,
) -> dict:
    event = {
        "id": f"1_{block_number}_{log_index}",
        "flow_type": flow_type,
        "vaultAddress": "0xParent",
        "chainId": 1,
        "blockNumber": block_number,
        "blockTimestamp": 1_700_000_000,
        "transactionHash": "0xTransaction",
        "transactionFrom": "0xTransactionFrom",
        "logIndex": log_index,
        "sender": "0xSender",
        "owner": "0xOwner",
        "assets": assets,
        "shares": assets,
    }
    if flow_type == "withdrawal":
        event["receiver"] = "0xReceiver"
    return event


def test_small_flow_uses_raw_asset_units() -> None:
    assert monitor.format_units("1234567", 6) == Decimal("1.234567")
    assert monitor.is_small_flow("9999", 10_000)
    assert not monitor.is_small_flow("10000", 10_000)
    assert not monitor.is_small_flow("10001", 10_000)
    assert not monitor.is_small_flow("0", 10_000)


def test_process_deposit_sends_low_alert_with_all_addresses() -> None:
    alerts = []

    did_alert = monitor.process_event(
        make_event(),
        {"0xparent": VAULT},
        10_000,
        alert_sender=alerts.append,
    )

    assert did_alert
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity is AlertSeverity.LOW
    assert alert.protocol == "yearn"
    assert "Small parent-vault deposit" in alert.message
    assert "Raw Assets: 5,000" in alert.message
    assert "Normalized: 0.005 USDC" in alert.message
    assert "Raw Threshold: < 10,000" in alert.message
    assert "0xOwner" in alert.message
    assert "0xSender" in alert.message
    assert "0xTransactionFrom" in alert.message
    assert "0xTransaction" in alert.message
    assert "Receiver" not in alert.message


def test_process_withdrawal_includes_asset_receiver() -> None:
    alerts = []

    did_alert = monitor.process_event(
        make_event(flow_type="withdrawal"),
        {"0xparent": VAULT},
        10_000,
        alert_sender=alerts.append,
    )

    assert did_alert
    assert len(alerts) == 1
    assert "Small parent-vault withdrawal" in alerts[0].message
    assert "Receiver" in alerts[0].message
    assert "0xReceiver" in alerts[0].message


def test_process_event_does_not_alert_at_threshold() -> None:
    alerts = []

    did_alert = monitor.process_event(
        make_event(assets="10000"),
        {"0xparent": VAULT},
        10_000,
        alert_sender=alerts.append,
    )

    assert not did_alert
    assert alerts == []


def test_load_events_selects_envio_entity_and_receiver(monkeypatch) -> None:
    queries = []

    def fake_gql(query, variables):
        queries.append((query, variables))
        return {"data": {"events": [make_event(flow_type="withdrawal")]}}

    monkeypatch.setattr(monitor, "gql_request", fake_gql)

    events = monitor.load_events(
        "withdrawal",
        1,
        ["0xParent"],
        monitor.EventCursor(10, 2),
        1_700_000_000,
        100,
    )

    assert "events: Withdraw(" in queries[0][0]
    assert "receiver" in queries[0][0]
    assert queries[0][1]["lastBlock"] == 10
    assert events and events[0]["flow_type"] == "withdrawal"


def test_monitor_flow_type_pages_and_persists_each_processed_event(monkeypatch) -> None:
    first = make_event(flow_type="withdrawal", block_number=100, log_index=2)
    second = make_event(flow_type="withdrawal", block_number=101, log_index=3)
    calls = []
    saved = []

    monkeypatch.setattr(monitor, "load_cursor", lambda _chain_id, _flow_type: None)
    monkeypatch.setattr(
        monitor,
        "save_cursor",
        lambda chain_id, flow_type, cursor: saved.append((chain_id, flow_type, cursor)),
    )
    monkeypatch.setattr(monitor, "process_event", lambda event, *_args: event is first)

    def fake_load(flow_type, chain_id, addresses, cursor, since_ts, limit):
        calls.append((flow_type, chain_id, addresses, cursor, since_ts, limit))
        if len(calls) == 1:
            return [first, second]
        return []

    monkeypatch.setattr(monitor, "load_events", fake_load)

    processed, alerted = monitor.monitor_flow_type(
        1,
        "withdrawal",
        ["0xParent"],
        {"0xparent": VAULT},
        10_000,
        lookback_seconds=7200,
        page_size=2,
        now=1_700_010_000,
    )

    assert (processed, alerted) == (2, 1)
    assert calls[0][0] == "withdrawal"
    assert calls[0][1] == 1
    assert calls[0][3] == monitor.EventCursor(0, -1)
    assert calls[0][4] == 1_700_002_800
    assert calls[1][3] == monitor.EventCursor(101, 3)
    assert saved == [
        (1, "withdrawal", monitor.EventCursor(100, 2)),
        (1, "withdrawal", monitor.EventCursor(101, 3)),
    ]


def test_monitor_chain_runs_deposit_and_withdrawal_streams(monkeypatch) -> None:
    flow_types = []

    monkeypatch.setattr(monitor, "fetch_kong_parent_vaults", lambda _chain: [VAULT])

    def fake_monitor(_chain_id, flow_type, *_args):
        flow_types.append(flow_type)
        return 1, 1

    monkeypatch.setattr(monitor, "monitor_flow_type", fake_monitor)

    result = monitor.monitor_chain(Chain.MAINNET, 10_000, 7200, 1000)

    assert result == (2, 2)
    assert flow_types == ["deposit", "withdrawal"]
