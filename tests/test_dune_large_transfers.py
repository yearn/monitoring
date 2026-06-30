"""Tests for the Dune-backed large transfer monitor."""

from unittest.mock import Mock

from protocols.stables import dune_large_transfers as monitor


def _row(**overrides):
    row = {
        "block_time": "2026-05-12 14:30:00.000 UTC",
        "blockchain": "ethereum",
        "tx_hash": "0xABC",
        "contract_address": "0xCCCC62962d17b8914c62D74FfB843d73B2a3cCCc",
        "symbol": "cUSD",
        "amount": "5000000",
        "amount_usd": "5000000",
    }
    row.update(overrides)
    return row


def test_row_key_excludes_colon_timestamp_from_cache_value():
    key = monitor._row_key(_row(block_time="2026-05-12 14:30:00.000 UTC", log_index=None))

    assert ":" not in key
    assert key == "0xabc|0xcccc62962d17b8914c62d74ffb843d73b2a3cccc"


def test_row_key_uses_log_index_when_present():
    key = monitor._row_key(_row(log_index=17))

    assert key.endswith("|17")


def test_route_for_row_returns_protocol_for_known_token():
    assert monitor._route_for_row(_row()) == ("cUSD", "cap")


def test_route_for_row_skips_unknown_token_instead_of_stables_fallback():
    row = _row(contract_address="0x0000000000000000000000000000000000000000")

    assert monitor._route_for_row(row) is None


def test_route_for_row_skips_usdai():
    row = _row(
        blockchain="arbitrum",
        contract_address="0x0a1a1a107e45b7ced86833863f482bc5f4ed82ef",
        symbol="USDai",
    )

    assert monitor._route_for_row(row) is None


def test_is_large_transfer_requires_positive_usd_amount():
    row = _row(amount="5000000", amount_usd="0")

    assert monitor._is_large_transfer(row, 5_000_000) is False


def test_new_rows_since_last_seen_only_returns_new_prefix():
    newest = _row(tx_hash="0xnew", block_time="2026-05-12 15:00:00.000 UTC")
    previous = _row(tx_hash="0xold", block_time="2026-05-12 14:00:00.000 UTC")
    older = _row(tx_hash="0xolder", block_time="2026-05-12 13:00:00.000 UTC")

    rows = [newest, previous, older]

    assert monitor._new_rows_since_last_seen(rows, monitor._row_key(previous)) == [newest]


def test_sort_rows_newest_first_defends_dedup_order():
    newest = _row(tx_hash="0xnew", block_time="2026-05-12 15:00:00.000 UTC")
    older = _row(tx_hash="0xold", block_time="2026-05-12 14:00:00.000 UTC")

    assert monitor._sort_rows_newest_first([older, newest]) == [newest, older]


def test_build_protocol_lines_appends_truncation_notice():
    rows = [_row(tx_hash=f"0x{i}") for i in range(monitor.MAX_ROWS_PER_PROTOCOL_ALERT + 2)]

    lines = monitor._build_protocol_lines(rows, query_id=1234567)

    assert len(lines) == monitor.MAX_ROWS_PER_PROTOCOL_ALERT + 1
    assert lines[-1] == "…and 2 more transactions. See Dune query 1234567 for the full result."


def test_build_protocol_lines_shows_one_entry_per_transaction():
    rows = [
        _row(tx_hash="0xSAME", log_index=1, amount="5139554.464867114", amount_usd="5139554.464867114"),
        _row(tx_hash="0xSAME", log_index=2, amount="5139554.464867114", amount_usd="5139554.464867114"),
    ]

    lines = monitor._build_protocol_lines(rows, query_id=1234567)

    assert len(lines) == 1
    assert lines[0].startswith("*Transaction 1*")
    assert "🧾 Matched transfers in tx: 2" in lines[0]
    assert "*Transaction 2*" not in lines[0]


def test_build_alert_message_is_readable_and_formats_large_values():
    tx_hash = "0xbcd224d842f47167ec6339c47ac473ba751b73afbce36ed82142d8603c0c1bfd"
    row = _row(
        contract_address="0x48f9e38f3070ad8945dfeae3fa70987722e3d89c",
        symbol="iUSD",
        amount="5139554.464867114",
        amount_usd="5139554.464867114",
        tx_hash=tx_hash,
    )

    message = monitor._build_alert_message("infinifi", [row], query_id=7558262, total_rows=1)

    assert message == (
        "*Large iUSD transfer detected*\n\n"
        "🏦 Protocol: Infinifi\n"
        "📦 New transactions: 1\n"
        "📊 Dune query: 7558262\n\n"
        "*Transaction 1*\n"
        "🌐 Network: Ethereum\n"
        "💰 Amount: 5,139,554.46 iUSD\n"
        "💵 Value: $5,139,554.46\n"
        f"🔗 Transaction: [0xbcd224d8…3c0c1bfd](https://etherscan.io/tx/{tx_hash})"
    )


def test_build_alert_message_counts_duplicate_rows_inside_same_tx_once():
    tx_hash = "0xbcd224d842f47167ec6339c47ac473ba751b73afbce36ed82142d8603c0c1bfd"
    rows = [
        _row(
            contract_address="0x48f9e38f3070ad8945dfeae3fa70987722e3d89c",
            symbol="iUSD",
            amount="5139554.464867114",
            amount_usd="5139554.464867114",
            tx_hash=tx_hash,
            log_index=1,
        ),
        _row(
            contract_address="0x48f9e38f3070ad8945dfeae3fa70987722e3d89c",
            symbol="iUSD",
            amount="5139554.464867114",
            amount_usd="5139554.464867114",
            tx_hash=tx_hash,
            log_index=2,
        ),
    ]

    message = monitor._build_alert_message("infinifi", rows, query_id=7558262, total_rows=2)

    assert "📦 New transactions: 1 (2 matched transfers)" in message
    assert message.count("*Transaction ") == 1
    assert "🧾 Matched transfers in tx: 2" in message


def test_main_sends_pretty_alert_with_markdown_enabled(monkeypatch):
    row = _row()
    result = Mock()
    result.result.rows = [row]
    dune = Mock()
    dune.run_query.return_value = result

    monkeypatch.setenv("DUNE_API_KEY", "test-key")
    monkeypatch.setattr(monitor, "DuneClient", Mock(return_value=dune))
    monkeypatch.setattr(monitor.Config, "get_env_int", Mock(return_value=7558262))
    monkeypatch.setattr(monitor.Config, "get_env_float", Mock(return_value=5_000_000))
    monkeypatch.setattr(monitor, "get_last_value_for_key_from_file", Mock(return_value=""))
    send_alert = Mock()
    monkeypatch.setattr(monitor, "send_alert", send_alert)
    monkeypatch.setattr(monitor, "write_last_value_to_file", Mock())

    monitor.main()

    send_alert.assert_called_once()
    alert = send_alert.call_args.args[0]
    assert alert.message.startswith("*Large cUSD transfer detected*")
    assert send_alert.call_args.kwargs == {}
