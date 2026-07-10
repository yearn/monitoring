"""
3Jane USD3/sUSD3 monitoring script.

3Jane is a credit-based money market on Ethereum built as a modified Morpho Blue fork.
USD3 is the senior tranche ERC-4626 vault backed by USDC deposits.
sUSD3 is the junior (first-loss) tranche created by staking USD3.

Monitors:
- PPS (Price Per Share) for USD3 and sUSD3 — alerts on any decrease
- TVL (Total Value Locked) via totalAssets() — alerts on >15% change
- Junior tranche buffer — alerts when sUSD3 coverage drops below threshold
- USD3 OC — alerts when senior-tranche overcollateralization drops below thresholds
- Insurance fund — alerts on waUSDC outflows of at least $50k
- Withdraw liquidity — alerts when USD3 availableWithdrawLimit falls below $4M

Threshold alerts (junior buffer, USD3 OC, withdraw liquidity) are deduped via
cache: the alerted value is stored and no new alert is sent until the value
drops below it; recovering above the threshold re-arms the alert.
- Vault shutdown status — alerts once if either vault enters emergency shutdown
- Debt cap changes — alerts when ProtocolConfig debt cap is modified
- Nominal sUSD3 backing floor — alerts on change and when floor > sUSD3 backing
- Protocol-wide pause — alerts once when ProtocolConfig IS_PAUSED flips to true
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from web3 import Web3

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_path, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.formatting import format_usd
from utils.logger import get_logger
from utils.telegram import escape_markdown
from utils.web3_wrapper import ChainManager

PROTOCOL = "3jane"
logger = get_logger(PROTOCOL)

CACHE_FILENAME = cache_path("cache-id.txt")

# --- ABIs ---
ABI_VAULT = load_abi("protocols/3jane/abi/ERC4626Vault.json")
ABI_PROTOCOL_CONFIG = load_abi("protocols/3jane/abi/ProtocolConfig.json")

# --- Contract Addresses ---
USD3_ADDRESS = "0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc"
SUSD3_ADDRESS = "0xf689555121e529Ff0463e191F9Bd9d1E496164a7"
PROTOCOL_CONFIG_ADDRESS = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"
WAUSDC_ADDRESS = "0xD4fa2D31b7968E448877f69A96DE69f5de8cD23E"
INSURANCE_FUND_ADDRESS = "0x4507B5B23340D248457d955a211C8B0634D29935"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# --- External data sources ---
ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")
ENVIO_PAGE_SIZE = int(os.getenv("THREE_JANE_ENVIO_PAGE_SIZE", "1000"))

# USDC has 6 decimals, USD3 and sUSD3 inherit this
DECIMALS = 6
ONE_SHARE = 10**DECIMALS
RATE_SCALE = 10**18
SECONDS_PER_DAY = 86_400

# --- Cache Keys ---
CACHE_KEY_USD3_PPS = "3JANE_USD3_PPS"
CACHE_KEY_SUSD3_PPS = "3JANE_SUSD3_PPS"
CACHE_KEY_USD3_TVL = "3JANE_USD3_TVL"
CACHE_KEY_SUSD3_TVL = "3JANE_SUSD3_TVL"
CACHE_KEY_SHUTDOWN_USD3 = "3JANE_SHUTDOWN_USD3"
CACHE_KEY_SHUTDOWN_SUSD3 = "3JANE_SHUTDOWN_SUSD3"
CACHE_KEY_DEBT_CAP = "3JANE_DEBT_CAP"
CACHE_KEY_NOMINAL_FLOOR = "3JANE_NOMINAL_FLOOR"
CACHE_KEY_FLOOR_BREACH = "3JANE_FLOOR_BREACH"
CACHE_KEY_IS_PAUSED = "3JANE_IS_PAUSED"
CACHE_KEY_INSURANCE_FUND_SHARES = "3JANE_INSURANCE_FUND_SHARES"
CACHE_KEY_BORROWER_DEFAULT_WATCH_PREFIX = "3JANE_BORROWER_DEFAULT_WATCH"
CACHE_KEY_JUNIOR_BUFFER_ALERTED = "3JANE_JUNIOR_BUFFER_ALERTED"
CACHE_KEY_USD3_OC_ALERTED = "3JANE_USD3_OC_ALERTED"
CACHE_KEY_WITHDRAW_LIMIT_ALERTED = "3JANE_WITHDRAW_LIMIT_ALERTED"

# --- ProtocolConfig keys (keccak256 of the string label) ---
CFG_KEY_SUSD3_NOMINAL_BACKING_FLOOR = Web3.keccak(text="SUSD3_NOMINAL_BACKING_FLOOR")
CFG_KEY_IS_PAUSED = Web3.keccak(text="IS_PAUSED")

# --- Thresholds ---
TVL_CHANGE_THRESHOLD = 0.15  # 15% TVL change alert
JUNIOR_BUFFER_THRESHOLD = 0.15  # Alert when sUSD3 backing < 15% of deployed credit
USD3_OC_HIGH_THRESHOLD = 1.11  # Alert when USD3 OC drops below the 111% target
USD3_OC_CRITICAL_THRESHOLD = 1.06  # Alert when USD3 OC drops below 106%
INSURANCE_FUND_OUTFLOW_THRESHOLD = 50_000  # USDC
WITHDRAW_LIMIT_THRESHOLD = 4_000_000  # USDC, alert when USD3 availableWithdrawLimit falls below

THREE_JANE_BORROWER_DEFAULT_WATCH_QUERY = """
query GetThreeJaneBorrowerDefaultWatch($limit: Int!, $offset: Int!) {
  ThreeJaneBorrowerMarket(
    where: { settled: { _neq: true } }
    order_by: { lastSeenBlock: asc }
    limit: $limit
    offset: $offset
  ) {
    id
    marketId
    borrower
    credit
    amountDue
    cycleId
    cycleEnd
    endingBalance
    gracePeriod
    delinquencyPeriod
    defaultAt
    defaultStarted
    settled
    lastSeenBlock
  }
}
"""


@dataclass(frozen=True)
class BorrowerRepaymentSnapshot:
    market_id: str
    borrower: str
    cycle_id: int
    cycle_end: int
    amount_due_raw: int
    ending_balance_raw: int
    credit_raw: int
    default_started: bool
    repayment_status: str
    default_at: int
    seconds_to_default: int
    seconds_since_default: int
    default_bucket: str | None


def get_cache_value(key: str) -> float:
    """Read a cached float value, returns 0.0 if not found."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def get_cache_int(key: str) -> int:
    """Read an integer cache value without passing it through float."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return int(val)
    except (ValueError, TypeError):
        # Accept values written by the previous implementation as "123.0".
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return 0


def set_cache_value(key: str, value: int | float) -> None:
    """Write a numeric value to cache."""
    write_last_value_to_file(CACHE_FILENAME, key, value)


def _get_alerted_value(cache_key: str) -> float:
    """Read the last alerted value for a threshold alert (-1 = none outstanding)."""
    raw_cached = get_last_value_for_key_from_file(CACHE_FILENAME, cache_key)
    try:
        return float(raw_cached) if isinstance(raw_cached, str) else -1.0
    except ValueError:
        return -1.0


def should_alert_value_drop(cache_key: str, value: float, threshold: float) -> bool:
    """Decide whether a lower-is-worse threshold alert should be sent.

    Deduped like the debt cap check: repeat runs stay silent until the metric
    drops below the last alerted value (worsens). Recovering to or above the
    threshold clears the cache so the next breach alerts again. The caller
    must record the value with mark_alerted_value() only after send_alert()
    returns, so a failed Telegram send retries on the next run.

    Args:
        cache_key: Cache key holding the last alerted value (-1 = none outstanding).
        value: Current metric value; lower is worse.
        threshold: Alert when the value drops below this.

    Returns:
        True when a new alert should be sent.
    """
    if value >= threshold:
        clear_alerted_value(cache_key)
        return False

    cached = _get_alerted_value(cache_key)
    return not 0 <= cached <= value


def mark_alerted_value(cache_key: str, value: float) -> None:
    """Record the value a threshold alert fired for; call after send_alert() returns."""
    set_cache_value(cache_key, value)


def clear_alerted_value(cache_key: str) -> None:
    """Clear an outstanding threshold alert so the next breach alerts again."""
    if _get_alerted_value(cache_key) >= 0:
        set_cache_value(cache_key, -1)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def _normalize_market_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    market_id = value.lower()
    if market_id.startswith("0x") and len(market_id) == 66:
        return market_id
    return None


def _normalize_borrower(value: Any) -> str | None:
    if not isinstance(value, str) or not Web3.is_address(value):
        return None
    return Web3.to_checksum_address(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_actionable_repayment_status(status: str) -> bool:
    return status in {"Delinquent", "Default"}


def current_unix_timestamp() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def select_default_watch_bucket(repayment_status: str, seconds_to_default: int) -> str | None:
    if repayment_status == "Default":
        return "default"
    if repayment_status != "Delinquent":
        return None
    if seconds_to_default <= SECONDS_PER_DAY:
        return "1d"
    if seconds_to_default <= 3 * SECONDS_PER_DAY:
        return "3d"
    if seconds_to_default <= 7 * SECONDS_PER_DAY:
        return "7d"
    if seconds_to_default <= 14 * SECONDS_PER_DAY:
        return "14d"
    return "delinquent"


def compute_default_watch_status(
    amount_due_raw: int,
    cycle_end: int,
    grace_period: int,
    delinquency_period: int,
    default_started: bool,
    now_timestamp: int,
) -> tuple[str, int, int, str | None] | None:
    if amount_due_raw <= 0 or cycle_end <= 0:
        return None

    grace_end = cycle_end + grace_period
    default_at = grace_end + delinquency_period
    seconds_to_default = default_at - now_timestamp

    if default_started or now_timestamp >= default_at:
        repayment_status = "Default"
        seconds_since_default = max(0, now_timestamp - default_at)
    elif now_timestamp > grace_end:
        repayment_status = "Delinquent"
        seconds_since_default = 0
    else:
        return None

    return (
        repayment_status,
        seconds_to_default,
        seconds_since_default,
        select_default_watch_bucket(repayment_status, seconds_to_default),
    )


def http_json(url: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST JSON and return parsed JSON, or None on transient/indexer errors."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("3Jane Envio request failed; skipping borrower default watch this run: %s", exc)
        return None


def gql_request(query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
    if not ENVIO_GRAPHQL_URL:
        logger.warning("ENVIO_GRAPHQL_URL is not set; skipping borrower default watch")
        return None
    return http_json(ENVIO_GRAPHQL_URL, {"query": query, "variables": variables})


def _extract_envio_borrower_default_watch_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = (
        data.get("ThreeJaneBorrowerMarket")
        or data.get("threeJaneBorrowerMarkets")
        or data.get("threeJaneBorrowerMarket")
    )
    return rows if isinstance(rows, list) else []


def parse_envio_borrower_default_watch_rows(
    rows: list[dict[str, Any]], now_timestamp: int | None = None
) -> list[BorrowerRepaymentSnapshot]:
    """Parse Envio 3Jane borrower rows and compute current default risk."""
    if now_timestamp is None:
        now_timestamp = current_unix_timestamp()

    parsed: list[BorrowerRepaymentSnapshot] = []
    seen: set[tuple[str, str]] = set()

    for row in rows:
        if not isinstance(row, dict) or _as_bool(row.get("settled")):
            continue
        market_id = _normalize_market_id(row.get("marketId"))
        borrower = _normalize_borrower(row.get("borrower") or row.get("onBehalf"))
        amount_due_raw = _as_int(row.get("amountDue"))
        cycle_end = _as_int(row.get("cycleEnd"))
        grace_period = _as_int(row.get("gracePeriod"), 7 * SECONDS_PER_DAY)
        delinquency_period = _as_int(row.get("delinquencyPeriod"), 23 * SECONDS_PER_DAY)
        default_started = _as_bool(row.get("defaultStarted"))
        default_watch_status = compute_default_watch_status(
            amount_due_raw,
            cycle_end,
            grace_period,
            delinquency_period,
            default_started,
            now_timestamp,
        )
        if market_id is None or borrower is None or default_watch_status is None:
            continue

        repayment_status, seconds_to_default, seconds_since_default, bucket = default_watch_status
        if bucket is None or not _is_actionable_repayment_status(repayment_status):
            continue
        key = (market_id, borrower.lower())
        if key in seen:
            continue
        seen.add(key)
        parsed.append(
            BorrowerRepaymentSnapshot(
                market_id=market_id,
                borrower=borrower,
                cycle_id=_as_int(row.get("cycleId")),
                cycle_end=cycle_end,
                amount_due_raw=amount_due_raw,
                ending_balance_raw=_as_int(row.get("endingBalance")),
                credit_raw=_as_int(row.get("credit")),
                default_started=default_started,
                repayment_status=repayment_status,
                default_at=cycle_end + grace_period + delinquency_period,
                seconds_to_default=seconds_to_default,
                seconds_since_default=seconds_since_default,
                default_bucket=bucket,
            )
        )

    return parsed


def load_borrower_default_watch_snapshots_from_envio() -> list[BorrowerRepaymentSnapshot]:
    """Load Envio 3Jane borrower rows and compute current default watch candidates."""
    snapshots: list[BorrowerRepaymentSnapshot] = []
    seen: set[tuple[str, str]] = set()
    offset = 0
    now_timestamp = current_unix_timestamp()

    while True:
        payload = gql_request(THREE_JANE_BORROWER_DEFAULT_WATCH_QUERY, {"limit": ENVIO_PAGE_SIZE, "offset": offset})
        if payload is None:
            return snapshots
        if payload.get("errors"):
            logger.warning("3Jane Envio GraphQL errors; skipping borrower default watch: %s", payload["errors"])
            return snapshots

        rows = _extract_envio_borrower_default_watch_rows(payload)
        page = parse_envio_borrower_default_watch_rows(rows, now_timestamp)
        for snapshot in page:
            key = (snapshot.market_id, snapshot.borrower.lower())
            if key not in seen:
                seen.add(key)
                snapshots.append(snapshot)

        if len(rows) < ENVIO_PAGE_SIZE:
            return snapshots
        offset += ENVIO_PAGE_SIZE


def format_utc_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    days = seconds // SECONDS_PER_DAY
    hours = (seconds % SECONDS_PER_DAY) // 3600
    minutes = (seconds % 3600) // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else f"{seconds}s"


def _borrower_default_cache_key(snapshot: BorrowerRepaymentSnapshot, bucket: str) -> str:
    return (
        f"{CACHE_KEY_BORROWER_DEFAULT_WATCH_PREFIX}:"
        f"{snapshot.market_id}:{snapshot.borrower.lower()}:"
        f"{snapshot.cycle_id}:{snapshot.default_at}:{bucket}"
    )


def _default_watch_bucket_was_sent(snapshot: BorrowerRepaymentSnapshot, bucket: str) -> bool:
    return str(get_last_value_for_key_from_file(CACHE_FILENAME, _borrower_default_cache_key(snapshot, bucket))) == "1"


def _mark_default_watch_bucket_sent(snapshot: BorrowerRepaymentSnapshot, bucket: str) -> None:
    write_last_value_to_file(CACHE_FILENAME, _borrower_default_cache_key(snapshot, bucket), 1)


def check_borrower_default_watch_snapshot(snapshot: BorrowerRepaymentSnapshot) -> None:
    """Send a MEDIUM-only borrower default countdown alert when a new bucket is reached."""
    bucket = snapshot.default_bucket
    if bucket is None or _default_watch_bucket_was_sent(snapshot, bucket):
        return

    amount_due = snapshot.amount_due_raw / ONE_SHARE
    ending_balance = snapshot.ending_balance_raw / ONE_SHARE
    credit = snapshot.credit_raw / ONE_SHARE
    time_left = format_duration(snapshot.seconds_to_default)
    time_since_default = format_duration(snapshot.seconds_since_default)
    default_at = format_utc_timestamp(snapshot.default_at)
    cycle_end = format_utc_timestamp(snapshot.cycle_end)
    default_timing_line = (
        f"⏳ Defaulted at: {default_at} ({time_since_default} ago)"
        if snapshot.repayment_status == "Default"
        else f"⏳ Default at: {default_at} ({time_left})"
    )

    message = (
        f"⚠️ *3Jane Borrower Default Watch*\n"
        f"📊 Status: {snapshot.repayment_status} ({bucket})\n"
        f"👤 Borrower: `{snapshot.borrower}`\n"
        f"🏦 Market: `{snapshot.market_id}`\n"
        f"💰 Amount due: {format_usd(amount_due)} | Ending balance: {format_usd(ending_balance)}\n"
        f"📏 Credit line: {format_usd(credit)}\n"
        f"🗓️ Cycle end: {cycle_end}\n"
        f"{default_timing_line}\n"
        f"🔗 [Borrower](https://etherscan.io/address/{snapshot.borrower})"
    )
    send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    _mark_default_watch_bucket_sent(snapshot, bucket)


def check_borrower_default_watch(_client, _protocol_config) -> None:  # type: ignore[no-untyped-def]
    """Alert on 3Jane borrower default buckets computed from Envio rows."""
    snapshots = load_borrower_default_watch_snapshots_from_envio()
    if not snapshots:
        return

    logger.info("3Jane borrower default watch — Envio alert candidates: %d", len(snapshots))
    for snapshot in snapshots:
        check_borrower_default_watch_snapshot(snapshot)


def check_pps(usd3_pps_float: float, susd3_pps_float: float) -> None:
    """Check Price Per Share for USD3 and sUSD3, alert on any decrease.

    A PPS decrease indicates loan markdowns, defaults, or losses being socialized
    through the vault. This is the most critical signal for 3Jane since loans are unsecured.

    Args:
        usd3_pps_float: Current USD3 price per share as a float.
        susd3_pps_float: Current sUSD3 price per share as a float.
    """
    # --- USD3 PPS ---
    previous_usd3_pps = get_cache_value(CACHE_KEY_USD3_PPS)
    logger.info("USD3 PPS: %.8f (previous: %.8f)", usd3_pps_float, previous_usd3_pps)

    if previous_usd3_pps > 0 and usd3_pps_float < previous_usd3_pps:
        decrease_pct = (previous_usd3_pps - usd3_pps_float) / previous_usd3_pps * 100
        message = (
            f"🚨 *3Jane USD3 PPS Decrease*\n"
            f"📉 PPS dropped from {previous_usd3_pps:.8f} to {usd3_pps_float:.8f}\n"
            f"📊 Decrease: {decrease_pct:.4f}%\n"
            f"⚠️ Possible loan markdown or default\n"
            f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))

    if usd3_pps_float != previous_usd3_pps:
        set_cache_value(CACHE_KEY_USD3_PPS, usd3_pps_float)

    # --- sUSD3 PPS ---
    previous_susd3_pps = get_cache_value(CACHE_KEY_SUSD3_PPS)
    logger.info("sUSD3 PPS: %.8f (previous: %.8f)", susd3_pps_float, previous_susd3_pps)

    if previous_susd3_pps > 0 and susd3_pps_float < previous_susd3_pps:
        decrease_pct = (previous_susd3_pps - susd3_pps_float) / previous_susd3_pps * 100
        message = (
            f"🚨 *3Jane sUSD3 PPS Decrease*\n"
            f"📉 PPS dropped from {previous_susd3_pps:.8f} to {susd3_pps_float:.8f}\n"
            f"📊 Decrease: {decrease_pct:.4f}%\n"
            f"⚠️ Junior tranche absorbing losses — first-loss buffer impacted\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    if susd3_pps_float != previous_susd3_pps:
        set_cache_value(CACHE_KEY_SUSD3_PPS, susd3_pps_float)


def check_tvl(usd3_tvl: float, susd3_tvl: float) -> None:
    """Check Total Value Locked for USD3 and sUSD3, alert on large changes.

    Significant TVL changes can indicate large deposits/withdrawals or
    protocol-level events that affect backing.

    Args:
        usd3_tvl: Current USD3 totalAssets in USDC terms.
        susd3_tvl: Current sUSD3 totalAssets in USD3 terms.
    """
    # --- USD3 TVL ---
    previous_usd3_tvl = get_cache_value(CACHE_KEY_USD3_TVL)
    logger.info("USD3 TVL: %s (previous: %s)", format_usd(usd3_tvl), format_usd(previous_usd3_tvl))

    if previous_usd3_tvl > 0:
        change_pct = abs(usd3_tvl - previous_usd3_tvl) / previous_usd3_tvl
        if change_pct >= TVL_CHANGE_THRESHOLD:
            direction = "increased" if usd3_tvl > previous_usd3_tvl else "decreased"
            message = (
                f"🚨 *3Jane USD3 TVL Change*\n"
                f"💰 TVL {direction} by {change_pct:.2%}\n"
                f"📊 {format_usd(previous_usd3_tvl)} → {format_usd(usd3_tvl)}\n"
                f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
            )
            send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))

    if usd3_tvl != previous_usd3_tvl:
        set_cache_value(CACHE_KEY_USD3_TVL, usd3_tvl)

    # --- sUSD3 TVL ---
    previous_susd3_tvl = get_cache_value(CACHE_KEY_SUSD3_TVL)
    logger.info("sUSD3 TVL: %s (previous: %s)", format_usd(susd3_tvl), format_usd(previous_susd3_tvl))

    if previous_susd3_tvl > 0:
        change_pct = abs(susd3_tvl - previous_susd3_tvl) / previous_susd3_tvl
        if change_pct >= TVL_CHANGE_THRESHOLD:
            direction = "increased" if susd3_tvl > previous_susd3_tvl else "decreased"
            message = (
                f"🚨 *3Jane sUSD3 TVL Change*\n"
                f"💰 TVL {direction} by {change_pct:.2%}\n"
                f"📊 {format_usd(previous_susd3_tvl)} → {format_usd(susd3_tvl)}\n"
                f"⚠️ Junior tranche buffer size changed significantly\n"
                f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
            )
            send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))

    if susd3_tvl != previous_susd3_tvl:
        set_cache_value(CACHE_KEY_SUSD3_TVL, susd3_tvl)


def check_junior_buffer(susd3_backing: float, deployed_credit: float) -> None:
    """Check if sUSD3 junior tranche provides adequate first-loss coverage.

    The sUSD3 junior tranche absorbs losses before the senior USD3 tranche.
    A thin buffer means USD3 holders are closer to bearing losses directly.
    This matches the protocol's backing metric: sUSD3 backing value divided by
    deployed credit. The caller supplies both values converted to USDC.
    Deduped via cache: re-alerts only when the ratio drops further.

    Args:
        susd3_backing: USD3 held by sUSD3, valued in USDC.
        deployed_credit: Borrowed waUSDC in the credit market, converted to USDC.
    """
    if deployed_credit <= 0:
        # No deployed credit means nothing at risk: clear any outstanding alert.
        clear_alerted_value(CACHE_KEY_JUNIOR_BUFFER_ALERTED)
        return

    buffer_ratio = susd3_backing / deployed_credit
    logger.info(
        "Junior buffer ratio: %.2f%% (sUSD3 backing: %s / deployed credit: %s)",
        buffer_ratio * 100,
        format_usd(susd3_backing),
        format_usd(deployed_credit),
    )

    if should_alert_value_drop(CACHE_KEY_JUNIOR_BUFFER_ALERTED, buffer_ratio, JUNIOR_BUFFER_THRESHOLD):
        message = (
            f"⚠️ *3Jane Junior Buffer Low*\n"
            f"📊 sUSD3 buffer: {buffer_ratio:.2%} of deployed credit\n"
            f"💰 sUSD3 backing: {format_usd(susd3_backing)} | Deployed: {format_usd(deployed_credit)}\n"
            f"⚠️ First-loss coverage is thin — USD3 holders at higher risk\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))
        mark_alerted_value(CACHE_KEY_JUNIOR_BUFFER_ALERTED, buffer_ratio)


def check_usd3_oc(susd3_backing: float, deployed_credit: float) -> None:
    """Check senior-tranche overcollateralization from sUSD3 subordination.

    USD3 OC is deployed credit divided by senior at-risk credit after sUSD3
    absorbs first losses: deployed / (deployed - sUSD3). Alert thresholds use
    the direct OC ratio, so 111% means OC is below 1.11x. Deduped via cache:
    re-alerts only when the ratio drops further (e.g. crossing into critical).

    Args:
        susd3_backing: USD3 held by sUSD3, valued in USDC.
        deployed_credit: Borrowed waUSDC in the credit market, converted to USDC.
    """
    if deployed_credit <= 0:
        # No deployed credit means nothing at risk: clear any outstanding alert.
        clear_alerted_value(CACHE_KEY_USD3_OC_ALERTED)
        return

    senior_at_risk = deployed_credit - susd3_backing
    if senior_at_risk <= 0:
        logger.info(
            "USD3 OC: fully covered by sUSD3 backing (sUSD3: %s / deployed credit: %s)",
            format_usd(susd3_backing),
            format_usd(deployed_credit),
        )
        # Fully covered counts as recovered: clear any outstanding OC alert.
        clear_alerted_value(CACHE_KEY_USD3_OC_ALERTED)
        return

    oc_ratio = deployed_credit / senior_at_risk
    oc_excess = oc_ratio - 1
    logger.info(
        "USD3 OC: %.2f%% (%.4fx; %.2f%% excess; deployed: %s / senior at-risk: %s)",
        oc_ratio * 100,
        oc_ratio,
        oc_excess * 100,
        format_usd(deployed_credit),
        format_usd(senior_at_risk),
    )

    if not should_alert_value_drop(CACHE_KEY_USD3_OC_ALERTED, oc_ratio, USD3_OC_HIGH_THRESHOLD):
        return

    if oc_ratio < USD3_OC_CRITICAL_THRESHOLD:
        severity = AlertSeverity.CRITICAL
        title = "3Jane USD3 OC Critical"
        threshold = USD3_OC_CRITICAL_THRESHOLD
    else:
        severity = AlertSeverity.HIGH
        title = "3Jane USD3 OC Low"
        threshold = USD3_OC_HIGH_THRESHOLD

    message = (
        f"🚨 *{title}*\n"
        f"📊 USD3 OC: {oc_ratio:.2%} ({oc_ratio:.4f}x; {oc_excess:.2%} excess)\n"
        f"💰 Deployed: {format_usd(deployed_credit)} | Senior at-risk: {format_usd(senior_at_risk)}\n"
        f"🛡️ sUSD3 subordination: {format_usd(susd3_backing)}\n"
        f"⚠️ Threshold: {threshold:.0%} OC\n"
        f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
    )
    send_alert(Alert(severity, message, PROTOCOL))
    mark_alerted_value(CACHE_KEY_USD3_OC_ALERTED, oc_ratio)


def check_insurance_fund(
    previous_shares: int,
    current_shares: int,
    current_assets: float,
    outflow_assets: float,
) -> None:
    """Alert when the insurance fund loses at least $50k of waUSDC shares.

    The raw share balance is cached so normal waUSDC appreciation cannot hide an
    outflow. The caller values both the current balance and share delta in USDC.
    """
    logger.info(
        "Insurance fund — balance: %s USDC, shares: %d (previous: %d)",
        format_usd(current_assets),
        current_shares,
        previous_shares,
    )

    if outflow_assets >= INSURANCE_FUND_OUTFLOW_THRESHOLD:
        message = (
            f"🚨 *3Jane Insurance Fund Outflow*\n"
            f"📉 Outflow: {format_usd(outflow_assets)}\n"
            f"💰 Remaining balance: {format_usd(current_assets)}\n"
            f"⚠️ First-loss insurance available for debt settlement decreased\n"
            f"🔗 [Insurance Fund](https://etherscan.io/address/{INSURANCE_FUND_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    if current_shares != previous_shares:
        set_cache_value(CACHE_KEY_INSURANCE_FUND_SHARES, current_shares)


def check_withdraw_limit(withdraw_limit: float) -> None:
    """Alert when USD3 withdraw liquidity drops below the safety threshold.

    availableWithdrawLimit is the USDC the USD3 vault can immediately honor for
    withdrawals. When it falls below the threshold, withdrawals may queue or
    stall, signalling a liquidity squeeze on the senior tranche.
    Deduped via cache: re-alerts only when the limit drops further.

    Args:
        withdraw_limit: USD3 availableWithdrawLimit in USDC.
    """
    logger.info("USD3 available withdraw limit: %s", format_usd(withdraw_limit))

    if should_alert_value_drop(CACHE_KEY_WITHDRAW_LIMIT_ALERTED, withdraw_limit, WITHDRAW_LIMIT_THRESHOLD):
        message = (
            f"🚨 *3Jane USD3 Withdraw Liquidity Low*\n"
            f"📉 Available withdraw limit: {format_usd(withdraw_limit)} "
            f"(threshold {format_usd(WITHDRAW_LIMIT_THRESHOLD)})\n"
            f"⚠️ Senior-tranche withdrawals may queue or stall\n"
            f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
        mark_alerted_value(CACHE_KEY_WITHDRAW_LIMIT_ALERTED, withdraw_limit)


def check_vault_shutdown(client, usd3_vault, susd3_vault) -> None:  # type: ignore[no-untyped-def]
    """Check if either vault has been emergency shut down.

    Uses alert-once pattern: only sends alert when shutdown state transitions
    from False to True.

    Args:
        client: Web3Client instance.
        usd3_vault: USD3 contract instance.
        susd3_vault: sUSD3 contract instance.
    """
    with client.batch_requests() as batch:
        batch.add(usd3_vault.functions.isShutdown())
        batch.add(susd3_vault.functions.isShutdown())
        responses = client.execute_batch(batch)
        if len(responses) != 2:
            raise ValueError(f"Expected 2 responses, got {len(responses)}")

    usd3_shutdown = responses[0]
    susd3_shutdown = responses[1]

    logger.info("Vault shutdown — USD3: %s, sUSD3: %s", usd3_shutdown, susd3_shutdown)

    # Alert once on USD3 shutdown
    previous_usd3_shutdown = get_cache_value(CACHE_KEY_SHUTDOWN_USD3)
    if usd3_shutdown and previous_usd3_shutdown == 0:
        message = (
            f"🚨 *3Jane USD3 Vault SHUTDOWN*\n"
            f"⚠️ USD3 vault has entered emergency shutdown\n"
            f"🔗 [USD3](https://etherscan.io/address/{USD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))
    if float(usd3_shutdown) != previous_usd3_shutdown:
        set_cache_value(CACHE_KEY_SHUTDOWN_USD3, float(usd3_shutdown))

    # Alert once on sUSD3 shutdown
    previous_susd3_shutdown = get_cache_value(CACHE_KEY_SHUTDOWN_SUSD3)
    if susd3_shutdown and previous_susd3_shutdown == 0:
        message = (
            f"🚨 *3Jane sUSD3 Vault SHUTDOWN*\n"
            f"⚠️ sUSD3 vault has entered emergency shutdown\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))
    if float(susd3_shutdown) != previous_susd3_shutdown:
        set_cache_value(CACHE_KEY_SHUTDOWN_SUSD3, float(susd3_shutdown))


def check_debt_cap(client) -> None:  # type: ignore[no-untyped-def]
    """Check ProtocolConfig debt cap for changes.

    The debt cap limits how much can be borrowed via unsecured credit lines.
    Changes to the debt cap signal governance decisions to scale the protocol
    up or down.

    Args:
        client: Web3Client instance.
    """
    config = client.eth.contract(address=PROTOCOL_CONFIG_ADDRESS, abi=ABI_PROTOCOL_CONFIG)
    debt_cap_raw = client.execute(config.functions.getDebtCap().call)
    debt_cap = debt_cap_raw / ONE_SHARE

    previous_debt_cap = get_cache_value(CACHE_KEY_DEBT_CAP)
    logger.info("Debt cap: %s (previous: %s)", format_usd(debt_cap), format_usd(previous_debt_cap))

    if previous_debt_cap > 0 and debt_cap != previous_debt_cap:
        direction = "increased" if debt_cap > previous_debt_cap else "decreased"
        message = (
            f"⚠️ *3Jane Debt Cap Change*\n"
            f"📊 Debt cap {direction}\n"
            f"💰 {format_usd(previous_debt_cap)} → {format_usd(debt_cap)}\n"
            f"🔗 [ProtocolConfig](https://etherscan.io/address/{PROTOCOL_CONFIG_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))

    if debt_cap != previous_debt_cap:
        set_cache_value(CACHE_KEY_DEBT_CAP, debt_cap)


def check_nominal_backing_floor(nominal_floor: float, susd3_backing: float) -> None:
    """Check ProtocolConfig SUSD3_NOMINAL_BACKING_FLOOR.

    The nominal floor is an absolute USDC amount of sUSD3 backing the protocol
    requires (in addition to the ratio-based floor). When set above current
    sUSD3 backing valued in USDC, sUSD3 redemptions can be blocked.

    Sends two distinct alerts:
    - Any change to the floor value (governance lever).
    - Transition from "floor <= backing" to "floor > backing" (active breach).

    Args:
        nominal_floor: Current SUSD3_NOMINAL_BACKING_FLOOR in USDC.
        susd3_backing: Current sUSD3 backing value in USDC.
    """
    # --- Alert on any change (treat first-run as a non-alert init) ---
    raw_previous = get_last_value_for_key_from_file(CACHE_FILENAME, CACHE_KEY_NOMINAL_FLOOR)
    first_run = not isinstance(raw_previous, str)
    previous_floor = float(raw_previous) if isinstance(raw_previous, str) else 0.0

    logger.info(
        "sUSD3 nominal backing floor: %s (previous: %s)",
        format_usd(nominal_floor),
        format_usd(previous_floor),
    )

    if not first_run and nominal_floor != previous_floor:
        direction = "increased" if nominal_floor > previous_floor else "decreased"
        message = (
            f"⚠️ *3Jane sUSD3 Nominal Backing Floor Change*\n"
            f"📊 Floor {direction}\n"
            f"💰 {format_usd(previous_floor)} → {format_usd(nominal_floor)}\n"
            f"ℹ️ Withdrawals blocked while sUSD3 backing < floor\n"
            f"🔗 [ProtocolConfig](https://etherscan.io/address/{PROTOCOL_CONFIG_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    if nominal_floor != previous_floor or first_run:
        set_cache_value(CACHE_KEY_NOMINAL_FLOOR, nominal_floor)

    # --- Alert-once on breach transition (floor > backing) ---
    breach = nominal_floor > susd3_backing and nominal_floor > 0
    previous_breach = get_cache_value(CACHE_KEY_FLOOR_BREACH)
    if breach and previous_breach == 0:
        shortfall = nominal_floor - susd3_backing
        message = (
            f"🚨 *3Jane sUSD3 Backing Below Nominal Floor*\n"
            f"📊 Floor: {format_usd(nominal_floor)} | sUSD3 backing: {format_usd(susd3_backing)}\n"
            f"💰 Shortfall: {format_usd(shortfall)}\n"
            f"⚠️ sUSD3 redemptions may be blocked until backing recovers\n"
            f"🔗 [sUSD3](https://etherscan.io/address/{SUSD3_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    if float(breach) != previous_breach:
        set_cache_value(CACHE_KEY_FLOOR_BREACH, float(breach))


def check_protocol_paused(is_paused: bool) -> None:
    """Check ProtocolConfig IS_PAUSED flag.

    Separate from per-vault isShutdown(). A protocol-wide pause stops the
    underlying credit market regardless of vault shutdown state.

    Args:
        is_paused: Current IS_PAUSED value from ProtocolConfig.
    """
    logger.info("Protocol IS_PAUSED: %s", is_paused)

    previous_paused = get_cache_value(CACHE_KEY_IS_PAUSED)
    if is_paused and previous_paused == 0:
        message = (
            f"🚨 *3Jane Protocol PAUSED*\n"
            f"⚠️ ProtocolConfig IS_PAUSED flipped to true\n"
            f"🔗 [ProtocolConfig](https://etherscan.io/address/{PROTOCOL_CONFIG_ADDRESS})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))
    if float(is_paused) != previous_paused:
        set_cache_value(CACHE_KEY_IS_PAUSED, float(is_paused))


def main() -> None:
    """Run all 3Jane monitoring checks."""
    logger.info("Starting 3Jane monitoring...")

    client = ChainManager.get_client(Chain.MAINNET)
    usd3_vault = client.eth.contract(address=USD3_ADDRESS, abi=ABI_VAULT)
    susd3_vault = client.eth.contract(address=SUSD3_ADDRESS, abi=ABI_VAULT)
    wausdc_vault = client.eth.contract(address=WAUSDC_ADDRESS, abi=ABI_VAULT)
    protocol_config = client.eth.contract(address=PROTOCOL_CONFIG_ADDRESS, abi=ABI_PROTOCOL_CONFIG)

    try:
        # Batch all core vault reads in a single RPC call
        with client.batch_requests() as batch:
            batch.add(usd3_vault.functions.totalAssets())
            batch.add(usd3_vault.functions.totalSupply())
            batch.add(usd3_vault.functions.convertToAssets(ONE_SHARE))
            batch.add(susd3_vault.functions.totalAssets())
            batch.add(susd3_vault.functions.totalSupply())
            batch.add(susd3_vault.functions.convertToAssets(ONE_SHARE))
            batch.add(usd3_vault.functions.balanceOf(SUSD3_ADDRESS))
            batch.add(usd3_vault.functions.getMarketLiquidity())
            batch.add(protocol_config.functions.config(CFG_KEY_SUSD3_NOMINAL_BACKING_FLOOR))
            batch.add(protocol_config.functions.config(CFG_KEY_IS_PAUSED))
            batch.add(wausdc_vault.functions.balanceOf(INSURANCE_FUND_ADDRESS))
            batch.add(usd3_vault.functions.availableWithdrawLimit(ZERO_ADDRESS))
            responses = client.execute_batch(batch)
            if len(responses) != 12:
                raise ValueError(f"Expected 12 responses, got {len(responses)}")

        usd3_total_assets = responses[0]
        usd3_total_supply = responses[1]
        usd3_pps_raw = responses[2]
        susd3_total_assets = responses[3]
        susd3_total_supply = responses[4]
        susd3_pps_raw = responses[5]
        susd3_usd3_balance = responses[6]
        market_liquidity = responses[7]
        nominal_floor_raw = responses[8]
        is_paused = bool(responses[9])
        insurance_fund_shares = responses[10]
        withdraw_limit_raw = responses[11]

        if len(market_liquidity) != 4:
            raise ValueError(f"Expected 4 market liquidity values, got {len(market_liquidity)}")
        total_borrow_wausdc = market_liquidity[2]
        previous_insurance_shares = get_cache_int(CACHE_KEY_INSURANCE_FUND_SHARES)
        insurance_outflow_shares = max(previous_insurance_shares - insurance_fund_shares, 0)

        # Value the USD3 shares held by sUSD3 and fetch one high-precision waUSDC
        # conversion rate. All waUSDC values below use that same rate and block.
        with client.batch_requests() as batch:
            batch.add(usd3_vault.functions.convertToAssets(susd3_usd3_balance))
            batch.add(wausdc_vault.functions.convertToAssets(RATE_SCALE))
            backing_responses = client.execute_batch(batch)
            if len(backing_responses) != 2:
                raise ValueError(f"Expected 2 backing responses, got {len(backing_responses)}")

        susd3_backing_raw = backing_responses[0]
        wausdc_assets_per_scale = backing_responses[1]
        deployed_credit_raw = total_borrow_wausdc * wausdc_assets_per_scale // RATE_SCALE
        insurance_fund_assets_raw = insurance_fund_shares * wausdc_assets_per_scale // RATE_SCALE
        insurance_outflow_assets_raw = insurance_outflow_shares * wausdc_assets_per_scale // RATE_SCALE

        # Convert to human-readable floats
        usd3_tvl = usd3_total_assets / ONE_SHARE
        usd3_supply = usd3_total_supply / ONE_SHARE
        usd3_pps = usd3_pps_raw / ONE_SHARE
        susd3_tvl = susd3_total_assets / ONE_SHARE
        susd3_supply = susd3_total_supply / ONE_SHARE
        susd3_pps = susd3_pps_raw / ONE_SHARE
        susd3_backing = susd3_backing_raw / ONE_SHARE
        deployed_credit = deployed_credit_raw / ONE_SHARE
        insurance_fund_assets = insurance_fund_assets_raw / ONE_SHARE
        insurance_outflow_assets = insurance_outflow_assets_raw / ONE_SHARE
        withdraw_limit = withdraw_limit_raw / ONE_SHARE
        nominal_floor = nominal_floor_raw / ONE_SHARE

        logger.info(
            "USD3 — TVL: %s, Supply: %s, PPS: %.8f",
            format_usd(usd3_tvl),
            format_usd(usd3_supply),
            usd3_pps,
        )
        logger.info(
            "sUSD3 — TVL: %s USD3, Supply: %s, PPS: %.8f",
            format_usd(susd3_tvl),
            format_usd(susd3_supply),
            susd3_pps,
        )
        logger.info(
            "Junior backing — sUSD3: %s USDC, deployed credit: %s USDC",
            format_usd(susd3_backing),
            format_usd(deployed_credit),
        )

        # Run all checks
        check_pps(usd3_pps, susd3_pps)
        check_tvl(usd3_tvl, susd3_tvl)
        check_junior_buffer(susd3_backing, deployed_credit)
        check_usd3_oc(susd3_backing, deployed_credit)
        check_insurance_fund(
            previous_insurance_shares,
            insurance_fund_shares,
            insurance_fund_assets,
            insurance_outflow_assets,
        )
        check_withdraw_limit(withdraw_limit)
        check_vault_shutdown(client, usd3_vault, susd3_vault)
        check_debt_cap(client)
        check_nominal_backing_floor(nominal_floor, susd3_backing)
        check_protocol_paused(is_paused)
        check_borrower_default_watch(client, protocol_config)

        logger.info(
            "Monitoring complete — USD3 PPS: %.8f, TVL: %s | sUSD3 PPS: %.8f, TVL: %s",
            usd3_pps,
            format_usd(usd3_tvl),
            susd3_pps,
            format_usd(susd3_tvl),
        )
    except Exception as e:
        logger.error("Error during 3Jane monitoring: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"🚨 *3Jane Monitoring Error*\n❌ {escape_markdown(str(e))}", PROTOCOL))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
