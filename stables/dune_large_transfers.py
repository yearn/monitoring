#!/usr/bin/env python3
"""Hourly large-transfer monitor backed by a Dune query result.

Expected Dune query output columns:
- block_time
- blockchain
- tx_hash
- from
- to
- contract_address
- symbol
- amount
- amount_usd
"""

from __future__ import annotations

import os
from typing import Any

from dune_client.client import DuneClient
from dune_client.query import QueryBase

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.config import Config
from utils.logging import get_logger

LOGGER = get_logger("stables.dune_large_transfers")
PROTOCOL = "stables"

CACHE_KEY_LAST_TX = "stables_dune_large_transfers_last_tx"
MAX_ROWS_IN_ALERT = 10
DEFAULT_LARGE_TRANSFER_THRESHOLD = 5_000_000.0

# Route each token to its owning protocol channel.
TOKEN_ROUTE: dict[tuple[str, str], tuple[str, str]] = {
    ("ethereum", "0xcccc62962d17b8914c62d74ffb843d73b2a3cccc"): ("cUSD", "cap"),
    ("ethereum", "0x48f9e38f3070ad8945dfeae3fa70987722e3d89c"): ("iUSD", "infinifi"),
    ("arbitrum", "0x0a1a1a107e45b7ced86833863f482bc5f4ed82ef"): ("USDai", "usdai"),
}

CHAIN_TX_EXPLORER: dict[str, str] = {
    "ethereum": "https://etherscan.io/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "optimism": "https://optimistic.etherscan.io/tx/",
    "base": "https://basescan.org/tx/",
    "polygon": "https://polygonscan.com/tx/",
}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _tx_link(blockchain: str, tx_hash: str) -> str:
    prefix = CHAIN_TX_EXPLORER.get(blockchain.lower())
    if not prefix:
        return tx_hash
    return f"{prefix}{tx_hash}"


def _row_key(row: dict[str, Any]) -> str:
    tx_hash = _as_str(row.get("tx_hash")).lower()
    block_time = _as_str(row.get("block_time"))
    contract = _as_str(row.get("contract_address")).lower()
    return f"{tx_hash}|{block_time}|{contract}"


def _route_for_row(row: dict[str, Any]) -> tuple[str, str]:
    chain = _as_str(row.get("blockchain")).lower()
    addr = _as_str(row.get("contract_address")).lower()
    if (chain, addr) in TOKEN_ROUTE:
        return TOKEN_ROUTE[(chain, addr)]

    symbol = _as_str(row.get("symbol")) or "unknown"
    return (symbol, PROTOCOL)


def _build_row_line(row: dict[str, Any]) -> str:
    chain = _as_str(row.get("blockchain"))
    symbol = _as_str(row.get("symbol")) or "unknown"
    amount = row.get("amount")
    amount_usd = row.get("amount_usd")
    tx_hash = _as_str(row.get("tx_hash"))
    link = _tx_link(chain, tx_hash)
    return f"- {symbol} on {chain}: amount={amount}, amount_usd={amount_usd}, tx={link}"


def _group_rows_by_protocol(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        _, protocol = _route_for_row(row)
        grouped.setdefault(protocol, []).append(row)
    return grouped


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_large_transfer(row: dict[str, Any], threshold: float) -> bool:
    amount_usd = _to_float(row.get("amount_usd"))
    amount = _to_float(row.get("amount"))
    metric = amount_usd if amount_usd > 0 else amount
    return metric >= threshold


def main() -> None:
    api_key = os.getenv("DUNE_API_KEY")
    query_id = Config.get_env_int("DUNE_LARGE_TRANSFERS_QUERY_ID", 0)
    threshold = Config.get_env_float("DUNE_LARGE_TRANSFER_THRESHOLD", DEFAULT_LARGE_TRANSFER_THRESHOLD)

    if not api_key:
        LOGGER.warning("DUNE_API_KEY is not set; skipping Dune large transfer monitor")
        return
    if query_id <= 0:
        LOGGER.warning("DUNE_LARGE_TRANSFERS_QUERY_ID is not set; skipping Dune large transfer monitor")
        return

    try:
        dune = DuneClient(api_key)
        result = dune.run_query(QueryBase(query_id=query_id, name="stables_large_transfers"), ping_frequency=2)
        rows = list(result.result.rows) if result and result.result and result.result.rows else []
    except Exception as exc:
        LOGGER.error("Failed to fetch Dune large-transfer query result: %s", exc)
        send_alert(
            Alert(
                AlertSeverity.MEDIUM,
                f"Dune large-transfer monitor failed while querying Dune: {exc}",
                PROTOCOL,
            ),
            plain_text=True,
        )
        return

    if not rows:
        LOGGER.info("No large transfers returned by Dune query_id=%s", query_id)
        return

    alert_rows = [row for row in rows if _is_large_transfer(row, threshold)]
    if not alert_rows:
        LOGGER.info("No rows matched large-transfer threshold >= %s", threshold)
        return

    newest_key = _row_key(alert_rows[0])
    last_key = _as_str(get_last_value_for_key_from_file(cache_filename, CACHE_KEY_LAST_TX))
    if newest_key == last_key:
        LOGGER.info("No new large-transfer rows since last run (top row unchanged)")
        return

    grouped = _group_rows_by_protocol(alert_rows[:MAX_ROWS_IN_ALERT])
    total_rows = len(alert_rows)
    for protocol, protocol_rows in grouped.items():
        first_symbol, _ = _route_for_row(protocol_rows[0])
        lines = [_build_row_line(row) for row in protocol_rows]
        message = (
            f"*Dune Large Transfer Alert ({first_symbol}/{protocol})*\n\n"
            f"Query ID: {query_id}\n"
            f"Matched rows: {total_rows}\n"
            f"Included in this alert: {len(protocol_rows)}\n\n" + "\n".join(lines)
        )
        send_alert(Alert(AlertSeverity.HIGH, message, protocol), plain_text=True)

    write_last_value_to_file(cache_filename, CACHE_KEY_LAST_TX, newest_key)


if __name__ == "__main__":
    main()
