#!/usr/bin/env python3
"""Monitor all TimelockEvent types and send Telegram alerts."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from dotenv import load_dotenv
from eth_utils import to_checksum_address

from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.calldata.decoder import decode_calldata, format_call_lines
from utils.chains import EXPLORER_URLS, Chain
from utils.llm.ai_explainer import explain_batch_transaction, explain_transaction, format_explanation_line
from utils.logger import get_logger
from utils.proxy import build_diff_url, detect_proxy_upgrade, get_current_implementation
from utils.safe_tx import unwrap_safe_exec_transaction
from utils.telegram import MAX_MESSAGE_LENGTH, escape_markdown, send_error_message, send_telegram_message
from utils.web3_wrapper import ChainManager

load_dotenv()

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")
DEFAULT_LOG_LEVEL = os.getenv("TIMELOCK_ALERTS_LOG_LEVEL", "INFO")
CACHE_KEY = "TIMELOCK_LAST_TS"

# YEARN_TIMELOCK alerts are also mirrored to this internal-only chat, in lockstep
# with the public topic. Configure its credentials with
# TELEGRAM_BOT_TOKEN_YEARN_TIMELOCK_INTERNAL / TELEGRAM_CHAT_ID_YEARN_TIMELOCK_INTERNAL.
YEARN_TIMELOCK_INTERNAL_PROTOCOL = "YEARN_TIMELOCK_INTERNAL"


@dataclass(frozen=True)
class TimelockConfig:
    """Configuration for a monitored timelock contract."""

    address: str
    chain_id: int
    protocol: str
    label: str


# All monitored timelocks; address field must be lowercase
TIMELOCK_LIST: list[TimelockConfig] = [
    # Chain 1 - Mainnet
    TimelockConfig("0xd8236031d8279d82e615af2bfab5fc0127a329ab", 1, "CAP", "CAP TimelockController"),
    TimelockConfig("0x5d8a7dc9405f08f14541ba918c1bf7eb2dace556", 1, "RTOKEN", "ETH+ Timelock"),
    TimelockConfig("0x055e84e7fe8955e2781010b866f10ef6e1e77e59", 1, "LRT", "Lombard TimeLock"),
    TimelockConfig("0x9f26d4c958fd811a1f59b01b86be7dffc9d20761", 1, "LRT", "EtherFi Timelock"),
    TimelockConfig("0x49bd9989e31ad35b0a62c20be86335196a3135b1", 1, "LRT", "KelpDAO(rsETH) Timelock"),
    TimelockConfig("0x3d18480cc32b6ab3b833dcabd80e76cfd41c48a9", 1, "INFINIFI", "Infinifi Longtimelock"),
    TimelockConfig("0x4b174afbed7b98ba01f50e36109eee5e6d327c32", 1, "INFINIFI", "Infinifi Shorttimelock"),
    TimelockConfig("0x9aee0b04504cef83a65ac3f0e838d0593bcb2bc7", 1, "AAVE", "Aave Governance V3"),
    TimelockConfig("0x6d903f6003cca6255d85cca4d3b5e5146dc33925", 1, "COMP", "Compound Timelock"),
    TimelockConfig("0x2386dc45added673317ef068992f19421b481f4c", 1, "FLUID", "Fluid Timelock"),
    TimelockConfig("0x2e59a20f205bb85a89c53f1936454680651e618e", 1, "LIDO", "Lido Timelock"),
    TimelockConfig("0x2efff88747eb5a3ff00d4d8d0f0800e306c0426b", 1, "MAPLE", "Maple GovernorTimelock"),
    TimelockConfig("0xb2a3cf69c97afd4de7882e5fee120e4efc77b706", 1, "STRATA", "Strata 48h Timelock"),
    TimelockConfig("0x4f2682b78f37910704fb1aff29358a1da07e022d", 1, "STRATA", "Strata 24h Timelock"),
    TimelockConfig("0x1dccd4628d48a50c1a7adea3848bcc869f08f8c2", 1, "3JANE", "3Jane 24h TimelockController"),
    TimelockConfig("0x3d3c41419ab401cd25055e8f9421d7d96d887885", 1, "3JANE", "3Jane 7d TimelockController"),
    # Chain 8453 - Base
    TimelockConfig("0xf817cb3092179083c48c014688d98b72fb61464f", 8453, "LRT", "superOETH Timelock"),
    # Yearn Timelock (0x88Ba032be87d5EF1fbE87336B7090767F367BF73) - all chains
    TimelockConfig("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 1, "YEARN_TIMELOCK", "Yearn TimelockController"),
    TimelockConfig("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 8453, "YEARN_TIMELOCK", "Yearn TimelockController"),
    TimelockConfig("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 42161, "YEARN_TIMELOCK", "Yearn TimelockController"),
    TimelockConfig("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 137, "YEARN_TIMELOCK", "Yearn TimelockController"),
    TimelockConfig("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 747474, "YEARN_TIMELOCK", "Yearn TimelockController"),
    TimelockConfig("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 10, "YEARN_TIMELOCK", "Yearn TimelockController"),
]

# Lookup by (lowercase address, chain_id) to support same address on multiple chains
TIMELOCKS: dict[tuple[str, int], TimelockConfig] = {(t.address, t.chain_id): t for t in TIMELOCK_LIST}

# Protocols whose governance proposals are already monitored (and human-described)
# by a dedicated script (e.g. aave/proposals.py, compound/proposals.py). For these,
# the AI summary on the timelock execution is redundant, so we skip it.
SKIP_AI_SUMMARY_PROTOCOLS: frozenset[str] = frozenset({"AAVE", "COMP", "LIDO", "FLUID"})

_logger = get_logger("timelock_alerts")


def http_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> dict | None:
    """Make an HTTP request and return JSON response."""
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
                payload = json.loads(resp.read().decode("utf-8"))
                _logger.info("http_json status=%s", resp.status)
                return payload
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
    _logger.info("gql_request")
    payload = {"query": query, "variables": variables}
    return http_json(ENVIO_GRAPHQL_URL, method="POST", body=payload)


def format_delay(seconds: int) -> str:
    """Convert delay in seconds to human-readable format."""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes}m")
    if not parts:
        return f"{seconds}s"
    return " ".join(parts)


def load_events(limit: int, since_ts: int, timelocks: list[TimelockConfig] | None = None) -> dict | None:
    """Fetch TimelockEvent events from the Envio GraphQL API."""
    source = timelocks if timelocks is not None else TIMELOCK_LIST
    # Some Envio deployments store timelockAddress checksummed; include both
    # representations to avoid case-sensitive misses.
    addresses = sorted(
        {
            addr
            for t in source
            for addr in (
                t.address,
                to_checksum_address(t.address),
            )
        }
    )
    _logger.info("load_events limit=%s since_ts=%s addresses=%s", limit, since_ts, len(addresses))
    query = """
    query GetTimelockEvents($limit: Int!, $sinceTs: Int!, $addresses: [String!]!) {
      TimelockEvent(
        where: {
          timelockAddress: { _in: $addresses }
          blockTimestamp: { _gt: $sinceTs }
        }
        order_by: { blockTimestamp: asc, blockNumber: asc, logIndex: asc }
        limit: $limit
      ) {
        id
        timelockAddress
        timelockType
        eventName
        chainId
        blockNumber
        blockTimestamp
        transactionHash
        operationId
        index
        target
        value
        data
        predecessor
        delay
        signature
        creator
        metadata
        votesFor
        votesAgainst
      }
    }
    """
    variables: dict = {"limit": limit, "sinceTs": since_ts, "addresses": addresses}
    return gql_request(query, variables)


def _format_address(address: str, explorer: str | None, prefix: str = "") -> str:
    """Format an address with optional explorer link."""
    if explorer:
        return f"{prefix}[{address}]({explorer}/address/{address})"
    return f"{prefix}{address}"


def _format_delay_info(delay: int | None, timelock_type: str) -> str | None:
    """Format delay based on timelock type semantics."""
    if delay is None:
        return None

    delay_val = int(delay)
    if timelock_type in ("Compound", "Maple"):
        # Absolute timestamp
        relative = delay_val - int(time.time())
        if relative > 0:
            return f"⏳ Executable In: {format_delay(relative)}"
        return "⏳ Executable: Now"
    # Relative delay (TimelockController)
    return f"⏳ Delay: {format_delay(delay_val)}"


def _build_call_info(event: dict, explorer: str | None, show_index: bool, chain_id: int = 0) -> list[str]:
    """Build call info lines for TimelockController/Compound events."""
    lines: list[str] = []
    target = event.get("target")
    if not target:
        return lines

    if show_index and event.get("index") is not None:
        lines.append(f"--- Call {int(event['index'])} ---")

    lines.append(_format_address(target, explorer, "🎯 Target: "))

    # Prefer human-readable signature (Compound), fallback to selector
    signature = event.get("signature")
    data_hex = event.get("data") or "0x"
    if signature:
        lines.append(f"📝 Function: `{signature}`")
    elif len(data_hex) >= 10:
        lines.extend(format_call_lines(data_hex))

    # Proxy upgrade detection: show diff link between old and new implementation
    if len(data_hex) >= 10:
        upgrade = detect_proxy_upgrade(data_hex, target)
        if upgrade and chain_id:
            # For ProxyAdmin-routed upgrades, `target` is the ProxyAdmin contract;
            # the proxy being upgraded is inside the calldata. Surface it explicitly
            # so recipients know which contract is changing.
            if upgrade.proxy_address.lower() != target.lower():
                lines.append(f"🅿️ Proxy: `{upgrade.proxy_address}`")
            old_impl = get_current_implementation(upgrade.proxy_address, chain_id)
            new_impl = upgrade.new_implementation
            if old_impl:
                lines.append(f"🔄 Upgrade: `{old_impl}` → `{new_impl}`")
                diff_url = build_diff_url(old_impl, new_impl, chain_id)
                if diff_url:
                    lines.append(f"📊 [Diff]({diff_url})")
            else:
                lines.append(f"🔄 New impl: `{new_impl}`")

    value = event.get("value")
    if value and int(value) > 0:
        lines.append(f"💰 Value: {int(value) / 1e18:.4f} ETH")

    return lines


def _maple_proposal_calls(event: dict, chain_id: int) -> list[dict[str, str]] | None:
    """Recover the inner (target, data) pairs from a Maple ProposalScheduled event.

    The GovernorTimelock only stores a hash of the proposal calls on-chain, so the
    ProposalScheduled event itself has no target/data. The actual payload lives in
    the transaction that emitted the event — typically a Safe execTransaction wrapping
    a scheduleProposals(address[], bytes[]) call into the GovernorTimelock.

    Returns None if the tx can't be fetched or doesn't match the expected shape.
    """
    tx_hash = event.get("transactionHash")
    if not tx_hash:
        return None

    try:
        chain = Chain.from_chain_id(chain_id)
        client = ChainManager.get_client(chain)
        tx = client.eth.get_transaction(tx_hash)
    except Exception as e:  # noqa: BLE001
        _logger.info("Failed to fetch Maple proposal tx %s: %s", tx_hash, e)
        return None

    raw_input = tx.get("input")
    input_hex = raw_input.hex() if isinstance(raw_input, bytes) else str(raw_input or "")
    if input_hex and not input_hex.startswith("0x"):
        input_hex = "0x" + input_hex

    # Unwrap one layer of Safe execTransaction if present; otherwise decode directly.
    inner = unwrap_safe_exec_transaction(input_hex)
    inner_data = inner.data if inner else input_hex
    if not inner_data or len(inner_data) < 10:
        return None

    decoded = decode_calldata(inner_data)
    if not decoded or len(decoded.params) < 2:
        return None

    # Expect scheduleProposals(address[] targets, bytes[] data). Some Maple proposal
    # paths (proposeRoleUpdates etc.) don't carry concrete (target, data) tuples we
    # can hand to the explainer — bail out cleanly for those.
    targets_type, targets = decoded.params[0]
    data_type, datas = decoded.params[1]
    if targets_type != "address[]" or data_type != "bytes[]" or len(targets) != len(datas):
        return None

    def _to_hex(d: object) -> str:
        if isinstance(d, bytes):
            return "0x" + d.hex()
        s = str(d)
        return s if s.startswith("0x") else "0x" + s

    return [{"target": str(t), "data": _to_hex(d), "value": "0"} for t, d in zip(targets, datas)]


def _get_ai_explanation(events: list[dict], timelock_info: TimelockConfig, chain_id: int) -> str | None:
    """Generate AI explanation for timelock events. Returns None on any failure."""
    try:
        # Maple's ProposalScheduled event only carries an opaque proposalId — the
        # targets/data must be recovered from the originating transaction.
        if events and events[0].get("timelockType") == "Maple":
            calls = _maple_proposal_calls(events[0], chain_id)
            if not calls:
                return None
            return explain_batch_transaction(
                calls=calls,
                chain_id=chain_id,
                protocol=timelock_info.protocol,
                label=timelock_info.label,
                from_address=timelock_info.address,
                refine=True,
            )

        calls_with_data = [e for e in events if e.get("target") and e.get("data") and len(e.get("data", "")) >= 10]
        if not calls_with_data:
            return None

        if len(calls_with_data) == 1:
            event = calls_with_data[0]
            return explain_transaction(
                target=event["target"],
                calldata=event["data"],
                chain_id=chain_id,
                value=int(event.get("value", 0)),
                protocol=timelock_info.protocol,
                label=timelock_info.label,
                from_address=timelock_info.address,
                refine=True,
            )

        # Batch transaction
        calls = [{"target": e["target"], "data": e["data"], "value": str(e.get("value", 0))} for e in calls_with_data]
        return explain_batch_transaction(
            calls=calls,
            chain_id=chain_id,
            protocol=timelock_info.protocol,
            label=timelock_info.label,
            from_address=timelock_info.address,
            refine=True,
        )
    except Exception:
        _logger.warning("AI explanation failed", exc_info=True)
        return None


def _truncate_call_lines(call_lines: list[str], budget: int) -> str:
    """Join ``call_lines`` to fit within ``budget`` chars, dropping whole trailing lines.

    Slicing the joined text mid-line can sever a Markdown entity — e.g. cut an
    opening backtick in a `signature` before its closing one — which Telegram
    rejects with a 400 "can't parse entities". A failed send then blocks the
    caller's dedupe cursor and wedges the monitor into a re-send loop. Dropping
    whole lines keeps every entity balanced.
    """
    marker = "... (truncated)"
    kept: list[str] = []
    used = 0
    for line in call_lines:
        added = len(line) + (1 if kept else 0)  # +1 for the joining newline
        if used + added > budget:
            break
        kept.append(line)
        used += added

    if len(kept) == len(call_lines):
        return "\n".join(kept)

    # Make room for the truncation marker by dropping more whole lines if needed.
    while kept:
        extra = 1 if kept else 0  # newline only required when joining to existing lines
        if used + len(marker) + extra <= budget:
            break
        dropped = kept.pop()
        used -= len(dropped) + (1 if kept else 0)

    # Only append the marker when it fits within the budget; otherwise return an
    # empty call-details section rather than exceeding the caller's allowance.
    if kept and used + len(marker) + (1 if kept else 0) <= budget:
        kept.append(marker)

    return "\n".join(kept)


def build_alert_message(events: list[dict], timelock_info: TimelockConfig) -> str:
    """Build a Telegram alert message for a group of TimelockEvent events (same operationId).

    Priority order when message exceeds Telegram limit:
    header > AI summary > footer > call details (truncated first).
    """
    first = events[0]
    chain_id = int(first["chainId"])
    try:
        chain_name = Chain.from_chain_id(chain_id).network_name.capitalize()
    except ValueError:
        chain_name = f"Chain {chain_id}"

    explorer = EXPLORER_URLS.get(chain_id)
    tx_hash = first["transactionHash"]
    timelock_type = first.get("timelockType", "Unknown")

    # Header (always included). Escape protocol and label since they're
    # config-supplied and may contain Markdown-V1 specials — e.g. the
    # underscore in "YEARN_TIMELOCK" was opening an italic that never
    # closed, breaking the whole message with Telegram 400.
    header_lines: list[str] = [
        "⏰ *TIMELOCK: New Operation Scheduled*",
        f"🅿️ Protocol: {escape_markdown(timelock_info.protocol)}",
        _format_address(first["timelockAddress"], explorer, f"📋 {escape_markdown(timelock_info.label)}: "),
        f"🔗 Chain: {chain_name}",
    ]

    # Delay (if applicable)
    delay_line = _format_delay_info(first.get("delay"), timelock_type)
    if delay_line:
        header_lines.append(delay_line)

    # Type-specific call details (truncated first when message is too long)
    call_lines: list[str] = []
    if timelock_type == "Aave":
        votes_for = first.get("votesFor")
        votes_against = first.get("votesAgainst")
        if votes_for is not None:
            call_lines.append(f"✅ Votes For: {votes_for}")
        if votes_against is not None:
            call_lines.append(f"❌ Votes Against: {votes_against}")
        call_lines.append(f"🆔 Proposal: {first.get('operationId') or ''}")

    elif timelock_type == "Lido":
        creator = first.get("creator")
        if creator:
            call_lines.append(_format_address(creator, explorer, "👤 Creator: "))
        metadata = first.get("metadata")
        if metadata:
            call_lines.append(f"📄 Metadata: {metadata}")
        call_lines.append(f"🆔 Vote: {first.get('operationId') or ''}")

    elif timelock_type == "Maple":
        call_lines.append(f"🆔 Proposal: {first.get('operationId') or ''}")

    elif timelock_type in ("TimelockController", "Compound"):
        for event in events:
            call_lines.extend(_build_call_info(event, explorer, len(events) > 1, chain_id))

    else:
        # Unknown type - show operationId at minimum
        call_lines.append(f"🆔 Operation: {first.get('operationId') or ''}")

    # AI explanation (best-effort, non-blocking). Skipped for protocols whose
    # governance proposals are already monitored by a dedicated script.
    ai_line = ""
    if timelock_info.protocol.upper() not in SKIP_AI_SUMMARY_PROTOCOLS:
        explanation = _get_ai_explanation(events, timelock_info, chain_id)
        if explanation:
            ai_line = format_explanation_line(explanation)

    # Footer (always included)
    if explorer:
        footer = f"🔗 Tx: [{tx_hash}]({explorer}/tx/{tx_hash})"
    else:
        footer = f"🔗 Tx: {tx_hash}"

    # Assemble with priority: header > AI summary > footer > call details
    header_text = "\n".join(header_lines)
    fixed_len = len(header_text) + len(ai_line) + len(footer) + 3  # +3 for joining newlines between parts
    budget = MAX_MESSAGE_LENGTH - fixed_len

    if budget > 0:
        call_text = "\n".join(call_lines)
        if len(call_text) > budget:
            call_text = _truncate_call_lines(call_lines, budget)
    else:
        call_text = ""

    parts = [header_text]
    if call_text:
        parts.append(call_text)
    if ai_line:
        parts.append(ai_line)
    parts.append(footer)

    return "\n".join(parts)


def process_events(events: list[dict], use_cache: bool) -> None:
    """Process TimelockEvent events, group by operationId, and send alerts."""
    if not events:
        _logger.info("No new events to process")
        return

    # Group events: only TimelockController has batch operations (multiple
    # CallScheduled events sharing the same operationId). All other types
    # emit one event per operation, so each is its own group.
    # Key includes chainId because operationId is content-derived
    # (keccak of targets/values/data/predecessor/salt) — when the same
    # address (e.g. Yearn TimelockController) lives on multiple chains, an
    # identical payload scheduled on two of them collides on operationId
    # and only the first chain's alert would fire.
    operations: dict[str, list[dict]] = {}
    for event in events:
        if event.get("timelockType") == "TimelockController":
            key = f"{event['chainId']}:{event['operationId']}"
        else:
            key = event["id"]
        if key not in operations:
            operations[key] = []
        operations[key].append(event)

    _logger.info("Processing %s operations from %s events", len(operations), len(events))

    messages_by_protocol: dict[str, list[str]] = {}
    max_timestamp = 0

    for op_id, op_events in operations.items():
        # Events are already ordered by logIndex from the GraphQL query
        # so call order within batch operations is preserved

        timelock_addr = op_events[0]["timelockAddress"].lower()
        chain_id = int(op_events[0]["chainId"])
        timelock_info = TIMELOCKS.get((timelock_addr, chain_id))
        if not timelock_info:
            _logger.warning("Unknown timelock address: %s", timelock_addr)
            continue

        protocol = timelock_info.protocol
        messages_by_protocol.setdefault(protocol, []).append(build_alert_message(op_events, timelock_info))

        # Track max timestamp
        for event in op_events:
            ts = int(event["blockTimestamp"])
            if ts > max_timestamp:
                max_timestamp = ts

    # Mirror all Yearn timelock alerts to the internal-only chat: the send loop
    # below delivers the same messages to both protocols.
    if "YEARN_TIMELOCK" in messages_by_protocol:
        messages_by_protocol[YEARN_TIMELOCK_INTERNAL_PROTOCOL] = list(messages_by_protocol["YEARN_TIMELOCK"])

    # Send alerts grouped by protocol, splitting into chunks that fit Telegram's limit
    separator = "\n\n---\n\n"
    all_sent = True
    for protocol, messages in messages_by_protocol.items():
        chunks: list[str] = []
        current_parts: list[str] = []
        current_len = 0

        for msg in messages:
            added_len = len(msg) + (len(separator) if current_parts else 0)
            if current_parts and current_len + added_len > MAX_MESSAGE_LENGTH:
                chunks.append(separator.join(current_parts))
                current_parts = [msg]
                current_len = len(msg)
            else:
                current_parts.append(msg)
                current_len += added_len

        if current_parts:
            chunks.append(separator.join(current_parts))

        for chunk in chunks:
            try:
                send_telegram_message(chunk, protocol)
            except Exception:
                _logger.exception("Failed to send Telegram alert for protocol %s", protocol)
                all_sent = False

    # Only advance the cache when every chunk landed. Advancing on partial
    # failure silently drops the failed events — the next run sees no new
    # events past the new timestamp and the alerts are lost forever. Risk of
    # duplicate alerts on retry is acceptable; missing alerts is not.
    if use_cache and max_timestamp > 0:
        if all_sent:
            write_last_value_to_file(cache_filename, CACHE_KEY, str(max_timestamp))
            _logger.info("Updated cache: %s = %s", CACHE_KEY, max_timestamp)
        else:
            _logger.warning(
                "Skipping cache update due to Telegram send failure(s); %s events will be re-fetched on the next run",
                len(events),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert on all TimelockEvent types.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--since-seconds",
        type=int,
        default=43200,
        help="Fallback lookback window in seconds when no cache exists (default: 12h)",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable caching of last processed timestamp")
    parser.add_argument(
        "--protocol",
        type=str,
        default="",
        help="Filter to a specific protocol (e.g. MAPLE, AAVE). Case-insensitive.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=DEFAULT_LOG_LEVEL,
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args()
    _logger.setLevel(args.log_level.upper())

    # Filter timelocks by protocol if specified
    filtered_timelocks: list[TimelockConfig] | None = None
    if args.protocol:
        protocol_filter = args.protocol.upper()
        filtered_timelocks = [t for t in TIMELOCK_LIST if t.protocol.upper() == protocol_filter]
        if not filtered_timelocks:
            _logger.error("No timelocks found for protocol: %s", args.protocol)
            sys.exit(1)
        _logger.info("Filtering to protocol %s: %s timelocks", protocol_filter, len(filtered_timelocks))

    use_cache = not args.no_cache

    # Determine the starting timestamp
    since_ts = 0
    if use_cache:
        cached_ts = get_last_value_for_key_from_file(cache_filename, CACHE_KEY)
        if cached_ts and str(cached_ts) != "0":
            since_ts = int(cached_ts)
            _logger.info("Using cached timestamp: %s", since_ts)

    if since_ts == 0:
        since_ts = args.since_seconds or 24 * 60 * 60
        since_ts = int(time.time()) - since_ts
        _logger.info("No cached timestamp, using fallback: %s", since_ts)

    _logger.info("Fetching TimelockEvent events since timestamp %s", since_ts)

    response = load_events(args.limit, since_ts, filtered_timelocks)
    if response is None:
        msg = "⚠️ Timelock alerts: Envio API is unreachable after 3 retries"
        _logger.error(msg)
        try:
            send_error_message(msg, "timelock")
        except Exception:
            _logger.exception("Failed to send Envio error alert")
        return
    if "errors" in response:
        msg = f"Timelock alerts: GraphQL errors: {response['errors']}"
        _logger.error(msg)
        send_error_message(msg, "timelock")
        return

    data = response.get("data", {})
    events = data.get("TimelockEvent", [])
    _logger.info("Fetched %s TimelockEvent events", len(events))

    process_events(events, use_cache)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Multi-protocol script with per-timelock routing; crash alerts go to the general ops channel.
    run_with_alert(main, "yearn")
