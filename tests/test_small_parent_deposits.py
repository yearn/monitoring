from decimal import Decimal

from protocols.yearn import alert_small_parent_deposits as monitor
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


def make_event(*, assets: str = "9999999999", block_number: int = 100, log_index: int = 2) -> dict:
    return {
        "id": f"1_{block_number}_{log_index}",
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


def test_small_deposit_uses_normalized_token_units() -> None:
    threshold = Decimal("10000")

    assert monitor.format_units("1234567", 6) == Decimal("1.234567")
    assert monitor.is_small_deposit("9999999999", 6, threshold)
    assert not monitor.is_small_deposit("10000000000", 6, threshold)
    assert not monitor.is_small_deposit("10000000001", 6, threshold)
    assert not monitor.is_small_deposit("0", 6, threshold)


def test_process_event_sends_low_alert_with_all_addresses() -> None:
    alerts = []

    did_alert = monitor.process_event(
        make_event(),
        {"0xparent": VAULT},
        Decimal("10000"),
        alert_sender=alerts.append,
    )

    assert did_alert
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity is AlertSeverity.LOW
    assert alert.protocol == "yearn"
    assert "9,999.999999 USDC" in alert.message
    assert "0xOwner" in alert.message
    assert "0xSender" in alert.message
    assert "0xTransactionFrom" in alert.message
    assert "0xTransaction" in alert.message


def test_process_event_does_not_alert_at_threshold() -> None:
    alerts = []

    did_alert = monitor.process_event(
        make_event(assets="10000000000"),
        {"0xparent": VAULT},
        Decimal("10000"),
        alert_sender=alerts.append,
    )

    assert not did_alert
    assert alerts == []


def test_monitor_chain_pages_and_persists_each_processed_event(monkeypatch) -> None:
    first = make_event(block_number=100, log_index=2)
    second = make_event(block_number=101, log_index=3)
    calls = []
    saved = []

    monkeypatch.setattr(monitor, "fetch_kong_parent_vaults", lambda _chain: [VAULT])
    monkeypatch.setattr(monitor, "load_cursor", lambda _chain_id: None)
    monkeypatch.setattr(monitor, "save_cursor", lambda chain_id, cursor: saved.append((chain_id, cursor)))
    monkeypatch.setattr(monitor, "process_event", lambda event, *_args: event is first)

    def fake_load(chain_id, addresses, cursor, since_ts, limit):
        calls.append((chain_id, addresses, cursor, since_ts, limit))
        if len(calls) == 1:
            return [first, second]
        return []

    monkeypatch.setattr(monitor, "load_deposits", fake_load)

    processed, alerted = monitor.monitor_chain(
        Chain.MAINNET,
        Decimal("10000"),
        lookback_seconds=7200,
        page_size=2,
        now=1_700_010_000,
    )

    assert (processed, alerted) == (2, 1)
    assert calls[0][0] == 1
    assert calls[0][2] == monitor.EventCursor(0, -1)
    assert calls[0][3] == 1_700_002_800
    assert calls[1][2] == monitor.EventCursor(101, 3)
    assert saved == [
        (1, monitor.EventCursor(100, 2)),
        (1, monitor.EventCursor(101, 3)),
    ]
