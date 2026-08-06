#!/usr/bin/env python3
"""Alert when the Envio indexer stops keeping up with the chain head.

Several monitors read their events from the Envio indexer (`ENVIO_GRAPHQL_URL`):
Yearn large flows, the timelock alerts and the 3jane borrower watch. When the
indexer stalls they degrade silently — GraphQL keeps answering, it just stops
returning new rows — so an outage is indistinguishable from "nothing happened".

This check reads `chain_metadata` from the indexer and, for every chain this repo
monitors, resolves the wall-clock timestamp of `latest_processed_block` from an
RPC. Any chain whose newest indexed block is older than the lag threshold
(default 60 minutes) is reported to the envio channel — as is any expected chain
the indexer reports no sync state for at all, since "nothing was stale" must
never be mistaken for "everything is fresh".

The RPC round-trip is what makes the check meaningful: envio parks
`chain_metadata.block_height` at the last processed block once a chain looks
caught up, so a stalled indexer keeps reporting itself as zero blocks behind.
The same trap is documented in the indexer's own dashboard
(https://envio-monitoring.yearn.dev/).

Usage:
    python protocols/yearn/check_indexer_freshness.py [--max-lag-minutes 60]
"""

import argparse
import os
import time
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.formatting import format_duration
from utils.http_client import request_with_retry
from utils.logger import get_logger
from utils.telegram import send_envio_error_message
from utils.web3_wrapper import ChainManager

load_dotenv()

logger = get_logger("yearn.check_indexer_freshness")

PROTOCOL = "yearn"

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")
DASHBOARD_URL = "https://envio-monitoring.yearn.dev/"

# A chain is stale once its newest indexed block is older than this. One hour is
# generous for every indexed chain: the slowest of them (Mainnet) produces a
# block every ~12s, so an hour of lag is always a real stall, never jitter.
DEFAULT_MAX_LAG_MINUTES = 60

# Staleness persists for as long as the indexer takes to catch up (a re-sync can
# run for days), so re-alerting every hourly run would bury the envio channel.
# Each chain alerts on the way into staleness, then at most once per cooldown
# window, then once more when it recovers.
DEFAULT_ALERT_COOLDOWN_HOURS = 6

# Keyed per chain so one lagging chain can't suppress an alert for another.
CACHE_KEY_LAST_ALERT_PREFIX = "YEARN_INDEXER_STALE_ALERT_"

# Chains whose indexed events feed monitors here. Every one of them must show up
# in chain_metadata — a chain that silently drops out of the indexer's config
# leaves its monitors blind while the endpoint keeps answering, which is exactly
# the failure this script exists to catch.
#
# Kept explicit rather than derived from `Chain` so that adding an enum member
# for an unrelated protocol doesn't start alerting that the indexer is missing a
# chain it was never asked to index. Add a chain here when its events start
# feeding a monitor.
EXPECTED_CHAINS: tuple[Chain, ...] = (
    Chain.MAINNET,
    Chain.OPTIMISM,
    Chain.POLYGON,
    Chain.BASE,
    Chain.ARBITRUM,
    Chain.KATANA,
)
_EXPECTED_BY_CHAIN_ID: dict[int, Chain] = {chain.chain_id: chain for chain in EXPECTED_CHAINS}

CHAIN_METADATA_QUERY = """
{
  chain_metadata {
    chain_id
    latest_processed_block
    block_height
  }
}
"""


class IndexerUnavailableError(Exception):
    """The indexer's GraphQL endpoint could not be queried or returned no chains."""


@dataclass(frozen=True)
class ChainFreshness:
    """Freshness of a single indexed chain."""

    chain: Chain
    latest_processed_block: int
    # None when the block timestamp could not be resolved, i.e. lag is unknown.
    lag_seconds: int | None

    @property
    def name(self) -> str:
        """Human-readable chain name, e.g. "Mainnet"."""
        return self.chain.name.capitalize()

    def is_stale(self, max_lag_seconds: int) -> bool:
        """Return True when the newest indexed block is older than the threshold."""
        return self.lag_seconds is not None and self.lag_seconds > max_lag_seconds


def fetch_chain_metadata() -> list[dict]:
    """Fetch per-chain sync state from the indexer.

    Returns:
        The `chain_metadata` rows, one per indexed chain.

    Raises:
        IndexerUnavailableError: The endpoint is unset, unreachable, returned
            GraphQL errors, or reported no chains at all.
    """
    if not ENVIO_GRAPHQL_URL:
        raise IndexerUnavailableError(
            "ENVIO_GRAPHQL_URL is not set. Set it to the Envio GraphQL endpoint, "
            "e.g. export ENVIO_GRAPHQL_URL='https://envio-gql.yearn.dev/v1/graphql'."
        )

    try:
        response = request_with_retry("post", ENVIO_GRAPHQL_URL, json={"query": CHAIN_METADATA_QUERY})
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise IndexerUnavailableError(f"GraphQL request to {ENVIO_GRAPHQL_URL} failed: {exc}") from exc

    if payload.get("errors"):
        raise IndexerUnavailableError(f"GraphQL errors from {ENVIO_GRAPHQL_URL}: {payload['errors']}")

    rows = (payload.get("data") or {}).get("chain_metadata") or []
    if not rows:
        raise IndexerUnavailableError(f"chain_metadata is empty at {ENVIO_GRAPHQL_URL} — indexer has no sync state")
    return rows


def fetch_block_timestamp(chain: Chain, block_number: int) -> int | None:
    """Fetch the unix timestamp of a block.

    Args:
        chain: Chain the block belongs to.
        block_number: Block to look up.

    Returns:
        The block timestamp in seconds, or None when the lookup failed — one
        unreachable RPC must not mask the other chains' results.

    Note:
        Uses the raw JSON-RPC call rather than `eth.get_block`, which rejects the
        97-byte PoA `extraData` on Polygon and pre-Bedrock Optimism blocks — and
        old blocks are exactly what a lagging indexer points at.
    """
    try:
        client = ChainManager.get_client(chain)
        response = client.make_request("eth_getBlockByNumber", [hex(block_number), False])
        return int(response["result"]["timestamp"], 16)
    except Exception as exc:  # noqa: BLE001 - a dead provider is not a stale indexer
        logger.warning("Failed to fetch block %d timestamp on %s: %s", block_number, chain.name, exc)
        return None


def collect_freshness(rows: list[dict], now: int) -> list[ChainFreshness]:
    """Resolve how far behind wall-clock time each expected chain is.

    The indexer covers chains this repo doesn't read from (Gnosis, Berachain).
    Nothing here consumes their events, so they are skipped rather than alerted
    on. Expected chains absent from `rows` produce no entry — see
    `missing_chains`.

    Args:
        rows: `chain_metadata` rows from the indexer.
        now: Current unix timestamp.

    Returns:
        One ChainFreshness per expected chain present in `rows`, sorted by chain id.
    """
    freshness: list[ChainFreshness] = []
    for row in rows:
        chain_id = int(row["chain_id"])
        chain = _EXPECTED_BY_CHAIN_ID.get(chain_id)
        if chain is None:
            logger.info("Chain %d is indexed but not monitored here, skipping", chain_id)
            continue
        latest_block = int(row.get("latest_processed_block") or 0)
        if latest_block <= 0:
            # No processed block means no usable sync state — left out here so
            # `missing_chains` reports it rather than treating it as fresh.
            logger.warning("%s has no processed block yet", chain.name)
            continue
        block_timestamp = fetch_block_timestamp(chain, latest_block)
        lag = max(0, now - block_timestamp) if block_timestamp is not None else None
        lag_text = format_duration(lag) if lag is not None else "unknown"
        logger.info("%s: block %d, lag %s", chain.name, latest_block, lag_text)
        freshness.append(ChainFreshness(chain=chain, latest_processed_block=latest_block, lag_seconds=lag))
    return sorted(freshness, key=lambda f: f.chain.chain_id)


def missing_chains(freshness: list[ChainFreshness]) -> list[Chain]:
    """Return expected chains the indexer reported no usable sync state for.

    Covers both a chain absent from `chain_metadata` entirely (dropped from the
    indexer config, partial restart) and one present with no processed block.
    Either way its monitors are blind, so "nothing was stale" must not be read
    as "everything is fresh".
    """
    present = {entry.chain for entry in freshness}
    return [chain for chain in EXPECTED_CHAINS if chain not in present]


def build_alert_message(stale: list[ChainFreshness], missing: list[Chain], max_lag_seconds: int) -> str:
    """Build the plain-text alert body for lagging and missing chains."""
    affected = len(stale) + len(missing)
    lines = [
        f"Envio indexer problem on {affected} chain(s) — events may be missing from monitoring alerts.",
        "",
    ]
    for entry in stale:
        lag = format_duration(entry.lag_seconds or 0)
        lines.append(
            f"- {entry.name} (chain {entry.chain.chain_id}): {lag} behind, last block {entry.latest_processed_block}"
        )
    for chain in missing:
        lines.append(f"- {chain.name.capitalize()} (chain {chain.chain_id}): no sync state reported by the indexer")
    lines += [
        "",
        f"Threshold: {format_duration(max_lag_seconds)}",
        f"Dashboard: {DASHBOARD_URL}",
    ]
    return "\n".join(lines)


def _last_alert_timestamp(chain_id: int) -> int:
    """Return when this chain last alerted, or 0 if it is currently considered healthy."""
    return int(get_last_value_for_key_from_file(cache_filename, f"{CACHE_KEY_LAST_ALERT_PREFIX}{chain_id}"))


def _set_last_alert_timestamp(chain_id: int, timestamp: int) -> None:
    """Record the last alert time for a chain (0 clears it back to healthy)."""
    write_last_value_to_file(cache_filename, f"{CACHE_KEY_LAST_ALERT_PREFIX}{chain_id}", timestamp)


def _is_due_for_alert(chain: Chain, now: int, cooldown_seconds: int) -> bool:
    """Return True when this chain has not alerted within the cooldown window."""
    return now - _last_alert_timestamp(chain.chain_id) >= cooldown_seconds


def chains_to_alert(
    stale: list[ChainFreshness], missing: list[Chain], now: int, cooldown_seconds: int
) -> tuple[list[ChainFreshness], list[Chain]]:
    """Filter unhealthy chains down to those outside their re-alert cooldown.

    Args:
        stale: Chains currently past the lag threshold.
        missing: Expected chains with no usable sync state.
        now: Current unix timestamp.
        cooldown_seconds: Minimum gap between two alerts for the same chain.

    Returns:
        The stale and missing chains that should alert on this run.
    """
    return (
        [entry for entry in stale if _is_due_for_alert(entry.chain, now, cooldown_seconds)],
        [chain for chain in missing if _is_due_for_alert(chain, now, cooldown_seconds)],
    )


def report_recovered(fresh: list[ChainFreshness]) -> None:
    """Send a recovery note for chains that had alerted and are now caught up."""
    recovered = [chain for chain in fresh if _last_alert_timestamp(chain.chain.chain_id) > 0]
    if not recovered:
        return
    names = ", ".join(f"{chain.name} ({format_duration(chain.lag_seconds or 0)} behind)" for chain in recovered)
    send_envio_error_message(f"Envio indexer caught up: {names}", PROTOCOL, source="indexer_freshness")
    for chain in recovered:
        _set_last_alert_timestamp(chain.chain.chain_id, 0)


def main() -> None:
    """Alert on every expected chain the indexer is lagging on or has lost."""
    args = parse_args()
    max_lag_seconds = args.max_lag_minutes * 60
    cooldown_seconds = args.alert_cooldown_hours * 3600

    try:
        rows = fetch_chain_metadata()
    except IndexerUnavailableError as exc:
        # The endpoint being down is itself the outage we are watching for, so it
        # alerts on every run rather than riding the per-chain cooldown.
        logger.error("Indexer unavailable: %s", exc)
        send_envio_error_message(
            f"Envio indexer unavailable: {exc}\nDashboard: {DASHBOARD_URL}",
            PROTOCOL,
            source="indexer_freshness",
        )
        return

    now = int(time.time())
    freshness = collect_freshness(rows, now)
    stale = [entry for entry in freshness if entry.is_stale(max_lag_seconds)]
    missing = missing_chains(freshness)
    if missing:
        logger.error("No sync state for %s", ", ".join(chain.name for chain in missing))

    report_recovered([entry for entry in freshness if entry not in stale and entry.lag_seconds is not None])

    if not stale and not missing:
        logger.info("Indexer is fresh on all %d expected chain(s)", len(freshness))
        return

    stale_to_alert, missing_to_alert = chains_to_alert(stale, missing, now, cooldown_seconds)
    if not stale_to_alert and not missing_to_alert:
        logger.info("All %d unhealthy chain(s) already alerted within the cooldown window", len(stale) + len(missing))
        return

    send_envio_error_message(
        build_alert_message(stale_to_alert, missing_to_alert, max_lag_seconds),
        PROTOCOL,
        source="indexer_freshness",
    )
    for entry in stale_to_alert:
        _set_last_alert_timestamp(entry.chain.chain_id, now)
    for chain in missing_to_alert:
        _set_last_alert_timestamp(chain.chain_id, now)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Alert when the Envio indexer falls behind the chain head")
    parser.add_argument(
        "--max-lag-minutes",
        type=int,
        default=int(os.getenv("INDEXER_MAX_LAG_MINUTES", DEFAULT_MAX_LAG_MINUTES)),
        help=f"Alert when a chain's newest indexed block is older than this (default: {DEFAULT_MAX_LAG_MINUTES})",
    )
    parser.add_argument(
        "--alert-cooldown-hours",
        type=int,
        default=int(os.getenv("INDEXER_ALERT_COOLDOWN_HOURS", DEFAULT_ALERT_COOLDOWN_HOURS)),
        help=f"Minimum hours between repeat alerts for the same chain (default: {DEFAULT_ALERT_COOLDOWN_HOURS})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
