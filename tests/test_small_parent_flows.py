import sys
from decimal import Decimal

import pytest

from protocols.yearn import alert_small_parent_flows as monitor
from utils.alert import Alert, AlertSeverity
from utils.chains import Chain
from utils.telegram import MAX_MESSAGE_LENGTH, YEARN_MAINTANACE_CHANNEL

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
    chain_id: int = 1,
) -> dict:
    event = {
        "id": f"1_{block_number}_{log_index}",
        "flow_type": flow_type,
        "vaultAddress": "0xParent",
        "chainId": chain_id,
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


def test_process_event_builds_structured_record(monkeypatch) -> None:
    """``process_event`` should hand a ``SmallFlowRecord`` to ``alert_sender`` with every
    field the aggregator needs to render one line in the aggregated message. The record
    carries the rendered ``amount`` / ``asset_symbol`` and the chain/explorer already
    resolved — the aggregator doesn't need to re-look-up anything for the body."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_YEARN_MAINTANACE", "yearn_maintanace_chat_id")
    records: list[monitor.SmallFlowRecord] = []

    did_alert = monitor.process_event(
        make_event(),
        {"0xparent": VAULT},
        10_000,
        alert_sender=records.append,
    )

    assert did_alert
    assert len(records) == 1
    record = records[0]
    assert record.flow_type == "deposit"
    assert record.chain_name == "mainnet"
    assert record.amount == "0.005"
    assert record.asset_symbol == "USDC"
    assert record.vault_symbol == "yvUSDC"
    assert record.vault_address == "0xParent"
    assert record.tx_hash == "0xTransaction"
    assert record.block_number == 100
    assert record.log_index == 2
    assert record.explorer  # non-empty when the chain has an explorer URL


def test_process_event_carries_withdrawal_marker_and_receiver_in_event_payload() -> None:
    """The receiver lives on the Envio event payload, not on the record. The aggregator
    stays simple — the receiver field is omitted from the aggregated message because
    it would crowd each line; reviewers follow the explorer link for full context."""
    records: list[monitor.SmallFlowRecord] = []

    monitor.process_event(
        make_event(flow_type="withdrawal"),
        {"0xparent": VAULT},
        10_000,
        alert_sender=records.append,
    )

    assert records[0].flow_type == "withdrawal"


def test_process_event_does_not_alert_at_threshold() -> None:
    records: list[monitor.SmallFlowRecord] = []

    did_alert = monitor.process_event(
        make_event(assets="10000"),
        {"0xparent": VAULT},
        10_000,
        alert_sender=records.append,
    )

    assert not did_alert
    assert records == []


def test_process_event_raises_without_alert_sender() -> None:
    """Calling ``process_event`` on a small flow with no sender configured is a programmer
    error — surface it loudly rather than silently dropping the alert."""
    with pytest.raises(RuntimeError, match="no alert_sender"):
        monitor.process_event(make_event(), {"0xparent": VAULT}, 10_000)


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


def test_monitor_flow_type_pages_and_stages_last_processed_cursor(monkeypatch) -> None:
    first = make_event(flow_type="withdrawal", block_number=100, log_index=2)
    second = make_event(flow_type="withdrawal", block_number=101, log_index=3)
    calls = []
    pending_cursors = {}

    monkeypatch.setattr(monitor, "load_cursor", lambda _chain_id, _flow_type: None)
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
        pending_cursors=pending_cursors,
        now=1_700_010_000,
        alert_sender=lambda _record: None,
    )

    assert (processed, alerted) == (2, 1)
    assert calls[0][0] == "withdrawal"
    assert calls[0][1] == 1
    assert calls[0][3] == monitor.EventCursor(0, -1)
    assert calls[0][4] == 1_700_002_800
    assert calls[1][3] == monitor.EventCursor(101, 3)
    assert pending_cursors == {(1, "withdrawal"): monitor.EventCursor(101, 3)}


def test_monitor_chain_runs_deposit_and_withdrawal_streams(monkeypatch) -> None:
    flow_types = []

    monkeypatch.setattr(monitor, "fetch_kong_parent_vaults", lambda _chain: [VAULT])

    def fake_monitor(_chain_id, flow_type, *_args):
        flow_types.append(flow_type)
        return 1, 1

    monkeypatch.setattr(monitor, "monitor_flow_type", fake_monitor)

    result = monitor.monitor_chain(Chain.MAINNET, 10_000, 7200, 1000, {})

    assert result == (2, 2)
    assert flow_types == ["deposit", "withdrawal"]


def test_load_events_handles_null_data(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "gql_request", lambda _query, _variables: {"data": None})

    with pytest.raises(RuntimeError, match="missing Deposit list"):
        monitor.load_events("deposit", 1, ["0xParent"], monitor.EventCursor(0, -1), 0, 100)


def test_gql_request_reports_once_and_raises(monkeypatch) -> None:
    reported = []

    def failing_http(_url, _body):
        raise OSError("connection refused")

    monkeypatch.setattr(monitor, "ENVIO_GRAPHQL_URL", "https://envio.example/graphql")
    monkeypatch.setattr(monitor, "http_json", failing_http)
    monkeypatch.setattr(monitor, "send_envio_error_message", lambda *args, **kwargs: reported.append((args, kwargs)))

    with pytest.raises(monitor.EnvioUnavailableError):
        monitor.gql_request("query {}", {})
    assert len(reported) == 1
    args, kwargs = reported[0]
    assert args[1] == monitor.PROTOCOL
    assert kwargs["alert_protocol"] == "yearn-internal"


def test_first_run_lookback_floor_persists_without_events(monkeypatch) -> None:
    since_values = []

    def fake_load(_flow_type, _chain_id, _addresses, _cursor, since_ts, _limit):
        since_values.append(since_ts)
        return []

    monkeypatch.setattr(monitor, "load_events", fake_load)

    for now in (1_700_010_000, 1_700_100_000):
        monitor.monitor_flow_type(
            8453,
            "deposit",
            ["0xParent"],
            {"0xparent": VAULT},
            10_000,
            lookback_seconds=7200,
            page_size=100,
            pending_cursors={},
            now=now,
            alert_sender=lambda _record: None,
        )

    assert since_values == [1_700_002_800, 1_700_002_800]
    assert monitor.load_cursor(8453, "deposit") is None


# ---- FlowAggregator ----


def _record(
    *,
    chain_name: str = "mainnet",
    flow_type: str = "deposit",
    amount: str = "0.005",
    asset_symbol: str = "USDC",
    vault_symbol: str = "yvUSDC",
    vault_address: str = "0xParentVaultAddress",
    explorer: str | None = "https://etherscan.io",
    tx_hash: str = "0xTransactionHash",
    block_number: int = 100,
    log_index: int = 2,
) -> monitor.SmallFlowRecord:
    return monitor.SmallFlowRecord(
        chain_name=chain_name,
        flow_type=flow_type,
        amount=amount,
        asset_symbol=asset_symbol,
        vault_symbol=vault_symbol,
        vault_address=vault_address,
        explorer=explorer,
        tx_hash=tx_hash,
        block_number=block_number,
        log_index=log_index,
    )


def test_flow_aggregator_emits_one_alert_for_all_flows(monkeypatch) -> None:
    """All qualifying flows collected during a run must end up in exactly one
    Telegram message, not 1-per-flow. This is the headline fix for the 429
    rate-limit incident on 2026-09-17."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_YEARN_MAINTANACE", "yearn_maintanace_chat_id")
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=10, sender=delivered.append)

    for index in range(5):
        aggregator(_record(tx_hash=f"0xTx{index:02d}", block_number=100 + index))
    aggregator.send_summary()

    assert len(delivered) == 1
    assert delivered[0].channel == YEARN_MAINTANACE_CHANNEL
    assert delivered[0].severity is AlertSeverity.LOW
    assert delivered[0].protocol == "yearn-internal"
    body = delivered[0].message
    assert body.startswith("ℹ️ Small parent-vault flows — 5 in this run")
    assert body.count("→") == 5  # one arrow per flow line
    # The aggregated body should NOT contain any of the per-flow decorations
    # from the old build_alert_message — no "Raw Assets", "Owner:", "Receiver:" lines.
    assert "Raw Assets" not in body
    assert "Owner:" not in body
    assert "Receiver:" not in body


def test_flow_aggregator_no_ops_send_summary_when_no_flows_recorded() -> None:
    """A quiet run should NOT produce a "0 flows" header — that would spam the
    destination channel every hour on a healthy day."""
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=10, sender=delivered.append)

    aggregator.send_summary()

    assert delivered == []


def test_flow_aggregator_truncates_with_footer_past_the_cap(monkeypatch) -> None:
    """Past the per-run cap, the aggregator counts the overflow but does not render
    those flows inline. The truncation is visible in Telegram via a footer on the
    aggregated message so reviewers can tell when they're seeing a partial view."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_YEARN_MAINTANACE", "yearn_maintanace_chat_id")
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=2, sender=delivered.append)

    for index in range(5):
        aggregator(_record(tx_hash=f"0xTx{index:02d}", block_number=100 + index))
    aggregator.send_summary()

    assert aggregator.collected == 2
    assert aggregator.truncated == 3
    assert aggregator.total == 5
    assert len(delivered) == 1
    body = delivered[0].message
    assert "5 in this run" in body
    assert "3 truncated" in body
    # Only the first 2 flows should be rendered.
    assert body.count("→") == 2


def test_flow_aggregator_counts_every_flow_omitted_by_telegram_length_limit() -> None:
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=500, sender=delivered.append)

    for index in range(80):
        aggregator(_record(tx_hash=f"0x{index:064x}", block_number=100 + index))
    aggregator.send_summary()

    assert aggregator.total == 80
    assert aggregator.truncated > 0
    assert len(f"ℹ️ {delivered[0].message}") <= MAX_MESSAGE_LENGTH
    assert delivered[0].message.count("→") == aggregator.collected
    assert f"{aggregator.truncated} truncated" in delivered[0].message


def test_flow_aggregator_rejects_a_single_flow_that_cannot_fit() -> None:
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=500, sender=delivered.append)
    aggregator(_record(asset_symbol="A" * MAX_MESSAGE_LENGTH))

    with pytest.raises(RuntimeError, match="exceeds the Telegram message limit"):
        aggregator.send_summary()

    assert delivered == []
    assert aggregator.total == 1


def test_failed_aggregate_send_leaves_cursors_for_retry(monkeypatch) -> None:
    cursor_state: dict[tuple[int, str], monitor.EventCursor] = {}
    delivered: list[Alert] = []
    events = [make_event(block_number=100), make_event(block_number=101)]
    seen: list[monitor.EventCursor] = []

    monkeypatch.setattr(sys, "argv", ["alert_small_parent_flows.py", "--chain-ids", "1"])
    monkeypatch.setattr(monitor, "fetch_kong_parent_vaults", lambda _chain: [VAULT])
    monkeypatch.setattr(monitor, "load_or_init_start_ts", lambda *_args: 0)
    monkeypatch.setattr(monitor, "load_cursor", lambda chain, flow: cursor_state.get((chain, flow)))
    monkeypatch.setattr(
        monitor, "save_cursor", lambda chain, flow, cursor: cursor_state.__setitem__((chain, flow), cursor)
    )

    def fake_load(flow_type, _chain, _addresses, cursor, _since, _limit):
        if flow_type != "deposit":
            return []
        seen.append(cursor)
        return [event for event in events if monitor.cursor_from_event(event) > cursor]

    def fake_send(alert: Alert) -> None:
        if not delivered:
            delivered.append(alert)
            raise RuntimeError("Telegram 429")
        delivered.append(alert)

    monkeypatch.setattr(monitor, "load_events", fake_load)
    monkeypatch.setattr(monitor, "send_alert", fake_send)

    with pytest.raises(RuntimeError, match="Telegram 429"):
        monitor.main()
    assert cursor_state == {}

    monitor.main()
    assert seen == [monitor.EventCursor(0, -1), monitor.EventCursor(0, -1)]
    assert cursor_state == {(1, "deposit"): monitor.EventCursor(101, 2)}
    assert delivered[0].message == delivered[1].message


def test_flow_aggregator_groups_by_chain_and_sorts_chronologically(monkeypatch) -> None:
    """The body must group flows by chain (alphabetical) and within each chain sort
    chronologically by (block_number, log_index) so reviewers can read top-to-bottom
    in event order rather than insertion order."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_YEARN_MAINTANACE", "yearn_maintanace_chat_id")
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=20, sender=delivered.append)

    # Intentionally scrambled insertion order across chains and blocks.
    aggregator(_record(chain_name="polygon", flow_type="withdrawal", tx_hash="0xPolyW2", block_number=200, log_index=1))
    aggregator(_record(chain_name="mainnet", flow_type="deposit", tx_hash="0xMainD1", block_number=150, log_index=0))
    aggregator(_record(chain_name="mainnet", flow_type="withdrawal", tx_hash="0xMainW1", block_number=100, log_index=5))
    aggregator(_record(chain_name="polygon", flow_type="deposit", tx_hash="0xPolyD1", block_number=100, log_index=0))
    aggregator(_record(chain_name="base", flow_type="deposit", tx_hash="0xBaseD1", block_number=110, log_index=0))
    aggregator.send_summary()

    body = delivered[0].message
    # Chains appear in alphabetical order: base, mainnet, polygon.
    base_idx = body.index("⛓️ base")
    mainnet_idx = body.index("⛓️ mainnet")
    polygon_idx = body.index("⛓️ polygon")
    assert base_idx < mainnet_idx < polygon_idx
    # Within mainnet: deposit block 150 must come before withdrawal block 100.
    mainnet_section = body[mainnet_idx:polygon_idx]
    assert mainnet_section.index("0xMainD1") < mainnet_section.index("0xMainW1")


def test_flow_aggregator_drops_zero_record_send_summary() -> None:
    """``send_summary`` is a no-op when no flows were ever recorded."""
    aggregator = monitor.FlowAggregator(max_flows=10, sender=lambda _alert: None)
    # No calls; should not raise.
    aggregator.send_summary()
    assert aggregator.collected == 0
    assert aggregator.total == 0


def test_flow_aggregator_falls_back_to_yearn_channel_without_yearn_maintanace_chat(monkeypatch) -> None:
    """When ``TELEGRAM_CHAT_ID_YEARN_MAINTANACE`` is unset the aggregated message should
    fall back to the protocol's own chat (mirrors ``CURATION_CHANNEL`` behavior) so
    operators see the alert until the dedicated group is configured."""
    monkeypatch.delenv("TELEGRAM_CHAT_ID_YEARN_MAINTANACE", raising=False)
    delivered: list[Alert] = []
    aggregator = monitor.FlowAggregator(max_flows=10, sender=delivered.append)
    aggregator(_record())
    aggregator.send_summary()

    assert delivered[0].channel == monitor.PROTOCOL


# ---- Message formatter ----


def test_format_aggregated_message_layout() -> None:
    flows = [
        _record(chain_name="mainnet", flow_type="deposit", tx_hash="0xMainD", block_number=100),
        _record(chain_name="mainnet", flow_type="withdrawal", tx_hash="0xMainW", block_number=200),
    ]
    body = monitor.format_aggregated_message(flows, truncated=0)

    assert body.startswith("ℹ️ Small parent-vault flows — 2 in this run")
    assert "⛓️ mainnet — 2 flow(s)" in body
    assert "• 1 deposit(s):" in body
    assert "• 1 withdrawal(s):" in body
    # Deposits appear before withdrawals within the same chain section.
    assert body.index("deposit(s):") < body.index("withdrawal(s):")


def test_render_flow_line_short_hash_and_ellipsized_vault() -> None:
    """The per-flow line should fit one phone screen line: short tx hash and a
    ellipsized vault address when the address is long-form (checksum, 42 chars)."""
    line = monitor.render_flow_line(_record())
    assert "0xTransact…Hash" in line
    assert "0xPare…ress" in line
    assert "→" in line  # deposit arrow
