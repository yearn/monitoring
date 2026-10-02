#!/usr/bin/env python3
"""Alert on TimelockController operations that are ready but nobody has executed.

An OpenZeppelin TimelockController operation never expires. Once its delay has
passed it stays executable until someone executes or cancels it, so an operation
the team forgot about, like an infiniFi rate change scheduled on 23/08/2026, can
land weeks later without a new alert. ``timelock_alerts.py`` reports each
operation once, when it is scheduled.

This script reads the CallScheduled events Envio indexed for the monitored
timelocks over a lookback window, then asks each timelock for the operation's
state with ``getTimestamp(id)``: 0 is unset or cancelled, 1 is executed, and
anything else is the time it became ready. Envio indexes only CallScheduled, so
the state comes from the chain. Operations ready for longer than
``TIMELOCK_STALE_DAYS`` (default 7) are reported once each, silently, to the
protocol's channel.
"""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from eth_utils import to_checksum_address

from protocols.timelock.timelock_alerts import (
    TIMELOCKS,
    YEARN_TIMELOCK_INTERNAL_PROTOCOL,
    TimelockConfig,
    alert_history_protocol,
    load_events,
)
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.calldata.decoder import decode_calldata
from utils.chains import EXPLORER_URLS, Chain
from utils.logger import get_logger
from utils.telegram import MAX_MESSAGE_LENGTH, send_envio_error_message, send_telegram_message
from utils.web3_wrapper import ChainManager

logger = get_logger("timelock_stale_operations")

STALE_DAYS = int(os.getenv("TIMELOCK_STALE_DAYS", "7"))
LOOKBACK_DAYS = int(os.getenv("TIMELOCK_STALE_LOOKBACK_DAYS", "180"))
PAGE_SIZE = 1000
BATCH_SIZE = 50
# Limit call details as well as their total character budget.
MAX_CALLS_SHOWN = 10
DAY = 86400

# getTimestamp sentinels from OpenZeppelin TimelockController.
_UNSET = 0
_DONE = 1

_TIMELOCK_ABI = [
    {
        "name": "getTimestamp",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "id", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


@dataclass(frozen=True)
class Operation:
    """One scheduled TimelockController operation and its calls."""

    timelock: TimelockConfig
    operation_id: str
    scheduled_at: int
    transaction_hash: str
    calls: tuple[tuple[str, str], ...]  # (target, calldata)


def load_scheduled_operations(since_ts: int) -> list[Operation] | None:
    """Every TimelockController operation scheduled since ``since_ts``, or None if Envio fails."""
    events_by_operation: dict[tuple[int, str, str], list[dict]] = {}
    seen: set[str] = set()
    cursor = since_ts
    while True:
        response = load_events(PAGE_SIZE, cursor)
        if response is None or "errors" in response:
            logger.error("Envio query failed: %s", None if response is None else response["errors"])
            return None
        events = response.get("data", {}).get("TimelockEvent", [])
        new_events = [event for event in events if event["id"] not in seen]
        for event in new_events:
            seen.add(event["id"])
            if event.get("timelockType") != "TimelockController" or event.get("eventName") != "CallScheduled":
                continue
            key = (int(event["chainId"]), event["timelockAddress"].lower(), event["operationId"])
            events_by_operation.setdefault(key, []).append(event)
        if len(events) < PAGE_SIZE or not new_events:
            break
        # Re-read the last second so a batch split across pages is not lost; ids dedupe the overlap.
        cursor = int(events[-1]["blockTimestamp"]) - 1

    operations = []
    for (chain_id, address, operation_id), events in events_by_operation.items():
        timelock = TIMELOCKS.get((address, chain_id))
        if timelock is None:
            continue
        events.sort(key=lambda event: int(event.get("index") or 0))
        operations.append(
            Operation(
                timelock=timelock,
                operation_id=operation_id,
                scheduled_at=int(events[0]["blockTimestamp"]),
                transaction_hash=events[0]["transactionHash"],
                calls=tuple((event.get("target") or "", event.get("data") or "0x") for event in events),
            )
        )
    return operations


def ready_times(operations: list[Operation]) -> dict[str, int]:
    """``getTimestamp`` for each operation, keyed by ``chain:timelock:id``; unreadable ones are left out."""
    result: dict[str, int] = {}
    by_chain: dict[int, list[Operation]] = {}
    for operation in operations:
        by_chain.setdefault(operation.timelock.chain_id, []).append(operation)
    for chain_id, chain_operations in by_chain.items():
        try:
            client = ChainManager.get_client(Chain.from_chain_id(chain_id))
        except ValueError:
            logger.warning("No client for chain %s", chain_id)
            continue
        for start in range(0, len(chain_operations), BATCH_SIZE):
            chunk = chain_operations[start : start + BATCH_SIZE]
            try:
                with client.batch_requests() as batch:
                    for operation in chunk:
                        contract = client.get_contract(to_checksum_address(operation.timelock.address), _TIMELOCK_ABI)
                        batch.add(contract.functions.getTimestamp(bytes.fromhex(operation.operation_id[2:])))
                    responses = client.execute_batch(batch)
            except Exception as error:  # noqa: BLE001 - one chain's RPC failure must not hide the others
                logger.warning("getTimestamp batch failed on chain %s: %s", chain_id, error)
                continue
            for operation, timestamp in zip(chunk, responses, strict=True):
                result[_key(operation)] = int(timestamp)
    return result


def stale_operations(operations: list[Operation], ready: dict[str, int], now: int) -> list[tuple[Operation, int]]:
    """(operation, ready_at) for operations ready for more than STALE_DAYS and not executed or cancelled."""
    stale = []
    for operation in operations:
        ready_at = ready.get(_key(operation))
        if ready_at is None or ready_at in (_UNSET, _DONE) or ready_at > now:
            continue
        if now - ready_at > STALE_DAYS * DAY:
            stale.append((operation, ready_at))
    return stale


def _key(operation: Operation) -> str:
    return f"{operation.timelock.chain_id}:{operation.timelock.address}:{operation.operation_id}"


def cache_key(operation: Operation) -> str:
    """Dedupe key for an operation. No colons: the file cache backend stores rows as ``key:value``."""
    return f"TIMELOCK_STALE_{operation.timelock.chain_id}_{operation.timelock.address}_{operation.operation_id}"


def _date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%d/%m/%Y")


def _call_line(chain_id: int, target: str, data: str, explorer: str | None) -> str:
    decoded = decode_calldata(data, chain_id=chain_id, target=target) if len(data) >= 10 else None
    function = decoded.signature if decoded else (data[:10] if len(data) >= 10 else "no calldata")
    link = f"[{target}]({explorer}/address/{target})" if explorer else target
    return f"- {link} `{function}`"


def _format_calls(operation: Operation, budget: int) -> str:
    """Fit complete call lines and an omitted-call notice into the available budget."""
    lines: list[str] = []
    chain_id = operation.timelock.chain_id
    explorer = EXPLORER_URLS.get(chain_id)
    for target, data in operation.calls[:MAX_CALLS_SHOWN]:
        line = _call_line(chain_id, target, data, explorer)
        remaining = len(operation.calls) - len(lines) - 1
        notice = f"\n- … and {remaining} more calls (see the schedule tx)" if remaining else ""
        if len("\n".join([*lines, line])) + len(notice) > budget:
            break
        lines.append(line)
    remaining = len(operation.calls) - len(lines)
    if remaining:
        lines.append(f"- … and {remaining} more calls (see the schedule tx)")
    return "\n".join(lines)


def format_operation(operation: Operation, ready_at: int, now: int) -> str:
    """Alert text for one stale operation."""
    chain_id = operation.timelock.chain_id
    explorer = EXPLORER_URLS.get(chain_id)
    address = to_checksum_address(operation.timelock.address)
    timelock = f"[{address}]({explorer}/address/{address})" if explorer else address
    tx = (
        f"[{operation.transaction_hash}]({explorer}/tx/{operation.transaction_hash})"
        if explorer
        else operation.transaction_hash
    )
    prefix = (
        f"*{operation.timelock.label}* (chain {chain_id}): {timelock}\n"
        f"Operation: `{operation.operation_id}`\n"
        f"Scheduled {_date(operation.scheduled_at)} in {tx}\n"
        f"Ready since {_date(ready_at)} ({(now - ready_at) // DAY} days)\n"
        "Calls:\n"
    )
    budget = MAX_MESSAGE_LENGTH - len(_HEADER) - len(_footer()) - len(prefix)
    return prefix + _format_calls(operation, budget)


_HEADER = "⏳ *Timelock operations ready but not executed*\n\n"
_SEPARATOR = "\n\n"


def _footer() -> str:
    return (
        f"\n\nReady for more than {STALE_DAYS} days. A TimelockController operation never expires: "
        "it can still be executed until it is cancelled."
    )


def chunk_entries(entries: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Group (cache_key, text) entries into messages that fit Telegram's limit.

    send_telegram_message truncates an oversized message and still succeeds, so
    an operation in a cut-off tail would be cached as alerted without being sent.
    """
    budget = MAX_MESSAGE_LENGTH - len(_HEADER) - len(_footer())
    chunks: list[list[tuple[str, str]]] = []
    size = 0
    for entry in entries:
        if len(entry[1]) > budget:
            raise ValueError(f"Stale-operation alert {entry[0]} exceeds the {budget}-character entry budget")
        added = len(entry[1]) + (len(_SEPARATOR) if chunks and chunks[-1] else 0)
        if not chunks or (chunks[-1] and size + added > budget):
            chunks.append([])
            size = 0
            added = len(entry[1])
        chunks[-1].append(entry)
        size += added
    return chunks


def _send(protocol: str, message: str) -> bool:
    """Send to the protocol's channel (and mirror Yearn internally); True when the protocol send landed."""
    try:
        send_telegram_message(
            message, protocol, disable_notification=True, origin_protocol=alert_history_protocol(protocol)
        )
    except Exception:
        logger.exception("Failed to send stale-operation alert for %s", protocol)
        return False
    if protocol == "YEARN_TIMELOCK":
        # Mirror to the internal-only chat, as timelock_alerts.py does for every Yearn timelock alert.
        try:
            send_telegram_message(message, YEARN_TIMELOCK_INTERNAL_PROTOCOL, disable_notification=True)
        except Exception:
            logger.exception("Failed to mirror stale-operation alert to %s", YEARN_TIMELOCK_INTERNAL_PROTOCOL)
    return True


def main() -> None:
    now = int(time.time())
    operations = load_scheduled_operations(now - LOOKBACK_DAYS * DAY)
    if operations is None:
        send_envio_error_message("⚠️ Timelock stale operations: Envio query failed", "timelock")
        return
    stale = stale_operations(operations, ready_times(operations), now)
    logger.info("%s operations scheduled in %s days, %s stale", len(operations), LOOKBACK_DAYS, len(stale))

    by_protocol: dict[str, list[tuple[str, str]]] = {}
    for operation, ready_at in stale:
        key = cache_key(operation)
        if str(get_last_value_for_key_from_file(cache_filename, key)) == "1":
            continue
        by_protocol.setdefault(operation.timelock.protocol, []).append(
            (key, format_operation(operation, ready_at, now))
        )

    for protocol, entries in by_protocol.items():
        for chunk in chunk_entries(entries):
            message = _HEADER + _SEPARATOR.join(text for _, text in chunk) + _footer()
            if not _send(protocol, message):
                continue
            # Cache only what this message carried, after it landed.
            for key, _ in chunk:
                write_last_value_to_file(cache_filename, key, 1)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Multi-protocol script with per-timelock routing; crash alerts go to the general ops channel.
    run_with_alert(main, "yearn")
