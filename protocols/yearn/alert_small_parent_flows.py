#!/usr/bin/env python3
"""Alert on small deposits and withdrawals from Yearn v3 parent vaults."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Callable

from dotenv import load_dotenv

from protocols.yearn.kong import fetch_kong_parent_vaults
from utils import store
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import EXPLORER_URLS, Chain
from utils.logger import get_logger
from utils.telegram import send_envio_error_message

load_dotenv()

getcontext().prec = 60

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")
DEFAULT_LOG_LEVEL = os.getenv("SMALL_PARENT_FLOWS_LOG_LEVEL", "INFO")
DEFAULT_THRESHOLD_RAW = 10_000
DEFAULT_LOOKBACK_SECONDS = 7200
DEFAULT_PAGE_SIZE = 1000
PROTOCOL = "yearn"
STATE_NAMESPACE = "yearn.small_parent_flows"
FLOW_TYPES = ("deposit", "withdrawal")
FLOW_ENTITY = {"deposit": "Deposit", "withdrawal": "Withdraw"}

logger = get_logger("yearn.alert_small_parent_flows")


@dataclass(frozen=True, order=True)
class EventCursor:
    """Per-chain Envio event cursor."""

    block_number: int
    log_index: int


def http_json(url: str, body: dict) -> dict:
    """POST a JSON body and return the decoded response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload: object = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Envio returned a non-object JSON response")
    return payload


def gql_request(query: str, variables: dict) -> dict | None:
    """Execute an Envio GraphQL query, routing failures to its ops channel."""
    if not ENVIO_GRAPHQL_URL:
        raise RuntimeError("ENVIO_GRAPHQL_URL is not set")

    try:
        payload = http_json(ENVIO_GRAPHQL_URL, {"query": query, "variables": variables})
    except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
        send_envio_error_message(
            f"Small parent flow monitor: Envio GraphQL request failed ({exc}). Skipping this run.",
            PROTOCOL,
            source="small_parent_flows",
        )
        logger.error("Envio request failed: %s", exc)
        return None

    if payload.get("errors"):
        send_envio_error_message(
            f"Small parent flow monitor: Envio GraphQL errors: {payload['errors']}",
            PROTOCOL,
            source="small_parent_flows",
        )
        logger.error("Envio GraphQL errors: %s", payload["errors"])
        return None
    return payload


def load_events(
    flow_type: str,
    chain_id: int,
    vault_addresses: list[str],
    cursor: EventCursor,
    since_ts: int,
    limit: int,
) -> list[dict] | None:
    """Load one ordered page of parent-vault flow events after ``cursor``."""
    try:
        entity = FLOW_ENTITY[flow_type]
    except KeyError as exc:
        raise ValueError(f"Unknown flow type: {flow_type}") from exc

    receiver_field = "receiver" if flow_type == "withdrawal" else ""
    query = """
    query SmallParentFlows(
      $chainId: Int!
      $addresses: [String!]!
      $lastBlock: Int!
      $lastLogIndex: Int!
      $sinceTs: Int!
      $limit: Int!
    ) {
      events: __ENTITY__(
        where: {
          chainId: { _eq: $chainId }
          vaultAddress: { _in: $addresses }
          _or: [
            { blockNumber: { _gt: $lastBlock }, blockTimestamp: { _gte: $sinceTs } }
            { blockNumber: { _eq: $lastBlock }, logIndex: { _gt: $lastLogIndex } }
          ]
        }
        order_by: { blockNumber: asc, logIndex: asc }
        limit: $limit
      ) {
        id
        vaultAddress
        chainId
        blockNumber
        blockTimestamp
        transactionHash
        transactionFrom
        logIndex
        sender
        owner
        __RECEIVER_FIELD__
        assets
        shares
      }
    }
    """
    query = query.replace("__ENTITY__", entity).replace("__RECEIVER_FIELD__", receiver_field)
    variables = {
        "chainId": chain_id,
        "addresses": vault_addresses,
        "lastBlock": cursor.block_number,
        "lastLogIndex": cursor.log_index,
        "sinceTs": since_ts,
        "limit": limit,
    }
    response = gql_request(query, variables)
    if response is None:
        return None
    events = response.get("data", {}).get("events")
    if not isinstance(events, list):
        raise RuntimeError(f"Envio response missing {entity} list")
    return [{**event, "flow_type": flow_type} for event in events]


def format_units(raw_assets: str | int, decimals: int) -> Decimal:
    """Convert an integer asset amount into normalized token units."""
    return Decimal(str(raw_assets)) / (Decimal(10) ** decimals)


def is_small_flow(raw_assets: str | int, threshold_raw: int) -> bool:
    """Return whether a positive raw asset amount is below the threshold."""
    amount_raw = int(str(raw_assets))
    return 0 < amount_raw < threshold_raw


def format_amount(amount: Decimal) -> str:
    """Format a token amount without scientific notation or trailing zeroes."""
    rendered = f"{amount:,.18f}".rstrip("0").rstrip(".")
    return rendered or "0"


def address_link(address: str, explorer: str | None) -> str:
    """Return a full address, linked to the chain explorer when available."""
    if explorer:
        return f"[{address}]({explorer}/address/{address})"
    return address


def build_alert_message(
    event: dict,
    vault: dict,
    raw_assets: int,
    amount: Decimal,
    threshold_raw: int,
) -> str:
    """Build the Telegram message for one qualifying flow."""
    chain_id = int(event["chainId"])
    chain = Chain.from_chain_id(chain_id)
    explorer = EXPLORER_URLS.get(chain_id)
    vault_address = str(event["vaultAddress"])
    tx_hash = str(event["transactionHash"])
    tx = f"[{tx_hash}]({explorer}/tx/{tx_hash})" if explorer else tx_hash
    flow_type = str(event["flow_type"])

    lines = [
        f"Small parent-vault {flow_type}",
        f"🏦 Vault: {address_link(vault_address, explorer)} ({vault['symbol']})",
        f"🔢 Raw Assets: {raw_assets:,}",
        f"🪙 Normalized: {format_amount(amount)} {vault['asset_symbol']}",
        f"📏 Raw Threshold: < {threshold_raw:,}",
        f"⛓️ Chain: {chain.network_name}",
        f"👤 Owner: {address_link(str(event['owner']), explorer)}",
        f"💳 Sender: {address_link(str(event['sender']), explorer)}",
    ]
    receiver = event.get("receiver")
    if receiver:
        lines.append(f"📥 Receiver: {address_link(str(receiver), explorer)}")
    transaction_from = event.get("transactionFrom")
    if transaction_from:
        lines.append(f"🚀 Tx From: {address_link(str(transaction_from), explorer)}")
    lines.append(f"🔗 Tx: {tx}")
    return "\n".join(lines)


def cursor_from_event(event: dict) -> EventCursor:
    """Return the sortable cursor represented by an Envio event."""
    return EventCursor(int(event["blockNumber"]), int(event["logIndex"]))


def state_key(chain_id: int, flow_type: str) -> str:
    """Return the persistent-state key for one chain and flow type."""
    if flow_type not in FLOW_ENTITY:
        raise ValueError(f"Unknown flow type: {flow_type}")
    return f"{chain_id}:{flow_type}"


def load_cursor(chain_id: int, flow_type: str) -> EventCursor | None:
    """Load a chain/flow cursor from persistent monitor state."""
    key = state_key(chain_id, flow_type)
    raw = store.state_get(STATE_NAMESPACE, key)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        return EventCursor(int(payload["block_number"]), int(payload["log_index"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid small-flow cursor for {key}: {raw}") from exc


def save_cursor(chain_id: int, flow_type: str, cursor: EventCursor) -> None:
    """Persist a successfully processed chain/flow cursor."""
    store.state_set(
        STATE_NAMESPACE,
        state_key(chain_id, flow_type),
        json.dumps({"block_number": cursor.block_number, "log_index": cursor.log_index}),
    )


def process_event(
    event: dict,
    vaults_by_address: dict[str, dict],
    threshold_raw: int,
    alert_sender: Callable[[Alert], None] = send_alert,
) -> bool:
    """Evaluate one flow, sending an alert when it is below the threshold."""
    vault_address = str(event["vaultAddress"]).lower()
    vault = vaults_by_address.get(vault_address)
    if vault is None:
        raise RuntimeError(f"Envio returned unknown parent vault {event['vaultAddress']}")

    raw_assets = int(str(event["assets"]))
    if not is_small_flow(raw_assets, threshold_raw):
        return False

    amount = format_units(raw_assets, int(vault["asset_decimals"]))
    message = build_alert_message(event, vault, raw_assets, amount, threshold_raw)
    alert_sender(Alert(AlertSeverity.LOW, message, PROTOCOL))
    return True


def monitor_flow_type(
    chain_id: int,
    flow_type: str,
    addresses: list[str],
    vaults_by_address: dict[str, dict],
    threshold_raw: int,
    lookback_seconds: int,
    page_size: int,
    now: int | None = None,
) -> tuple[int, int]:
    """Fetch and process all new events of one type for a chain."""
    persisted_cursor = load_cursor(chain_id, flow_type)
    cursor = persisted_cursor or EventCursor(0, -1)
    since_ts = 0 if persisted_cursor else (now or int(time.time())) - lookback_seconds
    processed = 0
    alerted = 0

    while True:
        events = load_events(flow_type, chain_id, addresses, cursor, since_ts, page_size)
        if events is None:
            break
        if not events:
            break

        for event in events:
            event_cursor = cursor_from_event(event)
            if event_cursor <= cursor:
                continue
            if process_event(event, vaults_by_address, threshold_raw):
                alerted += 1
            save_cursor(chain_id, flow_type, event_cursor)
            cursor = event_cursor
            processed += 1

        if len(events) < page_size:
            break

    return processed, alerted


def monitor_chain(
    chain: Chain,
    threshold_raw: int,
    lookback_seconds: int,
    page_size: int,
    now: int | None = None,
) -> tuple[int, int]:
    """Fetch and process deposits and withdrawals for one chain."""
    vaults = fetch_kong_parent_vaults(chain)
    if not vaults:
        logger.warning("No active parent vaults returned for %s", chain.network_name)
        return 0, 0

    vaults_by_address = {str(vault["address"]).lower(): vault for vault in vaults}
    # Envio stores checksummed addresses, while older rows or deployments may
    # use lowercase. Supplying both forms keeps the case-sensitive filter safe.
    addresses = sorted(
        {address for vault in vaults for address in (str(vault["address"]), str(vault["address"]).lower())}
    )

    processed = 0
    alerted = 0
    for flow_type in FLOW_TYPES:
        flow_processed, flow_alerted = monitor_flow_type(
            chain.chain_id,
            flow_type,
            addresses,
            vaults_by_address,
            threshold_raw,
            lookback_seconds,
            page_size,
            now,
        )
        processed += flow_processed
        alerted += flow_alerted
        logger.info(
            "%s %s: processed=%d alerted=%d",
            chain.network_name,
            flow_type,
            flow_processed,
            flow_alerted,
        )
    return processed, alerted


def parse_chain_ids(raw: str) -> list[Chain]:
    """Parse a comma-separated chain-ID list."""
    chains: list[Chain] = []
    for value in raw.split(","):
        if value.strip():
            chains.append(Chain.from_chain_id(int(value.strip())))
    return chains


def main() -> None:
    """Run the small parent-vault flow monitor."""
    default_chain_ids = ",".join(str(chain.chain_id) for chain in Chain)
    parser = argparse.ArgumentParser(
        description="Alert on Yearn v3 parent-vault deposits and withdrawals below a raw-assets threshold."
    )
    parser.add_argument("--threshold-raw", type=int, default=DEFAULT_THRESHOLD_RAW)
    parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--chain-ids", default=default_chain_ids)
    parser.add_argument("--log-level", default=DEFAULT_LOG_LEVEL)
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="[%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.threshold_raw <= 0:
        parser.error("--threshold-raw must be positive")
    if args.lookback_seconds < 0:
        parser.error("--lookback-seconds must be non-negative")
    if args.page_size <= 0:
        parser.error("--page-size must be positive")

    total_processed = 0
    total_alerted = 0
    for chain in parse_chain_ids(args.chain_ids):
        processed, alerted = monitor_chain(
            chain,
            args.threshold_raw,
            args.lookback_seconds,
            args.page_size,
        )
        total_processed += processed
        total_alerted += alerted
        logger.info("%s: processed=%d alerted=%d", chain.network_name, processed, alerted)
    logger.info("complete: processed=%d alerted=%d", total_processed, total_alerted)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
