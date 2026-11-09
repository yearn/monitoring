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

A chain whose block timestamp cannot be resolved (RPC error, `null` result,
malformed body) has *unknown* lag: it is neither confirmed fresh nor confirmed
stale. One such run is logged; once it persists for `--unknown-alert-after-runs`
consecutive runs the chain alerts as unmeasured coverage, and a note follows
when its lag is measurable again.

The RPC round-trip is what makes the check meaningful: envio parks
`chain_metadata.block_height` at the last processed block once a chain looks
caught up, so a stalled indexer keeps reporting itself as zero blocks behind.
The same trap is documented by the indexer itself.

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
from utils.formatting import format_duration, redact_urls
from utils.http_client import request_with_retry
from utils.logger import get_logger
from utils.telegram import send_envio_error_message
from utils.web3_wrapper import ChainManager

load_dotenv()

logger = get_logger("yearn.check_indexer_freshness")

PROTOCOL = "yearn"
# Alert-history key: internal-only, so indexer alerts stay off the public Yearn page.
ALERT_PROTOCOL = "yearn-internal"

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")

# A chain is stale once its newest indexed block is older than this. One hour is
# generous for every indexed chain: the slowest of them (Mainnet) produces a
# block every ~12s, so an hour of lag is always a real stall, never jitter.
DEFAULT_MAX_LAG_MINUTES = 60

# Staleness persists for as long as the indexer takes to catch up (a re-sync can
# run for days), so re-alerting every hourly run would bury the envio channel.
# Each chain alerts on the way into staleness, then at most once per cooldown
# window, then once more when it recovers.
DEFAULT_ALERT_COOLDOWN_HOURS = 6

# A single unmeasurable run is usually a provider blip (observed: one `null`
# block on Katana, fine on the next run), so unknown lag only alerts once it has
# persisted this many consecutive runs — two hourly runs is about an hour blind,
# matching the default staleness threshold.
DEFAULT_UNKNOWN_ALERT_AFTER_RUNS = 2

# Attempts per block-timestamp lookup when the provider answers with an RPC
# error, a `null` block or a malformed body. Transport failures are not retried
# here: `make_request` has already rotated through every provider with backoff.
BLOCK_TIMESTAMP_ATTEMPTS = 3
BLOCK_TIMESTAMP_RETRY_DELAY_SECONDS = 5

# Keyed per chain so one lagging chain can't suppress an alert for another.
CACHE_KEY_LAST_ALERT_PREFIX = "YEARN_INDEXER_STALE_ALERT_"
# Consecutive runs a chain's lag could not be measured (0 = measurable).
CACHE_KEY_UNKNOWN_RUNS_PREFIX = "YEARN_INDEXER_UNKNOWN_RUNS_"
# Last unknown-lag alert per chain (0 = none outstanding).
CACHE_KEY_UNKNOWN_ALERT_PREFIX = "YEARN_INDEXER_UNKNOWN_ALERT_"

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


class BlockTimestampUnavailableError(Exception):
    """A block's timestamp could not be resolved, so the chain's lag is unknown.

    The message is forwarded to Telegram, so it must stay credential-safe: it
    never includes transport exception text.
    """


@dataclass(frozen=True)
class ChainFreshness:
    """Freshness of a single indexed chain."""

    chain: Chain
    latest_processed_block: int
    # None when the block timestamp could not be resolved, i.e. lag is unknown.
    lag_seconds: int | None
    # Why the lag is unknown; None whenever `lag_seconds` is set.
    unavailable_reason: str | None = None

    @property
    def name(self) -> str:
        """Human-readable chain name, e.g. "Mainnet"."""
        return self.chain.name.capitalize()

    @property
    def is_measured(self) -> bool:
        """Return True when the lag is known; unknown lag is neither fresh nor stale."""
        return self.lag_seconds is not None

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
            "e.g. export ENVIO_GRAPHQL_URL='https://indexer.hyperindex.xyz/5a089e4/v1/graphql'."
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


def parse_block_timestamp(response: object) -> int:
    """Extract the timestamp from a raw `eth_getBlockByNumber` JSON-RPC response.

    The raw call bypasses web3's response handling, so an RPC error or a `null`
    block comes back as an ordinary dict rather than an exception.

    Args:
        response: The decoded JSON-RPC response.

    Returns:
        The block timestamp in seconds.

    Raises:
        BlockTimestampUnavailableError: The response is an RPC error, a `null`
            block, or does not carry a hex timestamp.
    """
    if not isinstance(response, dict):
        raise BlockTimestampUnavailableError(f"malformed RPC response ({type(response).__name__})")
    error = response.get("error")
    if error:
        if isinstance(error, dict):
            raise BlockTimestampUnavailableError(f"RPC error {error.get('code')}: {error.get('message')}")
        raise BlockTimestampUnavailableError(f"RPC error: {error}")
    block = response.get("result")
    if block is None:
        raise BlockTimestampUnavailableError("block not found (RPC returned a null result)")
    if not isinstance(block, dict):
        raise BlockTimestampUnavailableError(f"malformed block ({type(block).__name__} result)")
    raw_timestamp = block.get("timestamp")
    try:
        if not isinstance(raw_timestamp, str):
            raise ValueError
        return int(raw_timestamp, 16)
    except ValueError:
        raise BlockTimestampUnavailableError(f"malformed block timestamp {raw_timestamp!r}") from None


def fetch_block_timestamp(chain: Chain, block_number: int) -> int:
    """Fetch the unix timestamp of a block.

    A response-level failure (RPC error, `null` block, malformed body) is retried
    up to `BLOCK_TIMESTAMP_ATTEMPTS` times, moving to the next configured
    provider between attempts: a load-balanced node that has not seen the block
    yet typically has it seconds later, or another provider does.

    Args:
        chain: Chain the block belongs to.
        block_number: Block to look up.

    Returns:
        The block timestamp in seconds.

    Raises:
        BlockTimestampUnavailableError: Every attempt failed. Never raised for
            anything else, so one unreachable RPC cannot abort the other chains.

    Note:
        Uses the raw JSON-RPC call rather than `eth.get_block`, which rejects the
        97-byte PoA `extraData` on Polygon and pre-Bedrock Optimism blocks — and
        old blocks are exactly what a lagging indexer points at.
    """
    try:
        client = ChainManager.get_client(chain)
    except Exception as exc:  # noqa: BLE001 - no provider configured is a coverage gap, not a crash
        raise BlockTimestampUnavailableError(f"no RPC client: {redact_urls(str(exc))}") from exc

    reason = "no attempts made"
    for attempt in range(1, BLOCK_TIMESTAMP_ATTEMPTS + 1):
        try:
            response = client.make_request("eth_getBlockByNumber", [hex(block_number), False])
        except Exception as exc:  # noqa: BLE001 - a dead provider is not a stale indexer
            # make_request already retried every provider with backoff and logged each
            # failure. This reason reaches Telegram, so it carries no exception text:
            # transport errors can echo the credential-bearing request path.
            raise BlockTimestampUnavailableError(
                f"RPC request failed ({type(exc).__name__}), see monitor logs"
            ) from exc
        try:
            return parse_block_timestamp(response)
        except BlockTimestampUnavailableError as exc:
            reason = redact_urls(str(exc))
        logger.warning(
            "Block %d timestamp on %s unavailable (attempt %d/%d): %s",
            block_number,
            chain.name,
            attempt,
            BLOCK_TIMESTAMP_ATTEMPTS,
            reason,
        )
        if attempt < BLOCK_TIMESTAMP_ATTEMPTS:
            client.rotate_provider()
            time.sleep(BLOCK_TIMESTAMP_RETRY_DELAY_SECONDS)
    raise BlockTimestampUnavailableError(f"{reason} (after {BLOCK_TIMESTAMP_ATTEMPTS} attempts)")


def collect_freshness(rows: list[dict], now: int) -> list[ChainFreshness]:
    """Resolve how far behind wall-clock time each expected chain is.

    The indexer may cover chains this repo doesn't read from. Nothing here
    consumes their events, so they are skipped rather than alerted on.
    Expected chains absent from `rows` produce no entry — see
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
        try:
            block_timestamp = fetch_block_timestamp(chain, latest_block)
        except BlockTimestampUnavailableError as exc:
            logger.warning("%s: block %d, lag unknown — %s", chain.name, latest_block, exc)
            freshness.append(
                ChainFreshness(
                    chain=chain, latest_processed_block=latest_block, lag_seconds=None, unavailable_reason=str(exc)
                )
            )
            continue
        lag = max(0, now - block_timestamp)
        logger.info("%s: block %d, lag %s", chain.name, latest_block, format_duration(lag))
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
    ]
    return "\n".join(lines)


def build_unknown_alert_message(unknown: list[tuple[ChainFreshness, int]]) -> str:
    """Build the alert body for chains whose lag could not be measured.

    Args:
        unknown: Each unmeasured chain with its count of consecutive unknown runs.
    """
    lines = [
        f"Envio indexer freshness unknown on {len(unknown)} chain(s) — block timestamps could not be "
        "resolved, so lag is unmeasured (neither confirmed fresh nor stale).",
        "",
    ]
    for entry, runs in unknown:
        lines.append(
            f"- {entry.name} (chain {entry.chain.chain_id}): unknown for {runs} consecutive check(s), "
            f"last indexed block {entry.latest_processed_block} — {entry.unavailable_reason}"
        )
    return "\n".join(lines)


def _get_int(prefix: str, chain_id: int) -> int:
    """Read a per-chain integer from the cache (0 when unset)."""
    return int(get_last_value_for_key_from_file(cache_filename, f"{prefix}{chain_id}"))


def _set_int(prefix: str, chain_id: int, value: int) -> None:
    """Write a per-chain integer to the cache."""
    write_last_value_to_file(cache_filename, f"{prefix}{chain_id}", value)


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
    send_envio_error_message(
        f"Envio indexer caught up: {names}", PROTOCOL, source="indexer_freshness", alert_protocol=ALERT_PROTOCOL
    )
    for chain in recovered:
        _set_last_alert_timestamp(chain.chain.chain_id, 0)


def update_unknown_coverage(
    freshness: list[ChainFreshness], now: int, alert_after_runs: int, cooldown_seconds: int
) -> list[tuple[ChainFreshness, int]]:
    """Track consecutive unmeasurable runs per chain and pick which should alert.

    Measured chains reset their streak. Unmeasured chains extend it and alert
    once it reaches `alert_after_runs`, then at most once per cooldown. The
    stale-alert state is deliberately untouched either way: unknown lag neither
    confirms a stale chain recovered nor that a fresh one stalled.

    Args:
        freshness: Every expected chain with sync state this run.
        now: Current unix timestamp.
        alert_after_runs: Consecutive unknown runs before alerting.
        cooldown_seconds: Minimum gap between two unknown-lag alerts per chain.

    Returns:
        Unmeasured chains to alert on now, each with its consecutive-run count.
        The caller records the alert via `_set_int(CACHE_KEY_UNKNOWN_ALERT_PREFIX, ...)`.
    """
    to_alert: list[tuple[ChainFreshness, int]] = []
    for entry in freshness:
        chain_id = entry.chain.chain_id
        if entry.is_measured:
            if _get_int(CACHE_KEY_UNKNOWN_RUNS_PREFIX, chain_id):
                _set_int(CACHE_KEY_UNKNOWN_RUNS_PREFIX, chain_id, 0)
            continue
        runs = _get_int(CACHE_KEY_UNKNOWN_RUNS_PREFIX, chain_id) + 1
        _set_int(CACHE_KEY_UNKNOWN_RUNS_PREFIX, chain_id, runs)
        last_alert = _get_int(CACHE_KEY_UNKNOWN_ALERT_PREFIX, chain_id)
        if runs >= alert_after_runs and now - last_alert >= cooldown_seconds:
            to_alert.append((entry, runs))
    return to_alert


def report_measurable_again(measured: list[ChainFreshness]) -> None:
    """Send a note for chains that alerted as unmeasured and now have a known lag.

    Whether the chain turned out fresh or stale is reported alongside; a stale
    one is also handled by the regular stale alert.
    """
    restored = [entry for entry in measured if _get_int(CACHE_KEY_UNKNOWN_ALERT_PREFIX, entry.chain.chain_id) > 0]
    if not restored:
        return
    names = ", ".join(f"{entry.name} ({format_duration(entry.lag_seconds or 0)} behind)" for entry in restored)
    send_envio_error_message(
        f"Envio indexer lag measurable again: {names}",
        PROTOCOL,
        source="indexer_freshness",
        alert_protocol=ALERT_PROTOCOL,
    )
    for entry in restored:
        _set_int(CACHE_KEY_UNKNOWN_ALERT_PREFIX, entry.chain.chain_id, 0)


def main() -> None:
    """Alert on every expected chain the indexer is lagging on or has lost."""
    args = parse_args()
    max_lag_seconds = args.max_lag_minutes * 60
    cooldown_seconds = args.alert_cooldown_hours * 3600

    try:
        rows = fetch_chain_metadata()
    except IndexerUnavailableError as exc:
        # The endpoint being down is itself the outage we are watching for, so it
        # alerts on every run rather than riding the per-chain cooldown. Unlike
        # routine Envio errors it notifies: every Envio-backed monitor is blind
        # until it recovers, and a silent message went unnoticed in practice.
        logger.error("Indexer unavailable: %s", exc)
        send_envio_error_message(
            f"Envio indexer unavailable: {exc}",
            PROTOCOL,
            disable_notification=False,
            source="indexer_freshness",
            alert_protocol=ALERT_PROTOCOL,
        )
        return

    now = int(time.time())
    freshness = collect_freshness(rows, now)
    measured = [entry for entry in freshness if entry.is_measured]
    stale = [entry for entry in measured if entry.is_stale(max_lag_seconds)]
    missing = missing_chains(freshness)
    if missing:
        logger.error("No sync state for %s", ", ".join(chain.name for chain in missing))

    report_recovered([entry for entry in measured if entry not in stale])
    report_measurable_again(measured)

    unknown_to_alert = update_unknown_coverage(freshness, now, args.unknown_alert_after_runs, cooldown_seconds)
    if unknown_to_alert:
        send_envio_error_message(
            build_unknown_alert_message(unknown_to_alert),
            PROTOCOL,
            source="indexer_freshness",
            alert_protocol=ALERT_PROTOCOL,
        )
        for entry, _ in unknown_to_alert:
            _set_int(CACHE_KEY_UNKNOWN_ALERT_PREFIX, entry.chain.chain_id, now)

    if not stale and not missing:
        unmeasured = [entry.name for entry in freshness if not entry.is_measured]
        if unmeasured:
            logger.warning(
                "Indexer is fresh on %d measured chain(s); lag unknown on %s",
                len(measured),
                ", ".join(unmeasured),
            )
        else:
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
        alert_protocol=ALERT_PROTOCOL,
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
    parser.add_argument(
        "--unknown-alert-after-runs",
        type=int,
        default=int(os.getenv("INDEXER_UNKNOWN_ALERT_AFTER_RUNS", DEFAULT_UNKNOWN_ALERT_AFTER_RUNS)),
        help=(
            "Alert once a chain's lag has been unmeasurable for this many consecutive runs "
            f"(default: {DEFAULT_UNKNOWN_ALERT_AFTER_RUNS})"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
