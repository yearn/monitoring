"""Advance the TIMELOCK_LAST_TS cursor past a stuck batch of events.

When a malformed Markdown alert fails to deliver, `process_events` deliberately
leaves the cache cursor behind so the batch is retried. Once the underlying bug
is fixed, this one-off script can be run on the VPS to bump the cursor past the
stuck batch's max timestamp and stop the hourly re-sends.

Usage:
    uv run python scripts/advance_timelock_cursor.py
    uv run python scripts/advance_timelock_cursor.py --dry-run
    uv run python scripts/advance_timelock_cursor.py --timestamp 1750000000
"""

import argparse
import sys

from dotenv import load_dotenv

from protocols.timelock.timelock_alerts import CACHE_KEY, TIMELOCK_LIST, load_events
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.logger import get_logger

load_dotenv()

_logger = get_logger("advance_timelock_cursor")


def _max_event_timestamp(events: list[dict]) -> int:
    """Return the highest blockTimestamp in the event list."""
    return max(int(event["blockTimestamp"]) for event in events)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Advance the timelock alert cursor past a stuck batch.",
    )
    parser.add_argument(
        "--timestamp",
        type=int,
        default=None,
        help="Explicit UNIX timestamp to set as the new cursor.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum events to fetch from Envio when computing the new cursor (default: 1000).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the new cursor value without writing it.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    args = parser.parse_args()

    current_raw = get_last_value_for_key_from_file(cache_filename, CACHE_KEY)
    current_ts = int(current_raw) if current_raw else 0
    _logger.info("Current %s cursor: %s", CACHE_KEY, current_ts)

    if args.timestamp is not None:
        new_ts = args.timestamp
        _logger.info("Using explicit timestamp: %s", new_ts)
    else:
        if current_ts == 0:
            _logger.error("No existing cursor and --timestamp not provided; aborting")
            sys.exit(1)

        _logger.info("Fetching events since %s from Envio", current_ts)
        response = load_events(args.limit, current_ts, TIMELOCK_LIST)
        if response is None:
            _logger.error("Envio API unreachable")
            sys.exit(1)
        if "errors" in response:
            _logger.error("GraphQL errors: %s", response["errors"])
            sys.exit(1)

        events = response.get("data", {}).get("TimelockEvent", [])
        if not events:
            _logger.info("No events found since %s; cursor already ahead of the stuck batch", current_ts)
            return

        new_ts = _max_event_timestamp(events)
        _logger.info("Found %s events; max timestamp in batch: %s", len(events), new_ts)

    if new_ts <= current_ts:
        _logger.warning(
            "New timestamp (%s) is not ahead of current cursor (%s); nothing to do",
            new_ts,
            current_ts,
        )
        return

    if args.dry_run:
        _logger.info("Dry-run: would set %s to %s", CACHE_KEY, new_ts)
        return

    if not args.yes:
        prompt = f"Set {CACHE_KEY} from {current_ts} to {new_ts}? [y/N] "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            _logger.info("Aborted")
            sys.exit(1)
        if answer not in {"y", "yes"}:
            _logger.info("Aborted")
            sys.exit(1)

    write_last_value_to_file(cache_filename, CACHE_KEY, str(new_ts))
    _logger.info("Updated %s: %s -> %s", CACHE_KEY, current_ts, new_ts)


if __name__ == "__main__":
    main()
