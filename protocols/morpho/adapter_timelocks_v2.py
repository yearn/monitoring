"""On-chain checks of ``MorphoMarketV1AdapterV2``'s own timelock.

The adapter's ``burnShares(marketId)`` is the V2 equivalent of a V1
``submitMarketRemoval``: once executed it zeroes the adapter's supply shares in
that market, so ``realAssets`` drops and the vault takes the loss. It is
timelocked on the adapter itself, which the Morpho GraphQL API does not expose.

The adapter's ``executableAt`` mapping is keyed by the *exact* submitted
calldata, but Solidity ignores trailing bytes when decoding ``burnShares``'s
single ``bytes32`` argument. A burn can therefore be submitted under a
non-canonical key (canonical calldata plus extra bytes), so reading only the
canonical key is not enough. Pending burns are found two ways:

* **Submit logs** (``Submit(bytes4 indexed selector, bytes data, uint256)``
  filtered to ``burnShares``), scanned incrementally from the adapter's creation
  block with a cached cursor. Every discovered calldata is kept verbatim and
  re-read via ``executableAt(data)`` until it resolves.
* **Canonical keys** for every held market, a fast path that works even while a
  log backfill is still catching up.

Assets at risk come from the adapter's stored ``supplyShares`` and Morpho
Blue's stored market totals, never ``expectedSupplyAssets``: that goes through
the market's IRM, which may revert, and burning shares of such a market is
explicitly supported by the adapter.

We also read ``timelock(burnShares)`` to catch a shortened window after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from eth_abi import decode as abi_decode
from web3 import Web3

from protocols.morpho._alerts import REMINDER_WINDOW_SECONDS, VaultDiff
from protocols.morpho._shared import (
    MORPHO_BLUE_ABI_PATH,
    fetch_market_metadata,
    format_amount_at_risk,
    format_countdown,
    format_timelock,
    format_ts,
    get_market_url,
    to_assets_down,
)
from protocols.morpho.config import (
    DEFAULT_SUBMIT_LOG_BLOCK_RANGE,
    MAX_SUBMIT_LOG_REQUESTS_PER_RUN,
    SUBMIT_LOG_BLOCK_RANGE_BY_CHAIN,
)
from protocols.morpho.v2_decoders import selector_of
from utils.abi import load_abi
from utils.alert import AlertSeverity
from utils.cache import get_last_value_for_key_from_file, morpho_filename, morpho_key, write_last_value_to_file
from utils.chains import Chain
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("morpho.adapter_timelocks_v2")

ABI_MARKET_V1_ADAPTER_V2 = load_abi("protocols/morpho/abi/morpho_market_v1_adapter_v2.json")
ABI_MORPHO_BLUE = load_abi(MORPHO_BLUE_ABI_PATH)
BURN_SHARES_SELECTOR = selector_of("burnShares(bytes32)")
SUBMIT_TOPIC = bytes(Web3.keccak(text="Submit(bytes4,bytes,uint256)"))
# Indexed bytes4 topics are left-aligned and zero-padded to 32 bytes.
BURN_SHARES_TOPIC = BURN_SHARES_SELECTOR + b"\x00" * 28
# Shortest calldata ``burnShares`` can execute with: selector + one bytes32 word.
BURN_SHARES_MIN_LENGTH = 36
# Re-scan a few blocks below the cursor so a shallow reorg can't hide a Submit.
LOG_OVERLAP_BLOCKS = 50

# Cache value-type tags used with utils.cache.morpho_key.
BURN_TIMELOCK_TYPE = "v2_adapter_burn_timelock"
PENDING_BURN_TYPE = "v2_adapter_pending_burn"
PENDING_BURN_REMINDER_TYPE = "v2_adapter_pending_burn_reminder"
PENDING_BURN_INDEX_TYPE = "v2_adapter_pending_burn_index"
TRACKED_BURN_DATA_TYPE = "v2_adapter_tracked_burn_data"
SUBMIT_LOG_CURSOR_TYPE = "v2_adapter_submit_log_cursor"


@dataclass(frozen=True)
class PendingBurn:
    """A submitted, not yet executed ``burnShares``, keyed by its exact calldata."""

    data: bytes
    executable_at: int
    shares: int  # the adapter's stored supply shares in the market
    assets: int  # those shares valued at Morpho Blue's stored market totals

    @property
    def market_id(self) -> str:
        """0x-prefixed market id the calldata burns (its first argument word)."""
        return Web3.to_hex(self.data[4:BURN_SHARES_MIN_LENGTH])

    @property
    def extra_bytes(self) -> int:
        """Trailing bytes beyond canonical calldata; > 0 means a non-canonical key."""
        return len(self.data) - BURN_SHARES_MIN_LENGTH

    @property
    def data_key(self) -> str:
        """Stable cache key for this exact calldata."""
        return bytes(Web3.keccak(self.data)).hex()


@dataclass(frozen=True)
class AdapterTimelockState:
    """On-chain timelock state of one ``MorphoMarketV1AdapterV2``."""

    adapter: str  # checksummed
    burn_timelock: int
    pending_burns: List[PendingBurn] = field(default_factory=list)
    # Submit-log scan progress: last block scanned, and blocks still unscanned
    # below the head (non-zero while a backfill is catching up).
    scanned_to_block: int = 0
    lag_blocks: int = 0
    # Set when the Submit-log scan failed; canonical and tracked keys were still checked.
    scan_error: str = ""


def burn_shares_calldata(market_id: bytes) -> bytes:
    """Canonical calldata of ``burnShares(marketId)``."""
    return BURN_SHARES_SELECTOR + market_id


def _execute(client: Any, functions: List[Any], block_number: int) -> List[Any]:
    """Call contract functions as one JSON-RPC batch and check every call answered.

    ``functions`` are bound but not yet called (``contract.functions.x(args)``):
    ``.call()`` must run inside the batch context, or it executes eagerly.
    """
    if not functions:
        return []
    with client.batch_requests() as batch:
        for function in functions:
            batch.add(function.call(block_identifier=block_number))
        responses = client.execute_batch(batch)
    if len(responses) != len(functions):
        raise ValueError(f"Expected {len(functions)} batch responses, got {len(responses)}")
    return list(responses)


# ----------------------------------------------------------------------------
# Cache helpers
# ----------------------------------------------------------------------------


def _read_str(key: str) -> str:
    raw = get_last_value_for_key_from_file(morpho_filename, key)
    return str(raw) if raw else ""


def _read_int(key: str) -> int:
    try:
        return int(get_last_value_for_key_from_file(morpho_filename, key))
    except TypeError, ValueError:
        return 0


def _write(key: str, value: Any) -> None:
    write_last_value_to_file(morpho_filename, key, value)


def _key(adapter: str, item: str, value_type: str) -> str:
    return str(morpho_key(adapter.lower(), item, value_type))


def _tracked_data(adapter: str) -> set[bytes]:
    """Calldata of burns seen pending on earlier runs (comma-joined hex in the cache)."""
    raw = _read_str(_key(adapter, "tracked", TRACKED_BURN_DATA_TYPE))
    return {bytes.fromhex(item) for item in raw.split(",") if item}


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------


def scan_submitted_burns(
    client: Any,
    chain: Chain,
    adapter: str,
    from_block: int,
    to_block: int,
) -> tuple[set[bytes], int]:
    """Collect ``burnShares`` calldata from Submit logs in ``[from_block, to_block]``.

    Scans in chain-sized chunks and stops after ``MAX_SUBMIT_LOG_REQUESTS_PER_RUN``
    requests, returning the calldata found and the last block actually scanned so
    the next run resumes there.
    """
    span = SUBMIT_LOG_BLOCK_RANGE_BY_CHAIN.get(chain, DEFAULT_SUBMIT_LOG_BLOCK_RANGE)
    found: set[bytes] = set()
    scanned_to = from_block - 1
    for _ in range(MAX_SUBMIT_LOG_REQUESTS_PER_RUN):
        if scanned_to >= to_block:
            break
        start = scanned_to + 1
        end = min(start + span - 1, to_block)
        logs = client.eth.get_logs(
            {"address": adapter, "fromBlock": start, "toBlock": end, "topics": [SUBMIT_TOPIC, BURN_SHARES_TOPIC]}
        )
        for log in logs:
            data, _executable_at = abi_decode(["bytes", "uint256"], bytes(log["data"]))
            found.add(bytes(data))
        scanned_to = end
    return found, scanned_to


def load_adapter_state(
    client: Any,
    chain: Chain,
    adapter_address: str,
    creation_block: int,
    block_number: int,
) -> AdapterTimelockState:
    """Read ``timelock(burnShares)`` and every pending burn of one adapter."""
    adapter = client.eth.contract(address=Web3.to_checksum_address(adapter_address), abi=ABI_MARKET_V1_ADAPTER_V2)
    fns = adapter.functions
    length, burn_timelock, morpho_address = _execute(
        client, [fns.marketIdsLength(), fns.timelock(BURN_SHARES_SELECTOR), fns.morpho()], block_number
    )
    market_ids: List[bytes] = _execute(client, [fns.marketIds(i) for i in range(length)], block_number)

    cursor = _read_int(_key(adapter.address, "cursor", SUBMIT_LOG_CURSOR_TYPE))
    from_block = max(creation_block, cursor - LOG_OVERLAP_BLOCKS) if cursor else creation_block
    scan_error = ""
    try:
        discovered, scanned_to = scan_submitted_burns(client, chain, adapter.address, from_block, block_number)
    except Exception as e:
        # Keep the canonical/tracked checks rather than losing the adapter entirely;
        # the caller fails the run on ``scan_error``.
        logger.exception("Submit log scan failed for adapter %s on %s", adapter.address, chain.name)
        discovered, scanned_to = set(), (cursor or creation_block - 1)
        scan_error = f"{type(e).__name__}: {str(e)[:200]}"

    candidates = sorted(
        {burn_shares_calldata(m) for m in market_ids} | _tracked_data(adapter.address) | discovered,
    )
    # Shorter calldata can't decode the bytes32 argument, so it can never execute.
    candidates = [data for data in candidates if len(data) >= BURN_SHARES_MIN_LENGTH]
    executable_at = _execute(client, [fns.executableAt(data) for data in candidates], block_number)
    pending_data = [data for data, at in zip(candidates, executable_at) if at > 0]

    burned_markets = sorted({data[4:BURN_SHARES_MIN_LENGTH] for data in pending_data})
    blue = client.eth.contract(address=morpho_address, abi=ABI_MORPHO_BLUE)
    values = _execute(
        client,
        [f for m in burned_markets for f in (fns.supplyShares(m), blue.functions.market(m))],
        block_number,
    )
    shares_by_market: Dict[bytes, tuple[int, int]] = {}
    for index, market_id in enumerate(burned_markets):
        shares, market = values[index * 2], values[index * 2 + 1]
        shares_by_market[market_id] = (shares, to_assets_down(shares, market[0], market[1]))

    pending = []
    for data, at in zip(candidates, executable_at):
        if at > 0:
            shares, assets = shares_by_market[data[4:BURN_SHARES_MIN_LENGTH]]
            pending.append(PendingBurn(data=data, executable_at=at, shares=shares, assets=assets))
    return AdapterTimelockState(
        adapter=adapter.address,
        burn_timelock=burn_timelock,
        pending_burns=pending,
        scanned_to_block=scanned_to,
        lag_blocks=block_number - scanned_to,
        scan_error=scan_error,
    )


def load_adapter_states(chain: Chain, adapters: Dict[str, int]) -> List[AdapterTimelockState]:
    """Load every ``MorphoMarketV1AdapterV2`` of one vault, given ``{adapter: creation block}``."""
    if not adapters:
        return []
    client = ChainManager.get_client(chain)
    block_number = int(client.eth.block_number)
    return [
        load_adapter_state(client, chain, adapter, creation_block, block_number)
        for adapter, creation_block in sorted(adapters.items())
    ]


# ----------------------------------------------------------------------------
# Diff and alerting
# ----------------------------------------------------------------------------


def _address_link(address: str, chain: Chain) -> str:
    base = chain.explorer_url
    return f"[{address}]({base}/address/{address})" if base else address


def _burn_body(
    chain: Chain,
    adapter: str,
    burn: PendingBurn,
    total_assets: int | None,
    *,
    reminder: bool,
) -> str:
    metadata = fetch_market_metadata(burn.market_id, chain)
    if metadata:
        market = f"[{metadata['name']}]({get_market_url(burn.market_id, chain)})"
        decimals, symbol = metadata["loan_decimals"], metadata["loan_symbol"]
    else:
        market = f"[{burn.market_id}]({get_market_url(burn.market_id, chain)})"
        decimals, symbol = None, None
    title = "⏳ Reminder: adapter share burn is close" if reminder else "🔴 Adapter share burn submitted"
    lines = [
        f"{title}: `burnShares` on market {market}",
        f"🧩 Adapter: {_address_link(adapter, chain)}",
        f"💸 At risk: {format_amount_at_risk(burn.assets, decimals, symbol, total_assets)}",
        f"⏰ Executable at: {format_ts(burn.executable_at)} {format_countdown(burn.executable_at)}",
    ]
    if burn.extra_bytes > 0:
        lines.append(
            f"⚠️ Submitted with non-canonical calldata ({burn.extra_bytes} trailing bytes), "
            f"so revoke must use the exact data: `0x{burn.data.hex()}`"
        )
    lines.append(
        "Burning the shares writes off the adapter's supply in this market. "
        "If unintended, the curator or a sentinel can call `revoke(data)` on the adapter."
    )
    return "\n".join(lines)


def _diff_burn_timelock(chain: Chain, state: AdapterTimelockState, diff: VaultDiff) -> None:
    """Alert HIGH when ``timelock(burnShares)`` drops; the first run seeds the cache silently."""
    cache_key = _key(state.adapter, "burn_timelock", BURN_TIMELOCK_TYPE)
    last = _read_str(cache_key)
    if last and state.burn_timelock < int(last):
        diff.alert(
            AlertSeverity.HIGH,
            f"🚨 Adapter {_address_link(state.adapter, chain)} `burnShares` timelock decreased: "
            f"{format_timelock(int(last))} → {format_timelock(state.burn_timelock)}",
        )
    if last != str(state.burn_timelock):
        diff.defer(_write, cache_key, state.burn_timelock)


def _diff_pending_burns(chain: Chain, total_assets: int | None, state: AdapterTimelockState, diff: VaultDiff) -> None:
    """Alert on new pending burns (CRITICAL if the adapter holds shares), remind near the deadline, note resolutions."""
    now = int(datetime.now().timestamp())
    current: Dict[str, str] = {}  # data key → market id
    for burn in state.pending_burns:
        current[burn.data_key] = burn.market_id
        cache_key = _key(state.adapter, burn.data_key, PENDING_BURN_TYPE)
        reminder_key = _key(state.adapter, burn.data_key, PENDING_BURN_REMINDER_TYPE)
        in_window = now >= burn.executable_at - REMINDER_WINDOW_SECONDS
        if _read_int(cache_key) == burn.executable_at:
            if burn.shares > 0 and in_window and _read_int(reminder_key) != burn.executable_at:
                diff.loss(_burn_body(chain, state.adapter, burn, total_assets, reminder=True))
                diff.defer(_write, reminder_key, burn.executable_at)
            continue
        if burn.shares > 0:
            diff.loss(_burn_body(chain, state.adapter, burn, total_assets, reminder=False))
            if in_window:
                diff.defer(_write, reminder_key, burn.executable_at)
        else:
            diff.alert(
                AlertSeverity.MEDIUM,
                f"🔥 Adapter {_address_link(state.adapter, chain)} submitted `burnShares` on market "
                f"[{burn.market_id}]({get_market_url(burn.market_id, chain)}) with no shares in it; "
                f"executable at {format_ts(burn.executable_at)} {format_countdown(burn.executable_at)}",
            )
        diff.defer(_write, cache_key, burn.executable_at)

    index_key = _key(state.adapter, "pending_burns", PENDING_BURN_INDEX_TYPE)
    previous = dict(item.split(":", 1) for item in _read_str(index_key).split(",") if ":" in item)
    for data_key in sorted(set(previous) - set(current)):
        market_id = previous[data_key]
        cache_key = _key(state.adapter, data_key, PENDING_BURN_TYPE)
        executable_at = _read_int(cache_key)
        verb = "executed" if 0 < executable_at <= now else "revoked"
        diff.alert(
            AlertSeverity.LOW,
            f"{'✅' if verb == 'executed' else '🛑'} Pending `burnShares` on market "
            f"[{market_id}]({get_market_url(market_id, chain)}) of adapter "
            f"{_address_link(state.adapter, chain)} was {verb}.",
        )
        # Clear the cursor so a later re-submit with the same executableAt still alerts.
        diff.defer(_write, cache_key, 0)
    diff.defer(_write, index_key, ",".join(f"{k}:{m}" for k, m in sorted(current.items())))

    # Keep every pending calldata verbatim so it is re-read even once its Submit
    # log is behind the cursor or its market leaves the adapter's held list.
    tracked = ",".join(sorted(burn.data.hex() for burn in state.pending_burns))
    diff.defer(_write, _key(state.adapter, "tracked", TRACKED_BURN_DATA_TYPE), tracked)
    diff.defer(_write, _key(state.adapter, "cursor", SUBMIT_LOG_CURSOR_TYPE), state.scanned_to_block)


def diff_adapter(chain: Chain, total_assets: int | None, state: AdapterTimelockState, diff: VaultDiff) -> None:
    """Diff one adapter's on-chain timelock state against the cache into the vault's ``diff``."""
    _diff_burn_timelock(chain, state, diff)
    _diff_pending_burns(chain, total_assets, state, diff)
