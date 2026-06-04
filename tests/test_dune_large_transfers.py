"""Tests for the Dune-backed large transfer monitor."""

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
    assert lines[-1] == "- +2 more not shown -- see Dune query 1234567 directly"
