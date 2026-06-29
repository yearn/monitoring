#!/usr/bin/env python3
"""Layer 3 peg monitoring — Chainlink ``AnswerUpdated`` event consumer (hourly).

Reads the Chainlink ``AnswerUpdated(int256 current, uint256 roundId, uint256 updatedAt)``
rows captured by the Envio indexer (``chain-events/yearn-indexing-test`` issue #31)
and turns per-feed anomalies into alerts. Where L2 (``oracles.py``) polls the
*current* round each hour, this consumes the *full event stream* so no round is
missed between polls. Mirrors the Envio→Telegram pattern of
``protocols/timelock/timelock_alerts.py``.

Detects, per feed:

* **large round-over-round jumps** — ``|Δanswer| / prev ≥ JUMP_THRESHOLD``;
* **missed-heartbeat gaps** — ``updatedAt`` gap between consecutive rounds
  exceeds the feed heartbeat + buffer;
* **sequence anomalies** — a ``roundId`` that does not strictly increase.

Routing uses the shared :data:`PEGGED_ASSETS` registry (``protocol`` + ``channel``)
so alerts reach the owning protocol and its emergency dispatch. De-duped across
runs via a per-aggregator ``blockTimestamp`` cursor in the cache.

Address sourcing (per indexer #31): ``AnswerUpdated`` is emitted by the *underlying
aggregator*, whose address we resolve at runtime from each feed proxy's
``aggregator()`` — this also tracks phase rotations (a rotation shows up as a
staleness gap in L2).
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from dotenv import load_dotenv
from eth_utils import to_checksum_address

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.config import Config
from utils.logger import get_logger
from utils.pegged_assets import PEGGED_ASSETS, ChainlinkFeed, PeggedAsset
from utils.telegram import send_error_message
from utils.web3_wrapper import ChainManager, Web3Client

load_dotenv()

PROTOCOL = "pegs"
_logger = get_logger("stables-oracle-events")

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")

# Tunables (env-overridable).
JUMP_THRESHOLD = Config.get_env_float("PEG_EVENT_JUMP_THRESHOLD", 0.10)  # 10% round-over-round
HEARTBEAT_BUFFER = Config.get_env_int("PEG_EVENT_HEARTBEAT_BUFFER", 600)  # grace on heartbeat for gap detection
FALLBACK_LOOKBACK = Config.get_env_int("PEG_EVENT_FALLBACK_LOOKBACK", 86_400)  # 24h when no cursor yet
QUERY_LIMIT = Config.get_env_int("PEG_EVENT_QUERY_LIMIT", 1000)

# Extra history fetched before a feed's cursor so the first new round has a prior
# round to diff against (must exceed the largest feed heartbeat).
CONTEXT_WINDOW = Config.get_env_int("PEG_EVENT_CONTEXT_WINDOW", 2 * 86_400)  # 2 days

# Minimal EACAggregatorProxy ABI — resolve the underlying aggregator that emits AnswerUpdated.
AGGREGATOR_PROXY_ABI = [
    {
        "inputs": [],
        "name": "aggregator",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _cursor_key(aggregator: str) -> str:
    return f"peg_oracle_event_ts_{aggregator.lower()}"


# ---------------------------------------------------------------------------
# Data model + pure anomaly detection (unit tested without a chain or indexer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleRound:
    """One decoded ``AnswerUpdated`` row."""

    aggregator: str  # underlying aggregator address (lowercase)
    round_id: int
    answer: int
    updated_at: int  # on-chain updatedAt (unix seconds)
    block_timestamp: int  # indexer block time (unix seconds) — used for the cursor
    block_number: int  # canonical event order (with log_index); see event_order
    log_index: int
    tx_hash: str
    chain_id: int

    @property
    def event_order(self) -> tuple[int, int]:
        """Canonical on-chain ordering key — the actual emission order of the event.

        Never order the stream by ``round_id``: that is the very field whose
        monotonicity we validate, so using it as a sort key would hide a backwards
        round when two events land in the same block.
        """
        return (self.block_number, self.log_index)


# GraphQL field names for the AnswerUpdated entity (indexer #31). Centralised so a
# final-schema rename only touches one place.
_F_AGGREGATOR = "aggregatorAddress"
_F_ROUND_ID = "roundId"
_F_ANSWER = "current"
_F_UPDATED_AT = "updatedAt"
_F_BLOCK_TS = "blockTimestamp"
_F_BLOCK_NUM = "blockNumber"
_F_LOG_INDEX = "logIndex"
_F_TX = "transactionHash"
_F_CHAIN = "chainId"


def parse_round(row: dict) -> OracleRound:
    """Map a GraphQL ``AnswerUpdated`` row to an :class:`OracleRound`."""
    return OracleRound(
        aggregator=str(row[_F_AGGREGATOR]).lower(),
        round_id=int(row[_F_ROUND_ID]),
        answer=int(row[_F_ANSWER]),
        updated_at=int(row[_F_UPDATED_AT]),
        block_timestamp=int(row[_F_BLOCK_TS]),
        block_number=int(row[_F_BLOCK_NUM]),
        log_index=int(row[_F_LOG_INDEX]),
        tx_hash=str(row.get(_F_TX, "")),
        chain_id=int(row.get(_F_CHAIN, 1)),
    )


def detect_anomalies(
    asset: PeggedAsset,
    feed: ChainlinkFeed,
    rounds: list[OracleRound],
    *,
    since_ts: int,
    jump_threshold: float = JUMP_THRESHOLD,
    heartbeat_buffer: int = HEARTBEAT_BUFFER,
) -> list[Alert]:
    """Return alerts for anomalies among ``rounds``, only for rounds newer than ``since_ts``.

    ``rounds`` may include up to one round at/just before ``since_ts`` for context
    (jump / gap diffing); alerts only fire for the rounds whose ``block_timestamp``
    is strictly greater than ``since_ts`` so reruns never re-alert the same round.
    """
    # Sort by the true emission order (blockNumber, logIndex), NOT round_id — see
    # OracleRound.event_order. Using round_id would mask a backwards round whenever
    # two updates share a block_timestamp.
    ordered = sorted(rounds, key=lambda r: r.event_order)
    alerts: list[Alert] = []

    for prev, cur in zip(ordered, ordered[1:]):
        if cur.block_timestamp <= since_ts:
            continue  # already processed in a prior run

        # Sequence anomaly: roundId must strictly increase.
        if cur.round_id <= prev.round_id:
            alerts.append(
                _alert(
                    asset,
                    feed,
                    AlertSeverity.CRITICAL,
                    "sequence anomaly — roundId did not increase",
                    f"Previous roundId: {prev.round_id}\nCurrent roundId: {cur.round_id}",
                    cur,
                )
            )

        # Large round-over-round jump (unit-independent percentage).
        if prev.answer > 0:
            jump = abs(cur.answer - prev.answer) / prev.answer
            if jump >= jump_threshold:
                direction = "up" if cur.answer > prev.answer else "down"
                alerts.append(
                    _alert(
                        asset,
                        feed,
                        AlertSeverity.HIGH,
                        f"large round-over-round jump {direction} {jump:.2%} (threshold {jump_threshold:.0%})",
                        f"Previous answer: {prev.answer}\nCurrent answer: {cur.answer}",
                        cur,
                    )
                )

        # Missed-heartbeat gap between consecutive updates.
        gap = cur.updated_at - prev.updated_at
        if gap > feed.heartbeat + heartbeat_buffer:
            alerts.append(
                _alert(
                    asset,
                    feed,
                    AlertSeverity.HIGH,
                    f"missed-heartbeat gap {gap}s (heartbeat {feed.heartbeat}s + buffer {heartbeat_buffer}s)",
                    f"Previous updatedAt: {prev.updated_at}\nCurrent updatedAt: {cur.updated_at}",
                    cur,
                )
            )

    return alerts


def _alert(
    asset: PeggedAsset, feed: ChainlinkFeed, severity: AlertSeverity, summary: str, detail: str, cur: OracleRound
) -> Alert:
    """Build a routed alert for a feed anomaly."""
    tx_line = f"\nTx: `{cur.tx_hash}`" if cur.tx_hash else ""
    return Alert(
        severity,
        f"*{asset.name} oracle event — {summary}* ({feed.description})\n"
        f"{detail}\n"
        f"Aggregator: `{cur.aggregator}`"
        f"{tx_line}",
        asset.protocol,
        channel=asset.channel,
    )


def next_cursor(prev_cursor: int, rounds: list[OracleRound]) -> int:
    """Highest ``block_timestamp`` seen, but never below ``prev_cursor``."""
    return max([prev_cursor, *(r.block_timestamp for r in rounds)])


# ---------------------------------------------------------------------------
# Envio GraphQL (mirrors timelock_alerts.py)
# ---------------------------------------------------------------------------


def http_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> dict | None:
    """Make an HTTP request and return the JSON response, retrying transient errors."""
    _logger.info("http_json %s %s", method, url)
    data = None
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
                return parsed if isinstance(parsed, dict) else None
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            _logger.warning("http_json attempt %s/%s failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(2 * attempt)
    return None


def gql_request(query: str, variables: dict) -> dict | None:
    """Execute a GraphQL query against the Envio indexer."""
    if not ENVIO_GRAPHQL_URL:
        raise RuntimeError(
            "ENVIO_GRAPHQL_URL is not set. Set it to the Envio GraphQL endpoint, "
            "e.g. export ENVIO_GRAPHQL_URL='https://.../graphql'."
        )
    return http_json(ENVIO_GRAPHQL_URL, method="POST", body={"query": query, "variables": variables})


def load_answer_updated(aggregators: list[str], since_ts: int, limit: int) -> dict | None:
    """Fetch ``AnswerUpdated`` rows for the given aggregator addresses since ``since_ts``."""
    addresses = sorted({addr for agg in aggregators for addr in (agg, to_checksum_address(agg))})
    query = """
    query GetAnswerUpdated($limit: Int!, $sinceTs: Int!, $addresses: [String!]!) {
      AnswerUpdated(
        where: {
          aggregatorAddress: { _in: $addresses }
          blockTimestamp: { _gt: $sinceTs }
        }
        order_by: { blockTimestamp: asc, logIndex: asc }
        limit: $limit
      ) {
        id
        aggregatorAddress
        roundId
        current
        updatedAt
        blockNumber
        blockTimestamp
        logIndex
        transactionHash
        chainId
      }
    }
    """
    return gql_request(query, {"limit": limit, "sinceTs": since_ts, "addresses": addresses})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def resolve_aggregators(client: Web3Client, assets: list[PeggedAsset]) -> dict[str, tuple[PeggedAsset, ChainlinkFeed]]:
    """Resolve each feed proxy's current underlying aggregator (lowercase) → (asset, feed)."""
    feeds = [(a, a.chainlink_feed) for a in assets if a.chainlink_feed is not None]
    contracts = [client.get_contract(feed.address, AGGREGATOR_PROXY_ABI) for _, feed in feeds]

    with client.batch_requests() as batch:
        for contract in contracts:
            batch.add(contract.functions.aggregator())
        responses = client.execute_batch(batch)

    mapping: dict[str, tuple[PeggedAsset, ChainlinkFeed]] = {}
    for (asset, feed), aggregator in zip(feeds, responses):
        agg = str(aggregator).lower()
        mapping[agg] = (asset, feed)
        _logger.info("%s feed %s -> aggregator %s", asset.name, feed.address, agg)
    return mapping


def process_rounds(
    aggregator_map: dict[str, tuple[PeggedAsset, ChainlinkFeed]],
    rows: list[dict],
    use_cache: bool,
) -> None:
    """Group rows per aggregator, detect anomalies, alert, and advance per-feed cursors."""
    by_aggregator: dict[str, list[OracleRound]] = {}
    for row in rows:
        rnd = parse_round(row)
        if rnd.aggregator in aggregator_map:
            by_aggregator.setdefault(rnd.aggregator, []).append(rnd)

    for aggregator, (asset, feed) in aggregator_map.items():
        rounds = by_aggregator.get(aggregator, [])
        if not rounds:
            continue

        cursor = _read_cursor(aggregator) if use_cache else 0
        alerts = detect_anomalies(asset, feed, rounds, since_ts=cursor)
        _logger.info(
            "%s (%s): %d round(s), %d alert(s) since cursor %s",
            asset.name,
            feed.description,
            len(rounds),
            len(alerts),
            cursor,
        )

        all_sent = True
        for alert in alerts:
            try:
                send_alert(alert)
            except Exception:
                _logger.exception("Failed to send alert for %s", asset.name)
                all_sent = False

        # Advance the cursor only when every send landed, so a failed send is retried
        # next run rather than silently skipped (same trade-off as timelock_alerts).
        if use_cache and all_sent:
            new_cursor = next_cursor(cursor, rounds)
            if new_cursor > cursor:
                write_last_value_to_file(cache_filename, _cursor_key(aggregator), str(new_cursor))


def _read_cursor(aggregator: str) -> int:
    cached = get_last_value_for_key_from_file(cache_filename, _cursor_key(aggregator))
    if cached and str(cached) != "0":
        return int(str(cached))
    return int(time.time()) - FALLBACK_LOOKBACK


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert on Chainlink AnswerUpdated anomalies.")
    parser.add_argument("--limit", type=int, default=QUERY_LIMIT)
    parser.add_argument("--no-cache", action="store_true", help="Disable cursor caching (re-scan the window).")
    args = parser.parse_args()
    use_cache = not args.no_cache

    assets = [a for a in PEGGED_ASSETS if a.chainlink_feed is not None]
    if not assets:
        _logger.info("No Chainlink-backed assets in registry; nothing to do")
        return

    client = ChainManager.get_client(Chain.MAINNET)
    aggregator_map = resolve_aggregators(client, assets)

    # Single query covering all feeds: earliest cursor minus the context window so
    # each feed gets a prior round to diff against; per-feed gating happens later.
    cursors = [_read_cursor(agg) if use_cache else 0 for agg in aggregator_map]
    since_ts = max(0, min(cursors) - CONTEXT_WINDOW)
    _logger.info("Querying AnswerUpdated for %d aggregators since %s", len(aggregator_map), since_ts)

    response = load_answer_updated(list(aggregator_map), since_ts, args.limit)
    if response is None:
        send_error_message("Peg oracle events: Envio API unreachable after 3 retries", PROTOCOL)
        return
    if "errors" in response:
        send_error_message(f"Peg oracle events: GraphQL errors: {response['errors']}", PROTOCOL)
        return

    rows = response.get("data", {}).get("AnswerUpdated", [])
    _logger.info("Fetched %d AnswerUpdated rows", len(rows))
    process_rounds(aggregator_map, rows, use_cache)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
