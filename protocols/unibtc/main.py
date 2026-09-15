"""Bedrock uniBTC hourly state polling (no event subscriptions).

Detects unbacked minting, reserve-gate changes, pauses, PoR shortfalls, a stale
or wrong supply feeder, underfunded redemptions, and a BTC peg break. Queued
Safe actions are covered by the Safe monitor; this script only polls current
state.
"""

from __future__ import annotations

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
from utils.pegged_assets import BTC_USD_DEFILLAMA_KEY
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
FEEDER_GAP_THRESHOLD = Decimal("0.02")
FEEDER_STALE_SECONDS = 48 * 60 * 60
REDEMPTION_UNDERFUNDED_SECONDS = 24 * 60 * 60
PEG_FLOOR = Decimal("0.98")

RESERVE_API_URL = "https://affiliate-api-eosin.vercel.app/api/v1/third/stats/unibtc"
UNIBTC_PRICE_KEYS = (
    "coingecko:universal-btc",
    f"ethereum:{UNIBTC}",
)

CACHE_KEY_SNAPSHOTS = "UNIBTC_SUPPLY_SNAPSHOTS"
CACHE_KEY_RESERVE_GATE = "UNIBTC_RESERVE_GATE_ALERTED"
CACHE_KEY_PAUSED = "UNIBTC_PAUSED_ALERTED"
CACHE_KEY_POR_BAND = "UNIBTC_POR_BAND"
CACHE_KEY_POR_STALE = "UNIBTC_POR_STALE_ALERTED"
CACHE_KEY_FEEDER_GAP = "UNIBTC_FEEDER_GAP_ALERTED"
CACHE_KEY_FEEDER_VALUE = "UNIBTC_FEEDER_VALUE"
CACHE_KEY_FEEDER_CHANGED_TS = "UNIBTC_FEEDER_CHANGED_TS"
CACHE_KEY_FEEDER_STALE = "UNIBTC_FEEDER_STALE_ALERTED"
CACHE_KEY_REDEEM_SINCE = "UNIBTC_REDEEM_UNDERFUNDED_SINCE"
CACHE_KEY_REDEEM_UNCLEARED = "UNIBTC_REDEEM_UNCLEARED"
CACHE_KEY_REDEEM_ALERTED = "UNIBTC_REDEEM_ALERTED"
CACHE_KEY_PEG = "UNIBTC_PEG_ALERTED"

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


def _alert_while_true(key: str, active: bool, alert: Alert) -> None:
    """Send ``alert`` once while ``active`` is true; recovery re-arms."""
    previous = _cache_int(key) == 1
    if active and not previous:
        send_alert(alert)
    if active != previous:
        _set_cache(key, int(active))


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
    """Drop snapshots older than the 24h lookback window."""
    return [(ts, supply) for ts, supply in snapshots if 0 <= now - ts <= SNAPSHOT_RETENTION]


def mint_delta_1h(current_supply: int, now: int, snapshots: list[tuple[int, int]]) -> int | None:
    """Return the supply increase versus the latest snapshot no older than 3 hours."""
    recent = [(ts, supply) for ts, supply in snapshots if 0 < now - ts <= MINT_1H_MAX_BASELINE_AGE]
    if not recent:
        return None
    _ts, baseline = max(recent, key=lambda item: item[0])
    return current_supply - baseline


def mint_delta_24h(current_supply: int, now: int, snapshots: list[tuple[int, int]]) -> int | None:
    """Return the supply increase versus the snapshot closest to 24 hours ago."""
    window = [
        (ts, supply) for ts, supply in snapshots if MINT_24H_MIN_BASELINE_AGE <= now - ts <= MINT_24H_MAX_BASELINE_AGE
    ]
    if not window:
        return None
    _ts, baseline = min(window, key=lambda item: abs((now - item[0]) - 86_400))
    return current_supply - baseline


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


def fetch_api_total_supply() -> Decimal | None:
    """Return Bedrock dashboard ``data.total_supply``, or None on failure."""
    payload = fetch_json(RESERVE_API_URL)
    if not payload:
        send_error_message(f"Bedrock reserve API unavailable: {RESERVE_API_URL}", PROTOCOL)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    raw = data.get("total_supply") if isinstance(data, dict) else None
    try:
        supply = Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError):
        send_error_message(f"Bedrock reserve API missing total_supply: {payload!r}", PROTOCOL)
        return None
    if supply <= 0:
        send_error_message(f"Bedrock reserve API returned non-positive total_supply: {supply}", PROTOCOL)
        return None
    return supply


def fetch_price_in_btc() -> Decimal | None:
    """Return uniBTC priced in BTC via DeFiLlama, or None on failure."""
    keys = [*UNIBTC_PRICE_KEYS, BTC_USD_DEFILLAMA_KEY]
    try:
        prices = fetch_prices(list(keys))
    except Exception as exc:
        logger.error("Failed to fetch uniBTC/BTC prices: %s", exc)
        send_error_message(f"Failed to fetch uniBTC/BTC prices: {exc}", PROTOCOL)
        return None
    btc_usd = prices.get(BTC_USD_DEFILLAMA_KEY)
    if btc_usd is None or btc_usd <= 0:
        send_error_message("BTC/USD price unavailable from DeFiLlama", PROTOCOL)
        return None
    for key in UNIBTC_PRICE_KEYS:
        usd = prices.get(key)
        if usd is not None:
            return usd / btc_usd
    send_error_message("uniBTC USD price unavailable from DeFiLlama", PROTOCOL)
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


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

    if delta_1h is not None and delta_1h >= MINT_1H_CRITICAL_RAW:
        send_alert(
            Alert(
                AlertSeverity.CRITICAL,
                "*uniBTC unexpected minting (1h)*\n"
                f"Supply increased by {_fmt_btc(delta_1h)} uniBTC in about 1 hour "
                f"(threshold {_fmt_btc(MINT_1H_CRITICAL_RAW)}).\n"
                f"Current totalSupply: {_fmt_btc(state.total_supply)}\n"
                "No mint attribution — check the token txs manually.\n"
                f"🔗 Token {_etherscan(UNIBTC)}",
                PROTOCOL,
            )
        )
    if delta_24h is not None and delta_24h >= MINT_24H_HIGH_RAW:
        send_alert(
            Alert(
                AlertSeverity.HIGH,
                "*uniBTC unexpected minting (24h)*\n"
                f"Supply increased by {_fmt_btc(delta_24h)} uniBTC over ~24 hours "
                f"(threshold {_fmt_btc(MINT_24H_HIGH_RAW)}).\n"
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
    """Alert once while the Vault PoR gate differs from the known-good baseline.

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
    _alert_while_true(
        CACHE_KEY_RESERVE_GATE,
        bool(diffs),
        Alert(AlertSeverity.CRITICAL, message, PROTOCOL),
    )


def check_paused(state: UnibtcState) -> None:
    """Alert once while the Vault or live router is stopped or paused.

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
    _alert_while_true(CACHE_KEY_PAUSED, bool(flags), Alert(AlertSeverity.HIGH, message, PROTOCOL))


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

    if ratio < POR_CRITICAL_RATIO:
        band = "critical"
    elif ratio < POR_HIGH_RATIO:
        band = "high"
    else:
        band = "ok"

    previous = str(_cache_raw(CACHE_KEY_POR_BAND))
    if previous not in {"ok", "high", "critical"}:
        previous = "ok"
    band_rank = {"ok": 0, "high": 1, "critical": 2}
    if band_rank[band] > band_rank[previous]:
        severity = AlertSeverity.CRITICAL if band == "critical" else AlertSeverity.HIGH
        title = "*uniBTC reserves below supply*" if band == "critical" else "*uniBTC reserves thin versus supply*"
        send_alert(
            Alert(
                severity,
                f"{title}\n"
                f"PoR / API supply = {ratio:.4%} "
                f"(CRITICAL < {POR_CRITICAL_RATIO:.0%}, HIGH < {POR_HIGH_RATIO:.0%})\n"
                f"Chainlink PoR: {format_decimal_amount(reserves)} BTC\n"
                f"API total_supply: {format_decimal_amount(api_total_supply)} uniBTC\n"
                f"🔗 PoR {_etherscan(POR_FEED)}",
                PROTOCOL,
            )
        )
    if band != previous:
        _set_cache(CACHE_KEY_POR_BAND, band)


def check_por_stale(state: UnibtcState) -> None:
    """Alert once while the Chainlink PoR feed is older than the Vault heartbeat.

    Args:
        state: Current on-chain snapshot.
    """
    age = state.block_timestamp - state.por_updated_at
    stale = age > POR_STALE_SECONDS
    logger.info("uniBTC PoR age=%ss stale=%s", age, stale)
    message = (
        "*uniBTC PoR stale*\n"
        f"latestRoundData.updatedAt is {format_duration(age)} old "
        f"(Vault mint() reverts after {format_duration(POR_STALE_SECONDS)}).\n"
        f"🔗 PoR {_etherscan(POR_FEED)}"
    )
    _alert_while_true(CACHE_KEY_POR_STALE, stale, Alert(AlertSeverity.HIGH, message, PROTOCOL))


def feeder_gap(feeder_supply_raw: int, api_total_supply: Decimal) -> Decimal:
    """Return absolute relative gap between the on-chain feeder and API supply."""
    feeder = normalize_token_amount(feeder_supply_raw, UNIBTC_DECIMALS)
    return abs(feeder - api_total_supply) / api_total_supply


def check_supply_feeder(state: UnibtcState, api_total_supply: Decimal | None) -> None:
    """Alert when the supply feeder diverges from the dashboard or stops updating.

    Args:
        state: Current on-chain snapshot.
        api_total_supply: Bedrock dashboard total supply, or None to skip the gap check.
    """
    if api_total_supply is not None:
        gap = feeder_gap(state.feeder_supply, api_total_supply)
        logger.info("uniBTC feeder gap=%s feeder=%s api=%s", gap, state.feeder_supply, api_total_supply)
        message = (
            "*uniBTC supply feeder wrong*\n"
            f"On-chain feeder vs Bedrock API gap is {gap:.2%} (threshold {FEEDER_GAP_THRESHOLD:.0%}).\n"
            f"Feeder totalTokenSupply: {_fmt_btc(state.feeder_supply)} uniBTC\n"
            f"API total_supply: {format_decimal_amount(api_total_supply)} uniBTC\n"
            f"🔗 Feeder {_etherscan(SUPPLY_FEEDER)}"
        )
        _alert_while_true(
            CACHE_KEY_FEEDER_GAP,
            gap > FEEDER_GAP_THRESHOLD,
            Alert(AlertSeverity.HIGH, message, PROTOCOL),
        )

    previous_value = _cache_int(CACHE_KEY_FEEDER_VALUE)
    changed_ts = _cache_int(CACHE_KEY_FEEDER_CHANGED_TS)
    if previous_value == 0 or previous_value != state.feeder_supply:
        _set_cache(CACHE_KEY_FEEDER_VALUE, state.feeder_supply)
        _set_cache(CACHE_KEY_FEEDER_CHANGED_TS, state.block_timestamp)
        if _cache_int(CACHE_KEY_FEEDER_STALE):
            _set_cache(CACHE_KEY_FEEDER_STALE, 0)
        return

    unchanged_for = state.block_timestamp - changed_ts
    stale = changed_ts > 0 and unchanged_for > FEEDER_STALE_SECONDS
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


def check_peg(price_in_btc: Decimal | None) -> None:
    """Alert once while uniBTC trades below 0.98 BTC.

    Args:
        price_in_btc: uniBTC/BTC ratio, or None to skip.
    """
    if price_in_btc is None:
        return
    depegged = price_in_btc < PEG_FLOOR
    logger.info("uniBTC peg=%s depegged=%s", price_in_btc, depegged)
    message = (
        "*uniBTC peg below 0.98 BTC*\n"
        f"Price: {format_decimal_amount(price_in_btc)} BTC (threshold {PEG_FLOOR} BTC)\n"
        f"🔗 Token {_etherscan(UNIBTC)}"
    )
    _alert_while_true(CACHE_KEY_PEG, depegged, Alert(AlertSeverity.HIGH, message, PROTOCOL))


def main() -> None:
    """Run all Bedrock uniBTC state-polling checks."""
    client = ChainManager.get_client(Chain.MAINNET)
    state = load_state(client)
    api_total_supply = fetch_api_total_supply()
    price_in_btc = fetch_price_in_btc()

    check_unexpected_minting(state)
    check_reserve_gate(state)
    check_paused(state)
    check_por_coverage(state, api_total_supply)
    check_por_stale(state)
    check_supply_feeder(state, api_total_supply)
    check_redemptions_underfunded(state)
    check_peg(price_in_btc)

    logger.info(
        "uniBTC monitoring complete at block=%s supply=%s",
        state.block_number,
        _fmt_btc(state.total_supply),
    )


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
