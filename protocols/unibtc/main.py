"""Bedrock uniBTC hourly state polling (no event subscriptions).

Detects unbacked minting, reserve-gate changes, pauses, PoR shortfalls, a stale
or wrong supply feeder, underfunded redemptions, and a BTC peg break. Queued
Safe actions are covered by the Safe monitor; this script only polls current
state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.defillama import fetch_prices
from utils.formatting import format_decimal_amount, format_duration, normalize_token_amount
from utils.http_client import fetch_json
from utils.logger import get_logger
from utils.telegram import send_error_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "unibtc"
logger = get_logger(PROTOCOL)

UNIBTC = Web3.to_checksum_address("0x004E9C3EF86bc1ca1f0bB5C7662861Ee93350568")
VAULT = Web3.to_checksum_address("0x047D41F2544B7F63A8e991aF2068a363d210d6Da")
ROUTER = Web3.to_checksum_address("0xAA732c9c110A84d090a72da230eAe1E779f89246")
POR_FEED = Web3.to_checksum_address("0xc590D9fb8eE78a0909dFF341ccf717000b7b7fF2")
SUPPLY_FEEDER = Web3.to_checksum_address("0xE542919E4b281f10b437F947c8Ba224DdfaBc716")
WBTC = Web3.to_checksum_address("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599")

UNIBTC_DECIMALS = 8
WBTC_DECIMALS = 8
POR_DECIMALS = 18

EXPECTED_ADEQUACY_RATIO = 900
EXPECTED_POR_FEEDER = POR_FEED
EXPECTED_SUPPLY_FEEDER = SUPPLY_FEEDER
EXPECTED_HEARTBEAT = 86_400

MINT_1H_CRITICAL_RAW = 10 * 10**UNIBTC_DECIMALS
MINT_24H_HIGH_RAW = 2 * 10**UNIBTC_DECIMALS
MINT_1H_MAX_BASELINE_AGE = 3 * 60 * 60
MINT_24H_MIN_BASELINE_AGE = 20 * 60 * 60
MINT_24H_MAX_BASELINE_AGE = 36 * 60 * 60
SNAPSHOT_RETENTION = MINT_24H_MAX_BASELINE_AGE

POR_CRITICAL_RATIO = Decimal("1.00")
POR_HIGH_RATIO = Decimal("1.01")
POR_STALE_SECONDS = 86_400
# A healthy feeder tracks the API total supply (ratio ~1.00 through 2026-09-12).
FEEDER_GAP_THRESHOLD = Decimal("0.02")
# A chain whose supply is within this fraction of the feeder gap is named as the likely omission.
FEEDER_CHAIN_MATCH_TOLERANCE = Decimal("0.05")
FEEDER_STALE_SECONDS = 48 * 60 * 60
REDEMPTION_UNDERFUNDED_SECONDS = 24 * 60 * 60
# uniBTC/WBTC over 2025-09 → 2026-09 (4h samples): median 0.9945, <0.99 9% of the time
# (~130 dips/yr, routine), <0.985 ~36 dips/yr, <0.97 4 dips (Dec-16, Apr-26, May-9/10 stress).
PEG_HIGH_FLOOR = Decimal("0.985")
PEG_CRITICAL_FLOOR = Decimal("0.97")

# Undocumented backend of Bedrock's own dashboard (app.bedrock.technology). It is the
# issuer's figure, not independent, and has been observed dropping whole chains from
# ``supplies`` (BOB, ~700 uniBTC, on 2026-09-16), so every response is validated before use.
RESERVE_API_URL = "https://affiliate-api-eosin.vercel.app/api/v1/third/stats/unibtc"
API_MAX_AGE_SECONDS = 60 * 60
# The API's Ethereum entry must match our block-pinned totalSupply within this fraction.
API_MAINNET_SUPPLY_TOLERANCE = Decimal("0.01")
MAINNET_CHAIN_ID = 1
# Chains holding >= ~100 uniBTC (99.4% of supply on 2026-09-16). A response missing any
# of these, or reporting zero for one, understates total supply and is rejected.
API_REQUIRED_CHAINS = {
    MAINNET_CHAIN_ID: "Ethereum",
    56: "BSC",
    8453: "Base",
    60808: "BOB",
    80094: "Berachain",
}
UNIBTC_PRICE_KEYS = (
    "coingecko:universal-btc",
    f"ethereum:{UNIBTC}",
)
WBTC_PRICE_KEY = f"ethereum:{WBTC}"

CACHE_KEY_SNAPSHOTS = "UNIBTC_SUPPLY_SNAPSHOTS"
CACHE_KEY_MINT_1H_ALERTED = "UNIBTC_MINT_1H_ALERTED_SUPPLY"
CACHE_KEY_MINT_24H_ALERTED = "UNIBTC_MINT_24H_ALERTED_SUPPLY"
CACHE_KEY_RESERVE_GATE = "UNIBTC_RESERVE_GATE_ALERTED"
CACHE_KEY_PAUSED = "UNIBTC_PAUSED_ALERTED"
CACHE_KEY_POR_BAND = "UNIBTC_POR_BAND"
CACHE_KEY_POR_STALE = "UNIBTC_POR_STALE_ALERTED"
CACHE_KEY_FEEDER_GAP = "UNIBTC_FEEDER_GAP_ALERTED"
CACHE_KEY_FEEDER_VALUE = "UNIBTC_FEEDER_VALUE"
CACHE_KEY_FEEDER_CHANGED_TS = "UNIBTC_FEEDER_CHANGED_TS"
CACHE_KEY_FEEDER_STALE = "UNIBTC_FEEDER_STALE_ALERTED"
CACHE_KEY_FEEDER_ZERO = "UNIBTC_FEEDER_ZERO_ALERTED"
CACHE_KEY_REDEEM_SINCE = "UNIBTC_REDEEM_UNDERFUNDED_SINCE"
CACHE_KEY_REDEEM_UNCLEARED = "UNIBTC_REDEEM_UNCLEARED"
CACHE_KEY_REDEEM_ALERTED = "UNIBTC_REDEEM_ALERTED"
CACHE_KEY_PEG_BAND = "UNIBTC_PEG_BAND"

ABI_ERC20 = load_abi("common-abi/ERC20.json")
ABI_CHAINLINK = load_abi("common-abi/ChainlinkAggregator.json")
ABI_VAULT = [
    {
        "inputs": [],
        "name": "adequacyRatio",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "chainlinkReserveFeeder",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "uniBTCSupplyFeeder",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "feederHeartbeat",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "outOfService",
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
]
ABI_ROUTER = [
    {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "tokenDebts",
        "outputs": [
            {"name": "totalDebts", "type": "uint256"},
            {"name": "totalCleared", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]
# Number of calls added to the batch in load_state; keep in step with that block.
BATCH_CALL_COUNT = 13

ABI_FEEDER = [
    {
        "inputs": [],
        "name": "totalTokenSupply",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class UnibtcState:
    """Pinned on-chain snapshot for one monitoring run."""

    block_number: int
    block_timestamp: int
    total_supply: int
    adequacy_ratio: int
    chainlink_reserve_feeder: str
    unibtc_supply_feeder: str
    feeder_heartbeat: int
    vault_out_of_service: bool
    vault_paused: bool
    router_paused: bool
    por_answer: int
    por_decimals: int
    por_updated_at: int
    feeder_supply: int
    wbtc_total_debts: int
    wbtc_total_cleared: int
    vault_wbtc_balance: int


@dataclass(frozen=True)
class ChainSupply:
    """One per-chain uniBTC supply entry from the Bedrock API."""

    name: str
    chain_id: int
    supply: Decimal


@dataclass(frozen=True)
class ApiStats:
    """Parsed Bedrock API supply figures."""

    total_supply: Decimal
    updated_at: int
    chain_supplies: tuple[ChainSupply, ...]


# ---------------------------------------------------------------------------
# Formatting / cache
# ---------------------------------------------------------------------------


def _etherscan(address: str) -> str:
    """Return a Markdown etherscan link for a full address."""
    return f"[{address}](https://etherscan.io/address/{address})"


def _fmt_btc(raw: int, decimals: int = UNIBTC_DECIMALS) -> str:
    """Format a satoshi-scale amount as a BTC-unit token string."""
    return format_decimal_amount(normalize_token_amount(raw, decimals))


def _cache_raw(key: str) -> str | int:
    """Read a cache value, returning 0 when unset."""
    return get_last_value_for_key_from_file(cache_filename, key)


def _cache_int(key: str) -> int:
    """Read a cache value as int, defaulting to 0."""
    try:
        return int(_cache_raw(key))
    except (TypeError, ValueError):
        return 0


def _set_cache(key: str, value: int | str) -> None:
    """Write a cache value."""
    write_last_value_to_file(cache_filename, key, value)


def _fingerprint(parts: list[str]) -> str:
    """Return a stable short digest of ``parts``, or "" when there is nothing to report.

    ``hash()`` is salted per process, so alerts deduped across runs need an explicit
    stable digest.
    """
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _alert_on_change(key: str, fingerprint: str, alert: Alert) -> None:
    """Send ``alert`` whenever ``fingerprint`` changes to a new non-empty value.

    Unlike a plain boolean latch this re-fires when the *contents* of a condition
    change (a second reserve-gate field is tampered with, a different component is
    paused) instead of silently holding the first alert's state.
    """
    previous = str(_cache_raw(key))
    if previous in ("0", ""):
        previous = ""
    if fingerprint and fingerprint != previous:
        send_alert(alert)
    if fingerprint != previous:
        _set_cache(key, fingerprint or 0)


def _alert_while_true(key: str, active: bool, alert: Alert) -> None:
    """Send ``alert`` once while ``active`` is true; recovery re-arms."""
    _alert_on_change(key, "1" if active else "", alert)


BAND_RANK = {"ok": 0, "high": 1, "critical": 2}


def severity_band(value: Decimal, critical_below: Decimal, high_below: Decimal) -> str:
    """Return ``"critical"``, ``"high"`` or ``"ok"`` for a value with lower-is-worse floors."""
    if value < critical_below:
        return "critical"
    if value < high_below:
        return "high"
    return "ok"


def _alert_on_band_escalation(key: str, band: str, alerts: dict[str, Alert]) -> None:
    """Send the alert for ``band`` when it is worse than the cached band.

    Moving to a better band updates the cache silently, so HIGH → CRITICAL alerts
    again but CRITICAL → HIGH does not; recovering to ``"ok"`` re-arms both.

    Args:
        key: Cache key holding the last band.
        band: Current band.
        alerts: Alert to send for ``"high"`` and ``"critical"``.
    """
    previous = str(_cache_raw(key))
    if previous not in BAND_RANK:
        previous = "ok"
    if BAND_RANK[band] > BAND_RANK[previous]:
        send_alert(alerts[band])
    if band != previous:
        _set_cache(key, band)


def _to_int(value: Any, label: str) -> int:
    """Convert an RPC response to int and fail with field context when absent."""
    if value is None:
        raise RuntimeError(f"uniBTC RPC returned no value for {label}")
    return int(value)


def _to_bool(value: Any, label: str) -> bool:
    """Convert an RPC response to bool and fail with field context when absent."""
    if value is None:
        raise RuntimeError(f"uniBTC RPC returned no value for {label}")
    return bool(value)


def _to_address(value: Any, label: str) -> str:
    """Checksum an RPC address and fail with field context when absent."""
    if value is None:
        raise RuntimeError(f"uniBTC RPC returned no value for {label}")
    return Web3.to_checksum_address(value)


def _token_debts(value: Any) -> tuple[int, int]:
    """Decode ``tokenDebts`` as ``(totalDebts, totalCleared)``."""
    if value is None:
        raise RuntimeError("uniBTC RPC returned no value for router.tokenDebts(WBTC)")
    return _to_int(value[0], "tokenDebts.totalDebts"), _to_int(value[1], "tokenDebts.totalCleared")


# ---------------------------------------------------------------------------
# Supply snapshots
# ---------------------------------------------------------------------------


def load_supply_snapshots() -> list[tuple[int, int]]:
    """Load ``(timestamp, total_supply)`` snapshots from cache."""
    raw = _cache_raw(CACHE_KEY_SNAPSHOTS)
    if raw in (0, "0", ""):
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid uniBTC supply snapshot cache: %s", raw)
        return []
    snapshots: list[tuple[int, int]] = []
    for item in parsed:
        try:
            snapshots.append((int(item["ts"]), int(item["supply"])))
        except (KeyError, TypeError, ValueError):
            continue
    return snapshots


def store_supply_snapshots(snapshots: list[tuple[int, int]]) -> None:
    """Persist supply snapshots to cache."""
    payload = [{"ts": ts, "supply": supply} for ts, supply in snapshots]
    _set_cache(CACHE_KEY_SNAPSHOTS, json.dumps(payload, separators=(",", ":")))


def prune_snapshots(snapshots: list[tuple[int, int]], now: int) -> list[tuple[int, int]]:
    """Drop snapshots older than the 24h lookback window.

    Snapshots dated after ``now`` are kept rather than dropped: ``block_timestamp``
    can move backwards when the RPC pool rotates to a lagging provider, and
    discarding the newest snapshot there would throw away the only baseline the
    next run has. The delta helpers ignore future-dated entries on their own.
    """
    return [(ts, supply) for ts, supply in snapshots if now - ts <= SNAPSHOT_RETENTION]


def mint_delta_1h(current_supply: int, now: int, snapshots: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Return ``(supply increase, baseline age)`` versus the latest snapshot under 3 hours old."""
    recent = [(ts, supply) for ts, supply in snapshots if 0 < now - ts <= MINT_1H_MAX_BASELINE_AGE]
    if not recent:
        return None
    ts, baseline = max(recent, key=lambda item: item[0])
    return current_supply - baseline, now - ts


def mint_delta_24h(current_supply: int, now: int, snapshots: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Return ``(supply increase, baseline age)`` versus the snapshot closest to 24 hours ago."""
    window = [
        (ts, supply) for ts, supply in snapshots if MINT_24H_MIN_BASELINE_AGE <= now - ts <= MINT_24H_MAX_BASELINE_AGE
    ]
    if not window:
        return None
    ts, baseline = min(window, key=lambda item: abs((now - item[0]) - 86_400))
    return current_supply - baseline, now - ts


# ---------------------------------------------------------------------------
# On-chain + off-chain reads
# ---------------------------------------------------------------------------


def load_state(client: Any) -> UnibtcState:
    """Load uniBTC state at one Mainnet block in an RPC batch.

    Args:
        client: Mainnet Web3 client supporting batch requests.

    Returns:
        Current on-chain uniBTC state pinned to a single block.
    """
    block_number = _to_int(client.eth.block_number, "latest block number")
    block_timestamp = _to_int(client.eth.get_block(block_number)["timestamp"], "block timestamp")
    unibtc = client.eth.contract(address=UNIBTC, abi=ABI_ERC20)
    vault = client.eth.contract(address=VAULT, abi=ABI_VAULT)
    router = client.eth.contract(address=ROUTER, abi=ABI_ROUTER)
    feeder = client.eth.contract(address=SUPPLY_FEEDER, abi=ABI_FEEDER)
    wbtc = client.eth.contract(address=WBTC, abi=ABI_ERC20)
    por = client.eth.contract(address=POR_FEED, abi=ABI_CHAINLINK)
    logger.info("Loading uniBTC state at block=%s", block_number)

    with client.batch_requests() as batch:
        batch.add(unibtc.functions.totalSupply().call(block_identifier=block_number))
        batch.add(vault.functions.adequacyRatio().call(block_identifier=block_number))
        batch.add(vault.functions.chainlinkReserveFeeder().call(block_identifier=block_number))
        batch.add(vault.functions.uniBTCSupplyFeeder().call(block_identifier=block_number))
        batch.add(vault.functions.feederHeartbeat().call(block_identifier=block_number))
        batch.add(vault.functions.outOfService().call(block_identifier=block_number))
        batch.add(vault.functions.paused().call(block_identifier=block_number))
        batch.add(router.functions.paused().call(block_identifier=block_number))
        batch.add(por.functions.latestRoundData().call(block_identifier=block_number))
        batch.add(por.functions.decimals().call(block_identifier=block_number))
        batch.add(feeder.functions.totalTokenSupply().call(block_identifier=block_number))
        batch.add(router.functions.tokenDebts(WBTC).call(block_identifier=block_number))
        batch.add(wbtc.functions.balanceOf(VAULT).call(block_identifier=block_number))
        responses = client.execute_batch(batch)

    # A truncated batch would otherwise surface as a bare IndexError below, with no
    # indication of which call the RPC dropped.
    if responses is None or len(responses) < BATCH_CALL_COUNT:
        raise RuntimeError(
            f"uniBTC RPC batch returned {0 if responses is None else len(responses)} "
            f"of {BATCH_CALL_COUNT} expected responses"
        )

    por_round = responses[8]
    if por_round is None or len(por_round) < 4:
        raise RuntimeError("uniBTC RPC returned no value for PoR latestRoundData")
    total_debts, total_cleared = _token_debts(responses[11])

    return UnibtcState(
        block_number=block_number,
        block_timestamp=block_timestamp,
        total_supply=_to_int(responses[0], "uniBTC.totalSupply"),
        adequacy_ratio=_to_int(responses[1], "vault.adequacyRatio"),
        chainlink_reserve_feeder=_to_address(responses[2], "vault.chainlinkReserveFeeder"),
        unibtc_supply_feeder=_to_address(responses[3], "vault.uniBTCSupplyFeeder"),
        feeder_heartbeat=_to_int(responses[4], "vault.feederHeartbeat"),
        vault_out_of_service=_to_bool(responses[5], "vault.outOfService"),
        vault_paused=_to_bool(responses[6], "vault.paused"),
        router_paused=_to_bool(responses[7], "router.paused"),
        por_answer=_to_int(por_round[1], "PoR.answer"),
        por_decimals=_to_int(responses[9], "PoR.decimals"),
        por_updated_at=_to_int(por_round[3], "PoR.updatedAt"),
        feeder_supply=_to_int(responses[10], "feeder.totalTokenSupply"),
        wbtc_total_debts=total_debts,
        wbtc_total_cleared=total_cleared,
        vault_wbtc_balance=_to_int(responses[12], "WBTC.balanceOf(Vault)"),
    )


def _parse_chain_supplies(raw: Any) -> tuple[ChainSupply, ...] | None:
    """Parse ``data.supplies``, skipping malformed entries; None when the list is absent."""
    if not isinstance(raw, list):
        return None
    supplies: list[ChainSupply] = []
    for item in raw:
        try:
            supplies.append(ChainSupply(str(item["name"]), int(item["chain_id"]), Decimal(str(item["supply"]))))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            logger.warning("Ignoring malformed Bedrock API supply entry: %s", item)
    return tuple(supplies)


def fetch_api_stats() -> ApiStats | None:
    """Fetch and parse Bedrock API supply figures, or None on failure.

    Parsing only; use :func:`validate_api_stats` before trusting the figures.
    """
    payload = fetch_json(RESERVE_API_URL)
    if not payload:
        send_error_message(f"Bedrock reserve API unavailable: {RESERVE_API_URL}", PROTOCOL)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        send_error_message(f"Bedrock reserve API response has no data object: {payload!r}", PROTOCOL)
        return None
    try:
        total_supply = Decimal(str(data["total_supply"]))
        updated_at = int(data["time"]) // 1000
    except (KeyError, TypeError, ValueError, ArithmeticError):
        send_error_message("Bedrock reserve API missing total_supply or time", PROTOCOL)
        return None
    chain_supplies = _parse_chain_supplies(data.get("supplies"))
    if total_supply <= 0 or chain_supplies is None:
        send_error_message(
            f"Bedrock reserve API returned total_supply={total_supply} and supplies={data.get('supplies')!r}",
            PROTOCOL,
        )
        return None
    return ApiStats(total_supply=total_supply, updated_at=updated_at, chain_supplies=chain_supplies)


def api_stats_problems(stats: ApiStats, state: UnibtcState) -> list[str]:
    """Return reasons the API figures cannot be trusted this run; empty when usable.

    Args:
        stats: Parsed API figures.
        state: Current on-chain snapshot, used as the independent reference.

    Returns:
        Human-readable problems, one per failed safeguard.
    """
    problems: list[str] = []
    age = state.block_timestamp - stats.updated_at
    if age > API_MAX_AGE_SECONDS:
        problems.append(f"data is {format_duration(age)} old (max {format_duration(API_MAX_AGE_SECONDS)})")

    by_chain = {entry.chain_id: entry for entry in stats.chain_supplies}
    for chain_id, name in API_REQUIRED_CHAINS.items():
        entry = by_chain.get(chain_id)
        if entry is None:
            problems.append(f"{name} (chain {chain_id}) missing from supplies")
        elif entry.supply <= 0:
            problems.append(f"{name} (chain {chain_id}) supply is {entry.supply}")

    mainnet = by_chain.get(MAINNET_CHAIN_ID)
    onchain = normalize_token_amount(state.total_supply, UNIBTC_DECIMALS)
    if mainnet is not None and mainnet.supply > 0 and onchain > 0:
        mismatch = abs(mainnet.supply - onchain) / onchain
        if mismatch > API_MAINNET_SUPPLY_TOLERANCE:
            problems.append(
                f"Ethereum supply {format_decimal_amount(mainnet.supply)} differs from on-chain totalSupply "
                f"{format_decimal_amount(onchain)} by {mismatch:.2%}"
            )
    return problems


def validate_api_stats(stats: ApiStats | None, state: UnibtcState) -> ApiStats | None:
    """Return ``stats`` only when every safeguard passes; otherwise report and return None.

    A rejected response skips the PoR-coverage and feeder-gap checks for this run.
    Using it instead would be worse than skipping: a dropped chain understates total
    supply, which inflates PoR coverage and hides a feeder that omits the same chain.

    Args:
        stats: Parsed API figures, or None when the fetch failed.
        state: Current on-chain snapshot.

    Returns:
        The validated figures, or None.
    """
    if stats is None:
        return None
    problems = api_stats_problems(stats, state)
    if not problems:
        return stats
    logger.warning("Rejecting Bedrock reserve API response: %s", problems)
    send_error_message(
        "Bedrock reserve API response rejected; PoR coverage and feeder gap checks skipped:\n"
        + "\n".join(f"- {problem}" for problem in problems),
        PROTOCOL,
    )
    return None


def fetch_price_in_wbtc() -> Decimal | None:
    """Return the market price of uniBTC in WBTC via DeFiLlama, or None on failure."""
    keys = [*UNIBTC_PRICE_KEYS, WBTC_PRICE_KEY]
    try:
        prices = fetch_prices(keys)
    except Exception as exc:
        logger.error("Failed to fetch uniBTC/WBTC prices: %s", exc)
        send_error_message(f"Failed to fetch uniBTC/WBTC prices: {exc}", PROTOCOL)
        return None
    wbtc_usd = prices.get(WBTC_PRICE_KEY)
    if wbtc_usd is None or wbtc_usd <= 0:
        send_error_message("WBTC/USD price unavailable from DeFiLlama", PROTOCOL)
        return None
    for key in UNIBTC_PRICE_KEYS:
        usd = prices.get(key)
        # A zero or negative quote is a bad feed, not a depeg: returning it would
        # raise a false peg alert.
        if usd is not None and usd > 0:
            return usd / wbtc_usd
    send_error_message("uniBTC USD price unavailable from DeFiLlama", PROTOCOL)
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def mint_alert_due(key: str, delta: int | None, current_supply: int, threshold: int) -> bool:
    """Return whether a mint alert should fire, deduping repeats of the same mint.

    A single large mint stays inside the lookback window for many hourly runs. The
    supply level that last alerted is cached, so the alert repeats only once supply
    has grown by another full threshold; falling back under the threshold re-arms.

    A missing baseline (``delta is None``) also re-arms. The cached level is only
    meaningful relative to an unbroken series of observations: after a polling gap
    supply may have fallen back well below it, and holding the stale marker would
    silently suppress the next genuine mint.

    Args:
        key: Cache key holding the ``totalSupply`` at the last alert.
        delta: Window supply delta, or None when no baseline is available.
        current_supply: Current ``totalSupply``.
        threshold: Raw mint threshold for this window.

    Returns:
        True when the alert should be sent now.
    """
    last_alert_supply = _cache_int(key)
    if delta is None or delta < threshold:
        if last_alert_supply:
            _set_cache(key, 0)
        return False
    if last_alert_supply and current_supply < last_alert_supply + threshold:
        return False
    _set_cache(key, current_supply)
    return True


def check_unexpected_minting(state: UnibtcState) -> None:
    """Alert on large uniBTC ``totalSupply`` increases over 1h and 24h windows.

    Args:
        state: Current on-chain snapshot.
    """
    now = state.block_timestamp
    snapshots = prune_snapshots(load_supply_snapshots(), now)
    delta_1h = mint_delta_1h(state.total_supply, now, snapshots)
    delta_24h = mint_delta_24h(state.total_supply, now, snapshots)
    logger.info(
        "uniBTC supply=%s delta_1h=%s delta_24h=%s",
        state.total_supply,
        delta_1h,
        delta_24h,
    )
    if delta_1h is None or delta_24h is None:
        # Expected on a first run; otherwise the window is blind (missed runs, or a
        # gap longer than the retention window) and the mint check cannot fire.
        logger.warning(
            "uniBTC mint baseline missing (1h=%s 24h=%s) from %s cached snapshots",
            delta_1h is not None,
            delta_24h is not None,
            len(snapshots),
        )

    # Called unconditionally: a missing baseline must re-arm the marker, not leave
    # a stale one standing.
    due_1h = mint_alert_due(
        CACHE_KEY_MINT_1H_ALERTED,
        delta_1h[0] if delta_1h is not None else None,
        state.total_supply,
        MINT_1H_CRITICAL_RAW,
    )
    due_24h = mint_alert_due(
        CACHE_KEY_MINT_24H_ALERTED,
        delta_24h[0] if delta_24h is not None else None,
        state.total_supply,
        MINT_24H_HIGH_RAW,
    )

    if due_1h and delta_1h is not None:
        send_alert(
            Alert(
                AlertSeverity.CRITICAL,
                "*uniBTC unexpected minting (1h)*\n"
                f"Supply increased by {_fmt_btc(delta_1h[0])} uniBTC over the last "
                f"{format_duration(delta_1h[1])} (threshold {_fmt_btc(MINT_1H_CRITICAL_RAW)}).\n"
                f"Current totalSupply: {_fmt_btc(state.total_supply)}\n"
                "No mint attribution — check the token txs manually.\n"
                f"🔗 Token {_etherscan(UNIBTC)}",
                PROTOCOL,
            )
        )
    if due_24h and delta_24h is not None:
        send_alert(
            Alert(
                AlertSeverity.HIGH,
                "*uniBTC unexpected minting (24h)*\n"
                f"Supply increased by {_fmt_btc(delta_24h[0])} uniBTC over the last "
                f"{format_duration(delta_24h[1])} (threshold {_fmt_btc(MINT_24H_HIGH_RAW)}).\n"
                f"Current totalSupply: {_fmt_btc(state.total_supply)}\n"
                "No mint attribution — check the token txs manually.\n"
                f"🔗 Token {_etherscan(UNIBTC)}",
                PROTOCOL,
            )
        )

    snapshots.append((now, state.total_supply))
    store_supply_snapshots(prune_snapshots(snapshots, now))


def reserve_gate_diffs(state: UnibtcState) -> list[str]:
    """Return human-readable diffs when the Vault reserve gate left its baseline."""
    diffs: list[str] = []
    if state.adequacy_ratio != EXPECTED_ADEQUACY_RATIO:
        diffs.append(f"adequacyRatio: {EXPECTED_ADEQUACY_RATIO} → {state.adequacy_ratio}")
    if state.chainlink_reserve_feeder.lower() != EXPECTED_POR_FEEDER.lower():
        diffs.append(
            f"chainlinkReserveFeeder: {_etherscan(EXPECTED_POR_FEEDER)} → {_etherscan(state.chainlink_reserve_feeder)}"
        )
    if state.unibtc_supply_feeder.lower() != EXPECTED_SUPPLY_FEEDER.lower():
        diffs.append(
            f"uniBTCSupplyFeeder: {_etherscan(EXPECTED_SUPPLY_FEEDER)} → {_etherscan(state.unibtc_supply_feeder)}"
        )
    if state.feeder_heartbeat != EXPECTED_HEARTBEAT:
        diffs.append(f"feederHeartbeat: {EXPECTED_HEARTBEAT} → {state.feeder_heartbeat}")
    return diffs


def check_reserve_gate(state: UnibtcState) -> None:
    """Alert while the Vault PoR gate differs from the known-good baseline.

    Keyed on the diff contents, so a second tampered field re-alerts instead of
    being swallowed by the first alert's latch.

    Args:
        state: Current on-chain snapshot.
    """
    diffs = reserve_gate_diffs(state)
    logger.info("uniBTC reserve gate diffs=%s", diffs)
    message = (
        "*uniBTC reserve gate changed*\n"
        "Operational EOA with Vault MANAGER_ROLE can change these without a Safe tx.\n"
        + "\n".join(f"- {line}" for line in diffs)
        + f"\n🔗 Vault {_etherscan(VAULT)}"
    )
    _alert_on_change(
        CACHE_KEY_RESERVE_GATE,
        _fingerprint(diffs),
        Alert(AlertSeverity.CRITICAL, message, PROTOCOL),
    )


def check_paused(state: UnibtcState) -> None:
    """Alert while the Vault or live router is stopped or paused.

    Keyed on which components are flagged, so a shift in the pause set re-alerts
    instead of leaving the first alert's text standing.

    Args:
        state: Current on-chain snapshot.
    """
    flags = []
    if state.vault_out_of_service:
        flags.append(f"Vault.outOfService() = true {_etherscan(VAULT)}")
    if state.vault_paused:
        flags.append(f"Vault.paused() = true {_etherscan(VAULT)}")
    if state.router_paused:
        flags.append(f"Router.paused() = true {_etherscan(ROUTER)}")
    logger.info("uniBTC pause flags=%s", flags)
    message = "*uniBTC Vault or router paused*\n" + "\n".join(f"- {line}" for line in flags)
    _alert_on_change(CACHE_KEY_PAUSED, _fingerprint(flags), Alert(AlertSeverity.HIGH, message, PROTOCOL))


def por_coverage_ratio(por_answer: int, por_decimals: int, api_total_supply: Decimal) -> Decimal:
    """Return Chainlink PoR BTC reserves divided by Bedrock API total supply."""
    reserves = normalize_token_amount(por_answer, por_decimals)
    return reserves / api_total_supply


def check_por_coverage(state: UnibtcState, api_total_supply: Decimal | None) -> None:
    """Alert when Chainlink PoR reserves fall below API supply.

    Args:
        state: Current on-chain snapshot.
        api_total_supply: Bedrock dashboard total supply, or None to skip.
    """
    if api_total_supply is None:
        return
    ratio = por_coverage_ratio(state.por_answer, state.por_decimals, api_total_supply)
    reserves = normalize_token_amount(state.por_answer, state.por_decimals)
    logger.info("uniBTC PoR coverage ratio=%s reserves=%s api_supply=%s", ratio, reserves, api_total_supply)

    body = (
        f"PoR / API supply = {ratio:.4%} "
        f"(CRITICAL < {POR_CRITICAL_RATIO:.0%}, HIGH < {POR_HIGH_RATIO:.0%})\n"
        f"Chainlink PoR: {format_decimal_amount(reserves)} BTC\n"
        f"API total_supply: {format_decimal_amount(api_total_supply)} uniBTC\n"
        f"🔗 PoR {_etherscan(POR_FEED)}"
    )
    _alert_on_band_escalation(
        CACHE_KEY_POR_BAND,
        severity_band(ratio, POR_CRITICAL_RATIO, POR_HIGH_RATIO),
        {
            "critical": Alert(AlertSeverity.CRITICAL, f"*uniBTC reserves below supply*\n{body}", PROTOCOL),
            "high": Alert(AlertSeverity.HIGH, f"*uniBTC reserves thin versus supply*\n{body}", PROTOCOL),
        },
    )


def por_stale_threshold(state: UnibtcState) -> int:
    """Return the PoR staleness threshold actually enforced by the Vault.

    Tracks the live ``feederHeartbeat`` so the alert follows the window in which
    ``mint()`` really reverts, but never above ``POR_STALE_SECONDS``: a heartbeat
    widened by a compromised MANAGER_ROLE must not also blind this check. The
    widening itself is reported by :func:`check_reserve_gate`.

    Args:
        state: Current on-chain snapshot.

    Returns:
        Staleness threshold in seconds.
    """
    if state.feeder_heartbeat <= 0:
        return POR_STALE_SECONDS
    return min(state.feeder_heartbeat, POR_STALE_SECONDS)


def check_por_stale(state: UnibtcState) -> None:
    """Alert once while the Chainlink PoR feed is older than the Vault heartbeat.

    Args:
        state: Current on-chain snapshot.
    """
    age = state.block_timestamp - state.por_updated_at
    threshold = por_stale_threshold(state)
    stale = age > threshold
    logger.info("uniBTC PoR age=%ss threshold=%ss stale=%s", age, threshold, stale)
    message = (
        "*uniBTC PoR stale*\n"
        f"latestRoundData.updatedAt is {format_duration(age)} old "
        f"(Vault mint() reverts after {format_duration(threshold)}).\n"
        f"🔗 PoR {_etherscan(POR_FEED)}"
    )
    _alert_while_true(CACHE_KEY_POR_STALE, stale, Alert(AlertSeverity.HIGH, message, PROTOCOL))


def feeder_shortfall(feeder_supply_raw: int, api_total_supply: Decimal) -> Decimal:
    """Return API total supply minus feeder supply; positive when the feeder under-reports."""
    return api_total_supply - normalize_token_amount(feeder_supply_raw, UNIBTC_DECIMALS)


def chain_matching_gap(gap: Decimal, chain_supplies: tuple[ChainSupply, ...]) -> ChainSupply | None:
    """Return the chain whose supply best explains ``gap``, if within tolerance.

    The feeder has been observed omitting exactly one chain's supply (BOB), so naming
    the matching chain turns a bare percentage into an actionable alert.
    """
    size = abs(gap)
    if size <= 0 or not chain_supplies:
        return None
    best = min(chain_supplies, key=lambda entry: abs(entry.supply - size))
    if abs(best.supply - size) <= size * FEEDER_CHAIN_MATCH_TOLERANCE:
        return best
    return None


def check_feeder_gap(state: UnibtcState, api: ApiStats) -> None:
    """Alert once while the supply feeder differs from validated API total supply by >2%.

    Args:
        state: Current on-chain snapshot.
        api: Validated Bedrock API figures.
    """
    shortfall = feeder_shortfall(state.feeder_supply, api.total_supply)
    gap = abs(shortfall) / api.total_supply
    wrong = gap > FEEDER_GAP_THRESHOLD
    match = chain_matching_gap(shortfall, api.chain_supplies) if wrong else None
    logger.info(
        "uniBTC feeder gap=%s shortfall=%s feeder=%s api=%s match=%s",
        gap,
        shortfall,
        state.feeder_supply,
        api.total_supply,
        match,
    )
    direction = "below" if shortfall > 0 else "above"
    if match is not None:
        hint = (
            f"Gap matches {match.name} (chain {match.chain_id}) supply of "
            f"{format_decimal_amount(match.supply)} uniBTC; the feeder likely "
            f"{'omits' if shortfall > 0 else 'double-counts'} that chain.\n"
        )
    else:
        hint = "No single chain's supply matches the gap.\n"
    message = (
        "*uniBTC supply feeder wrong*\n"
        f"Feeder is {format_decimal_amount(abs(shortfall))} uniBTC ({gap:.2%}) {direction} Bedrock API "
        f"total supply (threshold {FEEDER_GAP_THRESHOLD:.0%}).\n"
        f"{hint}"
        f"Feeder totalTokenSupply: {_fmt_btc(state.feeder_supply)} uniBTC\n"
        f"API total_supply: {format_decimal_amount(api.total_supply)} uniBTC\n"
        f"🔗 Feeder {_etherscan(SUPPLY_FEEDER)}"
    )
    _alert_while_true(CACHE_KEY_FEEDER_GAP, wrong, Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_feeder_zero(state: UnibtcState) -> None:
    """Alert once while the supply feeder reports a zero ``totalTokenSupply``.

    A zero supply satisfies the Vault reserve check for any mint amount, so this
    fires on the first run and needs no Bedrock API, unlike the gap check.

    Args:
        state: Current on-chain snapshot.
    """
    zero = state.feeder_supply == 0
    logger.info("uniBTC feeder zero=%s", zero)
    message = (
        "*uniBTC supply feeder reports zero*\n"
        "totalTokenSupply() is 0, so the Vault reserve check passes for any mint amount.\n"
        f"🔗 Feeder {_etherscan(SUPPLY_FEEDER)}\n"
        f"🔗 Vault {_etherscan(VAULT)}"
    )
    _alert_while_true(CACHE_KEY_FEEDER_ZERO, zero, Alert(AlertSeverity.CRITICAL, message, PROTOCOL))


def check_supply_feeder(state: UnibtcState, api: ApiStats | None) -> None:
    """Alert when the supply feeder reports zero, diverges from the API, or stops updating.

    Args:
        state: Current on-chain snapshot.
        api: Validated Bedrock API figures, or None to skip the gap check.
    """
    check_feeder_zero(state)
    if api is not None:
        check_feeder_gap(state, api)

    previous_value = _cache_int(CACHE_KEY_FEEDER_VALUE)
    changed_ts = _cache_int(CACHE_KEY_FEEDER_CHANGED_TS)
    # The timestamp, not the value, marks "never seen". The cache reads 0 for an unset
    # key, so testing previous_value == 0 would treat a feeder genuinely reporting 0 as
    # a fresh observation on every run and never report it stale — the one reading that
    # most needs reporting, since a zero supply satisfies the Vault mint gate outright.
    if changed_ts <= 0 or previous_value != state.feeder_supply:
        _set_cache(CACHE_KEY_FEEDER_VALUE, state.feeder_supply)
        _set_cache(CACHE_KEY_FEEDER_CHANGED_TS, state.block_timestamp)
        if _cache_int(CACHE_KEY_FEEDER_STALE):
            _set_cache(CACHE_KEY_FEEDER_STALE, 0)
        return

    unchanged_for = state.block_timestamp - changed_ts
    stale = unchanged_for > FEEDER_STALE_SECONDS
    logger.info("uniBTC feeder unchanged_for=%ss stale=%s", unchanged_for, stale)
    message = (
        "*uniBTC supply feeder stale*\n"
        f"totalTokenSupply has been {_fmt_btc(state.feeder_supply)} uniBTC for "
        f"{format_duration(unchanged_for)} (normally changes daily; threshold "
        f"{format_duration(FEEDER_STALE_SECONDS)}).\n"
        f"🔗 Feeder {_etherscan(SUPPLY_FEEDER)}"
    )
    _alert_while_true(CACHE_KEY_FEEDER_STALE, stale, Alert(AlertSeverity.HIGH, message, PROTOCOL))


def uncleared_wbtc_debt(state: UnibtcState) -> int:
    """Return live-router WBTC debt that has not been cleared."""
    return max(state.wbtc_total_debts - state.wbtc_total_cleared, 0)


def check_redemptions_underfunded(state: UnibtcState) -> None:
    """Alert when uncleared WBTC redemptions exceed Vault WBTC for >24h and grow.

    Args:
        state: Current on-chain snapshot.
    """
    uncleared = uncleared_wbtc_debt(state)
    underfunded = uncleared > state.vault_wbtc_balance
    logger.info(
        "uniBTC redemptions uncleared=%s vault_wbtc=%s underfunded=%s",
        uncleared,
        state.vault_wbtc_balance,
        underfunded,
    )
    if not underfunded:
        if _cache_int(CACHE_KEY_REDEEM_SINCE) or _cache_int(CACHE_KEY_REDEEM_ALERTED):
            _set_cache(CACHE_KEY_REDEEM_SINCE, 0)
            _set_cache(CACHE_KEY_REDEEM_UNCLEARED, 0)
            _set_cache(CACHE_KEY_REDEEM_ALERTED, 0)
        return

    since = _cache_int(CACHE_KEY_REDEEM_SINCE)
    last_uncleared = _cache_int(CACHE_KEY_REDEEM_UNCLEARED)
    if since <= 0:
        _set_cache(CACHE_KEY_REDEEM_SINCE, state.block_timestamp)
        _set_cache(CACHE_KEY_REDEEM_UNCLEARED, uncleared)
        return

    duration = state.block_timestamp - since
    growing = uncleared > last_uncleared
    should_alert = duration > REDEMPTION_UNDERFUNDED_SECONDS and growing
    already = _cache_int(CACHE_KEY_REDEEM_ALERTED) == 1
    if should_alert and not already:
        send_alert(
            Alert(
                AlertSeverity.HIGH,
                "*uniBTC redemptions underfunded*\n"
                f"Uncleared WBTC debt {_fmt_btc(uncleared, WBTC_DECIMALS)} exceeds Vault WBTC "
                f"{_fmt_btc(state.vault_wbtc_balance, WBTC_DECIMALS)} for {format_duration(duration)} "
                "and is growing.\n"
                f"Includes requests still inside the 8-day delay.\n"
                f"🔗 Router {_etherscan(ROUTER)}\n"
                f"🔗 Vault {_etherscan(VAULT)}",
                PROTOCOL,
            )
        )
        _set_cache(CACHE_KEY_REDEEM_ALERTED, 1)
    _set_cache(CACHE_KEY_REDEEM_UNCLEARED, uncleared)


def check_peg(price_in_wbtc: Decimal | None) -> None:
    """Alert when uniBTC trades below 0.985 WBTC (HIGH) or 0.97 WBTC (CRITICAL).

    Alerts on entering a worse band, like PoR coverage.

    Args:
        price_in_wbtc: uniBTC/WBTC ratio, or None to skip.
    """
    if price_in_wbtc is None:
        return
    band = severity_band(price_in_wbtc, PEG_CRITICAL_FLOOR, PEG_HIGH_FLOOR)
    logger.info("uniBTC/WBTC peg=%s band=%s", price_in_wbtc, band)
    body = (
        f"Price: {format_decimal_amount(price_in_wbtc)} WBTC "
        f"(CRITICAL < {PEG_CRITICAL_FLOOR}, HIGH < {PEG_HIGH_FLOOR})\n"
        f"🔗 Token {_etherscan(UNIBTC)}"
    )
    _alert_on_band_escalation(
        CACHE_KEY_PEG_BAND,
        band,
        {
            "critical": Alert(
                AlertSeverity.CRITICAL, f"*uniBTC peg below {PEG_CRITICAL_FLOOR} WBTC*\n{body}", PROTOCOL
            ),
            "high": Alert(AlertSeverity.HIGH, f"*uniBTC peg below {PEG_HIGH_FLOOR} WBTC*\n{body}", PROTOCOL),
        },
    )


def main() -> None:
    """Run all Bedrock uniBTC state-polling checks."""
    client = ChainManager.get_client(Chain.MAINNET)
    state = load_state(client)
    api = validate_api_stats(fetch_api_stats(), state)
    price_in_wbtc = fetch_price_in_wbtc()

    check_unexpected_minting(state)
    check_reserve_gate(state)
    check_paused(state)
    check_por_coverage(state, api.total_supply if api is not None else None)
    check_por_stale(state)
    check_supply_feeder(state, api)
    check_redemptions_underfunded(state)
    check_peg(price_in_wbtc)

    logger.info(
        "uniBTC monitoring complete at block=%s supply=%s",
        state.block_number,
        _fmt_btc(state.total_supply),
    )


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
