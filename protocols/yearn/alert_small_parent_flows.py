#!/usr/bin/env python3
"""Alert on small deposits and withdrawals from Yearn v3 parent vaults."""

from __future__ import annotations

import argparse
import json
import logging
import os
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
from utils.telegram import MAX_MESSAGE_LENGTH, YEARN_MAINTENANCE_CHANNEL, resolve_channel, send_envio_error_message

load_dotenv()

getcontext().prec = 60

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")
DEFAULT_LOG_LEVEL = os.getenv("SMALL_PARENT_FLOWS_LOG_LEVEL") or os.getenv("LOG_LEVEL", "INFO")
DEFAULT_THRESHOLD_RAW = 10_000
DEFAULT_LOOKBACK_SECONDS = 7200
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MAX_FLOWS = 500
# Reserve room for send_alert's prefix and Telegram's UTF-16 emoji accounting.
MAX_AGGREGATE_LENGTH = MAX_MESSAGE_LENGTH - 32
PROTOCOL = "yearn"
# Stored alert-history key for flow and Envio error alerts. Kept off the public
# Yearn monitoring page, which queries ``yearn``; Telegram routing and the
# ``[yearn]`` label still use ``PROTOCOL``.
ALERT_PROTOCOL = "yearn-internal"
STATE_NAMESPACE = "yearn.small_parent_flows"
FLOW_TYPES = ("deposit", "withdrawal")
FLOW_ENTITY = {"deposit": "Deposit", "withdrawal": "Withdraw"}
# Chains indexed by yearn-envio (https://github.com/yearn/yearn-envio/blob/main/config.yaml).
# Optimism is a `Chain` member but is not indexed, so its flows would silently never arrive.
# Polygon is indexed but has no active parent vaults, so it only produced a warning every run.
ENVIO_CHAINS: tuple[Chain, ...] = (Chain.MAINNET, Chain.BASE, Chain.ARBITRUM, Chain.KATANA)

logger = get_logger("yearn.alert_small_parent_flows")


class EnvioUnavailableError(RuntimeError):
    """Raised after an Envio failure has been reported, so the run stops without re-alerting."""


class FlowAggregator:
    """Collect qualifying flows during a run and emit one aggregated Telegram message.

    Replaces the previous ``AlertLimiter`` design, which sent up to N individual alerts
    and then a single overflow summary. That produced two failure modes:

    - **Telegram rate-limit hits.** A burst of 20 LOW-severity messages from one cron
      tick (each ~1s apart) is enough to trip Telegram's per-chat-per-second limit on
      the destination group; the *21st* message then returns 429 with ``retry_after=17``
      and the run's overflow summary is lost. (See production incident on
      2026-09-17 02:05:57 UTC — alert #991 was created but ``delivery_status=failed``
      with the same 429 in ``delivery_error``.)
    - **Audit friction.** Reviewers had to scroll past N near-identical messages to
      reconstruct the picture; a single grouped message is easier to grep, diff across
      runs, and link from a gist.

    Flows are grouped by chain (network name, alphabetical), then by direction
    (deposits before withdrawals within each chain), sorted chronologically by
    ``(block_number, log_index)`` so the run reads top-to-bottom in event order.

    The aggregate is limited by ``max_flows`` and Telegram's message length. Flows
    beyond either limit are counted as truncated in the message and run log.
    """

    def __init__(
        self,
        max_flows: int,
        sender: Callable[[Alert], None] | None = None,
    ) -> None:
        self.max_flows = max_flows
        self.sender = sender
        self._flows: list[SmallFlowRecord] = []
        self.truncated = 0

    def __call__(self, record: SmallFlowRecord) -> None:
        """Record one qualifying flow from ``process_event``."""
        if len(self._flows) < self.max_flows:
            self._flows.append(record)
            return
        self.truncated += 1

    def send_summary(self) -> None:
        """Send one aggregated Telegram message within Telegram's length limit.

        No-op when no flows were recorded — avoids spamming the channel with a
        "0 flows" header on quiet runs.
        """
        if not self._flows:
            return
        shown = len(self._flows)
        truncated = self.truncated
        message = format_aggregated_message(self._flows, truncated)
        if len(message) > MAX_AGGREGATE_LENGTH:
            low, high = 0, shown
            while low < high:
                middle = (low + high + 1) // 2
                candidate = format_aggregated_message(self._flows[:middle], truncated + shown - middle)
                if len(candidate) <= MAX_AGGREGATE_LENGTH:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise RuntimeError("First small parent flow exceeds the Telegram message limit")
            message = format_aggregated_message(self._flows[:low], truncated + shown - low)
            truncated += shown - low
            shown = low
        (self.sender or send_alert)(
            Alert(
                AlertSeverity.LOW,
                message,
                ALERT_PROTOCOL,
                channel=resolve_channel(YEARN_MAINTENANCE_CHANNEL, PROTOCOL),
            )
        )
        self.truncated = truncated
        self._flows = self._flows[:shown]

    @property
    def collected(self) -> int:
        """Number of flows actually rendered into the aggregated message body."""
        return len(self._flows)

    @property
    def total(self) -> int:
        """Total qualifying flows seen this run, including any beyond the cap."""
        return len(self._flows) + self.truncated


@dataclass(frozen=True)
class SmallFlowRecord:
    """One qualifying flow, normalized for aggregation.

    Carries the minimum fields needed to render one line in the aggregated message.
    ``process_event`` builds one of these and hands it to the aggregator; the
    aggregator does *not* keep the per-flow ``Alert.message`` text — the final body is
    rebuilt from structured data at ``send_summary()`` time so the formatter can group
    and sort freely.
    """

    chain_name: str
    flow_type: str
    amount: str
    asset_symbol: str
    vault_symbol: str
    vault_address: str
    explorer: str | None
    tx_hash: str
    block_number: int
    log_index: int


def format_aggregated_message(flows: list[SmallFlowRecord], truncated: int) -> str:
    """Render one Telegram message body covering all ``flows`` in this run.

    Layout::

        ℹ️ Small parent-vault flows — N in this run

        ⛓️ Base — 8 flow(s)
          • 5 deposit(s):
            0.0015 USDC → yvUSDC (0xvault…abcd) — Tx [0x1234…5678]
            …
          • 3 withdrawal(s):
            …

        ⛓️ Ethereum — 4 flow(s)
          …

    Each flow line is a single short bullet; full owner/sender/receiver detail from
    ``build_alert_message`` is left to the per-flow explorer link in any follow-up
    gist, not repeated inline.
    """
    by_chain: dict[str, list[SmallFlowRecord]] = {}
    for flow in flows:
        by_chain.setdefault(flow.chain_name, []).append(flow)

    total = len(flows) + truncated
    header = f"ℹ️ Small parent-vault flows — {total} in this run"
    if truncated:
        header += f" ({truncated} truncated; {len(flows)} shown)"

    sections: list[str] = []
    for chain_name in sorted(by_chain):
        chain_flows = by_chain[chain_name]
        deposits = sorted(
            (f for f in chain_flows if f.flow_type == "deposit"),
            key=lambda f: (f.block_number, f.log_index),
        )
        withdrawals = sorted(
            (f for f in chain_flows if f.flow_type == "withdrawal"),
            key=lambda f: (f.block_number, f.log_index),
        )
        bits: list[str] = [f"⛓️ {chain_name} — {len(chain_flows)} flow(s)"]
        if deposits:
            bits.append(f"  • {len(deposits)} deposit(s):")
            bits.extend(f"    {render_flow_line(f)}" for f in deposits)
        if withdrawals:
            bits.append(f"  • {len(withdrawals)} withdrawal(s):")
            bits.extend(f"    {render_flow_line(f)}" for f in withdrawals)
        sections.append("\n".join(bits))

    return header + "\n\n" + "\n\n".join(sections)


def render_flow_line(flow: SmallFlowRecord) -> str:
    """Render one flow as a single Telegram-safe bullet line.

    Format: ``<amount> <symbol> <arrow> <vault_label> — Tx [<short_hash>]``.
    The vault address is shortened when it's the long-form checksum address; the
    explorer link (when present) is rendered as a Markdown link to the full tx.
    """
    arrow = "→" if flow.flow_type == "deposit" else "←"
    if flow.vault_address and len(flow.vault_address) > 14:
        vault = f"{flow.vault_symbol} ({flow.vault_address[:6]}…{flow.vault_address[-4:]})"
    else:
        vault = flow.vault_symbol or flow.vault_address or "vault"
    asset = f"{flow.amount} {flow.asset_symbol}".strip()
    tx_short = f"{flow.tx_hash[:10]}…{flow.tx_hash[-4:]}" if len(flow.tx_hash) > 16 else flow.tx_hash
    if flow.explorer:
        tx_part = f"[{tx_short}]({flow.explorer}/tx/{flow.tx_hash})"
    else:
        tx_part = tx_short
    return f"{asset} {arrow} {vault} — Tx {tx_part}"


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


def gql_request(query: str, variables: dict) -> dict:
    """Execute an Envio GraphQL query, routing failures to its ops channel.

    Raises:
        EnvioUnavailableError: After reporting a request or GraphQL failure once.
    """
    if not ENVIO_GRAPHQL_URL:
        raise RuntimeError("ENVIO_GRAPHQL_URL is not set")

    try:
        payload = http_json(ENVIO_GRAPHQL_URL, {"query": query, "variables": variables})
    except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
        send_envio_error_message(
            f"Small parent flow monitor: Envio GraphQL request failed ({exc}). Skipping this run.",
            PROTOCOL,
            source="small_parent_flows",
            alert_protocol=ALERT_PROTOCOL,
        )
        logger.error("Envio request failed: %s", exc)
        raise EnvioUnavailableError(f"Envio request failed: {exc}") from exc

    if payload.get("errors"):
        send_envio_error_message(
            f"Small parent flow monitor: Envio GraphQL errors: {payload['errors']}",
            PROTOCOL,
            source="small_parent_flows",
            alert_protocol=ALERT_PROTOCOL,
        )
        logger.error("Envio GraphQL errors: %s", payload["errors"])
        raise EnvioUnavailableError(f"Envio GraphQL errors: {payload['errors']}")
    return payload


def load_events(
    flow_type: str,
    chain_id: int,
    vault_addresses: list[str],
    cursor: EventCursor,
    since_ts: int,
    limit: int,
) -> list[dict]:
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
    events = (response.get("data") or {}).get("events")
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


def load_or_init_start_ts(chain_id: int, flow_type: str, default_ts: int) -> int:
    """Return the persisted first-run lookback floor, storing ``default_ts`` if none exists.

    Streams that have never seen an event have no block cursor. Persisting the
    first run's lookback floor keeps later runs from sliding the window forward,
    so a run gap longer than the lookback cannot silently drop events.
    """
    key = f"{state_key(chain_id, flow_type)}:start_ts"
    raw = store.state_get(STATE_NAMESPACE, key)
    if raw is None:
        store.state_set(STATE_NAMESPACE, key, str(default_ts))
        return default_ts
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid small-flow start timestamp for {key}: {raw}") from exc


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
    alert_sender: Callable[[SmallFlowRecord], None] | None = None,
) -> bool:
    """Evaluate one flow, handing a structured record to the aggregator when below threshold.

    The previous design built a full ``Alert`` here and routed it to ``send_alert``
    immediately — which made per-flow messages and forced an AlertLimiter on top to
    cap the volume. Now we hand a ``SmallFlowRecord`` (the minimum data needed for
    the aggregated message) to the caller-supplied ``alert_sender``, which in
    production is a ``FlowAggregator`` that emits one grouped Telegram message at
    ``send_summary()`` time. Tests can pass a list-collector to inspect the
    per-flow records without going through the aggregator.

    ``alert_sender`` is optional but only because events at-or-above the threshold
    short-circuit before the call site; if a small flow is found with no sender
    configured that's a programmer error and we raise rather than silently dropping.
    """
    vault_address = str(event["vaultAddress"]).lower()
    vault = vaults_by_address.get(vault_address)
    if vault is None:
        raise RuntimeError(f"Envio returned unknown parent vault {event['vaultAddress']}")

    raw_assets = int(str(event["assets"]))
    if not is_small_flow(raw_assets, threshold_raw):
        return False

    amount = format_units(raw_assets, int(vault["asset_decimals"]))
    chain_id = int(event["chainId"])
    chain = Chain.from_chain_id(chain_id)
    record = SmallFlowRecord(
        chain_name=chain.network_name,
        flow_type=str(event["flow_type"]),
        amount=format_amount(amount),
        asset_symbol=str(vault["asset_symbol"]),
        vault_symbol=str(vault["symbol"]),
        vault_address=str(event["vaultAddress"]),
        explorer=EXPLORER_URLS.get(chain_id),
        tx_hash=str(event["transactionHash"]),
        block_number=int(event["blockNumber"]),
        log_index=int(event["logIndex"]),
    )
    if alert_sender is None:
        raise RuntimeError("process_event called on a small flow with no alert_sender configured")
    alert_sender(record)
    return True


def monitor_flow_type(
    chain_id: int,
    flow_type: str,
    addresses: list[str],
    vaults_by_address: dict[str, dict],
    threshold_raw: int,
    lookback_seconds: int,
    page_size: int,
    pending_cursors: dict[tuple[int, str], EventCursor],
    now: int | None = None,
    alert_sender: Callable[[SmallFlowRecord], None] | None = None,
) -> tuple[int, int]:
    """Fetch events and stage the last processed cursor for delivery confirmation."""
    persisted_cursor = load_cursor(chain_id, flow_type)
    if persisted_cursor is not None:
        cursor = persisted_cursor
        since_ts = 0
    else:
        cursor = EventCursor(0, -1)
        since_ts = load_or_init_start_ts(chain_id, flow_type, (now or int(time.time())) - lookback_seconds)
    processed = 0
    alerted = 0

    while True:
        events = load_events(flow_type, chain_id, addresses, cursor, since_ts, page_size)
        if not events:
            break

        for event in events:
            event_cursor = cursor_from_event(event)
            if event_cursor <= cursor:
                continue
            if process_event(event, vaults_by_address, threshold_raw, alert_sender):
                alerted += 1
            pending_cursors[(chain_id, flow_type)] = event_cursor
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
    pending_cursors: dict[tuple[int, str], EventCursor],
    now: int | None = None,
    alert_sender: Callable[[SmallFlowRecord], None] | None = None,
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
            pending_cursors,
            now,
            alert_sender,
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
    default_chain_ids = ",".join(str(chain.chain_id) for chain in ENVIO_CHAINS)
    parser = argparse.ArgumentParser(
        description="Alert on Yearn v3 parent-vault deposits and withdrawals below a raw-assets threshold."
    )
    parser.add_argument("--threshold-raw", type=int, default=DEFAULT_THRESHOLD_RAW)
    parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--chain-ids", default=default_chain_ids)
    parser.add_argument(
        "--max-flows",
        type=int,
        default=DEFAULT_MAX_FLOWS,
        help=(
            "Maximum flows rendered in the aggregate Telegram message "
            f"(default: {DEFAULT_MAX_FLOWS}). Flows beyond this or Telegram's length "
            "limit are counted in the message and run log."
        ),
    )
    parser.add_argument("--log-level", default=DEFAULT_LOG_LEVEL)
    args = parser.parse_args()

    # get_logger() installs its own handler and disables propagation, so the
    # root-logger basicConfig would not affect this module's output.
    log_level = logging.getLevelNamesMapping().get(args.log_level.upper())
    if log_level is None:
        parser.error(f"--log-level must be a logging level name, got {args.log_level!r}")
    logger.setLevel(log_level)
    if args.threshold_raw <= 0:
        parser.error("--threshold-raw must be positive")
    if args.lookback_seconds < 0:
        parser.error("--lookback-seconds must be non-negative")
    if args.page_size <= 0:
        parser.error("--page-size must be positive")
    if args.max_flows <= 0:
        parser.error("--max-flows must be positive")

    aggregator = FlowAggregator(args.max_flows)
    pending_cursors: dict[tuple[int, str], EventCursor] = {}
    total_processed = 0
    total_alerted = 0
    try:
        for chain in parse_chain_ids(args.chain_ids):
            processed, alerted = monitor_chain(
                chain,
                args.threshold_raw,
                args.lookback_seconds,
                args.page_size,
                pending_cursors,
                alert_sender=aggregator,
            )
            total_processed += processed
            total_alerted += alerted
            logger.info("%s: processed=%d alerted=%d", chain.network_name, processed, alerted)
    except EnvioUnavailableError as exc:
        # Already reported to the Envio channel once; stop instead of repeating it per chain/flow.
        logger.error("Aborting run after Envio failure: %s", exc)
    finally:
        aggregator.send_summary()
        for (chain_id, flow_type), cursor in pending_cursors.items():
            save_cursor(chain_id, flow_type, cursor)
        if aggregator.truncated:
            logger.warning(
                "Aggregate message includes %d flow(s); %d additional flow(s) truncated",
                aggregator.collected,
                aggregator.truncated,
            )
    logger.info(
        "complete: processed=%d alerted=%d collected=%d truncated=%d",
        total_processed,
        total_alerted,
        aggregator.collected,
        aggregator.truncated,
    )


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
