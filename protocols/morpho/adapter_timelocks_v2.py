"""On-chain checks of ``MorphoMarketV1AdapterV2``'s own timelock.

The adapter's ``burnShares(marketId)`` is the V2 equivalent of a V1
``submitMarketRemoval``: once executed it zeroes the adapter's supply shares in
that market, so ``realAssets`` drops and the vault takes the loss. It is
timelocked on the adapter itself, which the Morpho GraphQL API does not expose.

Only batched ``eth_call`` reads are used, never ``eth_getLogs``:

* **Pending burns** — ``executableAt(abi.encodeCall(burnShares, (id)))`` for every
  held market. Limitation: ``executableAt`` is keyed by the exact calldata and
  Solidity ignores trailing bytes, so a burn submitted with padded calldata
  can't be found before it executes without reading ``Submit`` events.
* **Executed burns** — catch the effect, whatever calldata was used.
  ``allocate``/``deallocate`` keep the adapter's ``supplyShares[id]`` exactly equal
  to its Morpho Blue position; ``burnShares`` zeroes the former and leaves the
  latter. Morpho shares the adapter no longer accounts for were written off (or,
  rarely, donated by supplying on its behalf).
* ``timelock(burnShares)`` — a shortened reaction window, after the fact.

Amounts use the adapter's stored shares and Morpho Blue's stored market totals,
never ``expectedSupplyAssets``: that goes through the market's IRM, which may
revert, and burning shares of such a market is explicitly supported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List

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

# Cache value-type tags used with utils.cache.morpho_key.
BURN_TIMELOCK_TYPE = "v2_adapter_burn_timelock"
PENDING_BURN_TYPE = "v2_adapter_pending_burn"
PENDING_BURN_REMINDER_TYPE = "v2_adapter_pending_burn_reminder"
PENDING_BURN_INDEX_TYPE = "v2_adapter_pending_burn_index"
KNOWN_MARKETS_TYPE = "v2_adapter_known_markets"
WRITTEN_OFF_TYPE = "v2_adapter_written_off_shares"


@dataclass(frozen=True)
class MarketBalance:
    """The adapter's accounted shares vs. its actual Morpho Blue position in one market."""

    market_id: str  # 0x-prefixed hex
    adapter_shares: int  # adapter.supplyShares(id)
    morpho_shares: int  # MorphoBlue.position(id, adapter).supplyShares
    total_supply_assets: int
    total_supply_shares: int

    @property
    def unaccounted_shares(self) -> int:
        """Morpho shares the adapter no longer counts: written off by ``burnShares`` (or donated)."""
        return max(self.morpho_shares - self.adapter_shares, 0)

    def assets(self, shares: int) -> int:
        """Value ``shares`` at the market's stored totals (no IRM call)."""
        return to_assets_down(shares, self.total_supply_assets, self.total_supply_shares)


@dataclass(frozen=True)
class PendingBurn:
    """A submitted, not yet executed canonical ``burnShares`` for one market."""

    market_id: str  # 0x-prefixed hex
    executable_at: int
    shares: int  # the adapter's stored supply shares in the market
    assets: int  # those shares valued at Morpho Blue's stored market totals


@dataclass(frozen=True)
class AdapterTimelockState:
    """On-chain timelock state of one ``MorphoMarketV1AdapterV2``."""

    adapter: str  # checksummed
    burn_timelock: int
    pending_burns: List[PendingBurn] = field(default_factory=list)
    balances: List[MarketBalance] = field(default_factory=list)


def burn_shares_calldata(market_id: bytes) -> bytes:
    """Canonical calldata of ``burnShares(marketId)``, the key of the adapter's ``executableAt`` mapping."""
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


def _known_markets(adapter: str) -> set[bytes]:
    """Markets the adapter held on earlier runs; a burned-then-deallocated market leaves ``marketIds``."""
    raw = _read_str(_key(adapter, "known", KNOWN_MARKETS_TYPE))
    return {bytes.fromhex(item) for item in raw.split(",") if item}


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------


def load_adapter_state(client: Any, adapter_address: str, block_number: int) -> AdapterTimelockState:
    """Read the burn timelock, canonical pending burns, and share accounting of one adapter."""
    adapter = client.eth.contract(address=Web3.to_checksum_address(adapter_address), abi=ABI_MARKET_V1_ADAPTER_V2)
    fns = adapter.functions
    length, burn_timelock, morpho_address = _execute(
        client, [fns.marketIdsLength(), fns.timelock(BURN_SHARES_SELECTOR), fns.morpho()], block_number
    )
    held: List[bytes] = _execute(client, [fns.marketIds(i) for i in range(length)], block_number)
    markets = sorted(set(held) | _known_markets(adapter.address))

    blue = client.eth.contract(address=morpho_address, abi=ABI_MORPHO_BLUE)
    per_market = 4
    responses = _execute(
        client,
        [
            f
            for m in markets
            for f in (
                fns.executableAt(burn_shares_calldata(m)),
                fns.supplyShares(m),
                blue.functions.position(m, adapter.address),
                blue.functions.market(m),
            )
        ],
        block_number,
    )

    pending: List[PendingBurn] = []
    balances: List[MarketBalance] = []
    for index, market_id in enumerate(markets):
        executable_at, adapter_shares, position, market = responses[index * per_market : (index + 1) * per_market]
        balance = MarketBalance(
            market_id=Web3.to_hex(market_id),
            adapter_shares=adapter_shares,
            morpho_shares=position[0],
            total_supply_assets=market[0],
            total_supply_shares=market[1],
        )
        balances.append(balance)
        if executable_at > 0:
            pending.append(
                PendingBurn(balance.market_id, executable_at, adapter_shares, balance.assets(adapter_shares))
            )
    return AdapterTimelockState(
        adapter=adapter.address, burn_timelock=burn_timelock, pending_burns=pending, balances=balances
    )


def load_adapter_states(chain: Chain, adapters: List[str]) -> List[AdapterTimelockState]:
    """Load the timelock state of every ``MorphoMarketV1AdapterV2`` of one vault."""
    if not adapters:
        return []
    client = ChainManager.get_client(chain)
    block_number = int(client.eth.block_number)
    return [load_adapter_state(client, adapter, block_number) for adapter in adapters]


# ----------------------------------------------------------------------------
# Diff and alerting
# ----------------------------------------------------------------------------


def _address_link(address: str, chain: Chain) -> str:
    base = chain.explorer_url
    return f"[{address}]({base}/address/{address})" if base else address


def _market_label(market_id: str, chain: Chain) -> tuple[str, int | None, str | None]:
    """Return ``(markdown market link, loan decimals, loan symbol)``."""
    metadata = fetch_market_metadata(market_id, chain)
    if metadata:
        return (
            f"[{metadata['name']}]({get_market_url(market_id, chain)})",
            metadata["loan_decimals"],
            metadata["loan_symbol"],
        )
    return f"[{market_id}]({get_market_url(market_id, chain)})", None, None


def _burn_body(
    chain: Chain,
    adapter: str,
    burn: PendingBurn,
    total_assets: int | None,
    *,
    reminder: bool,
) -> str:
    market, decimals, symbol = _market_label(burn.market_id, chain)
    title = "⏳ Reminder: adapter share burn is close" if reminder else "🔴 Adapter share burn submitted"
    return (
        f"{title}: `burnShares` on market {market}\n"
        f"🧩 Adapter: {_address_link(adapter, chain)}\n"
        f"💸 At risk: {format_amount_at_risk(burn.assets, decimals, symbol, total_assets)}\n"
        f"⏰ Executable at: {format_ts(burn.executable_at)} {format_countdown(burn.executable_at)}\n"
        "Burning the shares writes off the adapter's supply in this market. "
        "If unintended, the curator or a sentinel can call `revoke(data)` on the adapter."
    )


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
    current: set[str] = set()
    for burn in state.pending_burns:
        current.add(burn.market_id)
        cache_key = _key(state.adapter, burn.market_id, PENDING_BURN_TYPE)
        reminder_key = _key(state.adapter, burn.market_id, PENDING_BURN_REMINDER_TYPE)
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
    previous = {m for m in _read_str(index_key).split(",") if m}
    for market_id in sorted(previous - current):
        cache_key = _key(state.adapter, market_id, PENDING_BURN_TYPE)
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
    diff.defer(_write, index_key, ",".join(sorted(current)))


def _diff_written_off(chain: Chain, total_assets: int | None, state: AdapterTimelockState, diff: VaultDiff) -> None:
    """Alert CRITICAL when the adapter stops accounting for Morpho shares it still holds.

    This catches an executed ``burnShares`` whatever calldata it was submitted
    with, including padded calldata the pending check can't see. Alerts once per
    increase of the unaccounted amount.
    """
    for balance in state.balances:
        cache_key = _key(state.adapter, balance.market_id, WRITTEN_OFF_TYPE)
        last = _read_int(cache_key)
        unaccounted = balance.unaccounted_shares
        if unaccounted > last:
            market, decimals, symbol = _market_label(balance.market_id, chain)
            amount = format_amount_at_risk(balance.assets(unaccounted), decimals, symbol, total_assets)
            diff.loss(
                f"🔴 Adapter supply written off on market {market}\n"
                f"🧩 Adapter: {_address_link(state.adapter, chain)}\n"
                f"💸 Lost: {amount}\n"
                f"The adapter accounts for {balance.adapter_shares:,} shares but holds {balance.morpho_shares:,} "
                "on Morpho Blue: shares were burned (`burnShares` executed) or, rarely, donated to the adapter."
            )
        if unaccounted != last:
            diff.defer(_write, cache_key, unaccounted)

    # Remember every market with a position so a burned market stays checked after it leaves ``marketIds``.
    known = sorted(
        b.market_id.removeprefix("0x") for b in state.balances if b.adapter_shares > 0 or b.morpho_shares > 0
    )
    diff.defer(_write, _key(state.adapter, "known", KNOWN_MARKETS_TYPE), ",".join(known))


def diff_adapter(chain: Chain, total_assets: int | None, state: AdapterTimelockState, diff: VaultDiff) -> None:
    """Diff one adapter's on-chain timelock state against the cache into the vault's ``diff``."""
    _diff_burn_timelock(chain, state, diff)
    _diff_pending_burns(chain, total_assets, state, diff)
    _diff_written_off(chain, total_assets, state, diff)
