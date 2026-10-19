"""Resolve Yearn V3 vault context for strategy-management calls.

A timelock batch that wires strategies into Yearn V3 vaults arrives at the LLM
as ``add_strategy(address,bool)`` and ``update_max_debt_for_strategy(address,
uint256)`` with raw integers, and none of the facts a reviewer needs:

- what unit ``max_debt`` is in (the vault's asset — so 500e18 on a WETH vault
  is 500 WETH, not "units unconfirmed"),
- what the bool means (``add_to_queue``: whether the strategy joins the
  default withdrawal queue),
- how the cap compares to the vault's size and to its other strategies, and
- whether funds move now (only ``update_debt`` moves funds).

Every V3 vault is an EIP-1167 clone of a verified Vyper implementation, and
the same vault code is governed by Yearn and by third-party curators alike, so
this adapter keys on the call shape and an on-chain ``apiVersion()`` of 3.x
rather than on the alert's protocol or a hard-coded address list.

Debt is usually moved through a ``DebtAllocator``, whose
``update_debt(vault, strategy, target_debt)`` forwards to the vault; those calls
are read as the vault's own. A batch can move debt between vaults that hold one
another — yvWETH-2 redeeming from yvWETH-1, which pulls from its own queue — so
every vault is walked in one global batch order, each flow is applied to the
inner vault too, and an allocator's look-through exposure is shown before →
after. Without that, a report called moving 270 WETH from yvWETH-1 into a
looper a shift "from indirect to direct exposure", when yvWETH-2's looper
exposure went from 70 to 295 WETH.
"""

from dataclasses import dataclass, field, replace
from typing import Any

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.eth_view import call_view
from utils.formatting import format_asset_amount
from utils.llm.report import address_link
from utils.llm.yearn_v3_batch import DebtMove, VaultBatchState
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.yearn_v3_context")

MAX_UINT256 = 2**256 - 1
# Vault-side constant: `default_queue` holds at most this many strategies.
MAX_QUEUE = 10

# Vault functions this adapter explains. Anything else on a vault is left to
# the generic pipeline.
VAULT_CALLS = frozenset(
    {
        "add_strategy",
        "revoke_strategy",
        "force_revoke_strategy",
        "update_max_debt_for_strategy",
        "update_debt",
        "set_default_queue",
        "set_use_default_queue",
        "set_deposit_limit",
        "set_minimum_total_idle",
    }
)
# DebtAllocator.update_debt(vault, strategy, target_debt): keeper-gated, forwards to vault.update_debt.
ALLOCATOR_UPDATE_DEBT = "update_debt(address,address,uint256)"
# Look-through exposure lists at most this many strategies.
MAX_EXPOSURE_LINES = 8


def _fn(name: str, outputs: list[str], inputs: list[str] | None = None) -> dict:
    """Minimal ABI entry for a view function."""
    return {
        "name": name,
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": t} for t in inputs or []],
        "outputs": [{"name": "", "type": t} for t in outputs],
    }


# The subset of the V3 vault ABI read here, identical across 3.0.x and 3.1.x.
# `strategies()` returns a static struct, which ABI-encodes the same as four
# separate uint256 outputs.
_VAULT_ABI = [
    _fn("apiVersion", ["string"]),
    _fn("asset", ["address"]),
    _fn("name", ["string"]),
    _fn("symbol", ["string"]),
    _fn("totalAssets", ["uint256"]),
    _fn("totalDebt", ["uint256"]),
    _fn("totalIdle", ["uint256"]),
    _fn("get_default_queue", ["address[]"]),
    _fn("isShutdown", ["bool"]),
    _fn("deposit_limit", ["uint256"]),
    _fn("minimum_total_idle", ["uint256"]),
    _fn("use_default_queue", ["bool"]),
    # Set when an external module decides each owner's withdraw limit.
    _fn("withdraw_limit_module", ["address"]),
    # 3.1+: when True, every deposit is pushed straight into the first default-queue strategy.
    _fn("auto_allocate", ["bool"]),
    _fn("strategies", ["uint256", "uint256", "uint256", "uint256"], ["address"]),
    # ERC-4626 limits, read on a strategy with the vault as owner: what
    # update_debt can actually deposit into / redeem from it.
    _fn("maxDeposit", ["uint256"], ["address"]),
    _fn("maxRedeem", ["uint256"], ["address"]),
    _fn("convertToAssets", ["uint256"], ["uint256"]),
]


@dataclass(frozen=True)
class StrategyState:
    """A strategy's registration on one vault, plus what it is."""

    address: str
    name: str | None
    activation: int
    current_debt: int
    max_debt: int
    # Read only for strategies the transaction names; None when unreadable.
    asset: str | None = None
    total_assets: int | None = None
    # Set when the strategy is itself a V3 vault (an allocator), with the names
    # of the strategies it allocates to.
    is_vault: bool = False
    sub_strategies: tuple[str, ...] = ()
    sub_strategy_addresses: tuple[str, ...] = ()
    # The allocator vault forwards deposits to its first queue strategy; None when unreadable.
    auto_allocate: bool | None = None
    # ERC-4626 limits with the vault as owner, read only for named strategies;
    # None when unreadable. They bound what update_debt can move.
    max_deposit: int | None = None
    max_withdraw: int | None = None

    @property
    def active(self) -> bool:
        """Whether the vault has registered this strategy (``activation != 0``)."""
        return self.activation != 0

    @property
    def display(self) -> str:
        """Name for prose, falling back to the address."""
        return self.name or self.address


@dataclass(frozen=True)
class YearnV3VaultContext:
    """On-chain state of one V3 vault and the calls this transaction makes on it."""

    vault_address: str
    name: str
    symbol: str
    api_version: str
    asset_address: str
    asset_symbol: str
    asset_decimals: int
    total_assets: int
    total_debt: int
    total_idle: int
    is_shutdown: bool
    deposit_limit: int | None
    minimum_total_idle: int | None
    use_default_queue: bool | None
    default_queue: tuple[StrategyState, ...]
    # Strategies the calls name that are not in the default queue.
    other_strategies: tuple[StrategyState, ...]
    calls: tuple[DecodedCall, ...] = field(default_factory=tuple)
    # 3.1+: deposits go straight to the first default-queue strategy; None when unreadable.
    auto_allocate: bool | None = None
    # Non-zero when an external module sets each owner's withdraw limit; None when unreadable or unset.
    withdraw_limit_module: str | None = None
    # DebtAllocators the update_debt calls come through, with their maxDebtUpdateLoss (bps; None if unread).
    debt_allocators: tuple[tuple[str, int | None], ...] = ()
    # Set by the resolver's global batch walk; None renders this vault's calls on their own.
    walked_lines: tuple[str, ...] | None = None
    # Batch outcome: exit liquidity, look-through exposure and DebtAllocator config, set by the resolver.
    outcome_lines: tuple[str, ...] = ()

    @property
    def addresses(self) -> list[str]:
        """Addresses this context introduces to the report."""
        return [
            self.vault_address,
            self.asset_address,
            *(s.address for s in self.default_queue),
            *(s.address for s in self.other_strategies),
        ]

    @property
    def labels(self) -> dict[str, str]:
        """Address labels for the vault, its asset, and its strategies."""
        labels = {self.vault_address: f"{self.name} ({self.symbol})", self.asset_address: self.asset_symbol}
        for strategy in (*self.default_queue, *self.other_strategies):
            if strategy.name:
                labels[strategy.address] = strategy.name
        return labels

    def strategy(self, address: str) -> StrategyState | None:
        """State of a strategy on this vault, by address (case-insensitive)."""
        wanted = address.lower()
        return next(
            (s for s in (*self.default_queue, *self.other_strategies) if s.address.lower() == wanted),
            None,
        )

    def amount(self, raw: int) -> str:
        """Render an asset-denominated amount with two truncated decimals."""
        if raw == MAX_UINT256:
            return "unlimited (max uint256)"
        return format_asset_amount(raw, self.asset_decimals, self.asset_symbol)

    def share_of_vault(self, raw: int) -> str:
        """Express an asset amount relative to the vault's totalAssets."""
        if raw == MAX_UINT256:
            return "no cap"
        if self.total_assets == 0:
            return "vault totalAssets is 0"
        ratio = raw / self.total_assets
        if ratio >= 1:
            return f"{ratio:.1f}× vault totalAssets"
        return f"{ratio * 100:.1f}% of vault totalAssets"

    def units_line(self) -> str:
        """The vault's unit for every debt and limit value, verified from its asset."""
        return (
            f"Units: max_debt, target_debt, deposit_limit and minimum_total_idle on this vault are denominated in "
            f"its asset {self.asset_symbol} ({self.asset_decimals} decimals) — verified, do not hedge about units."
        )

    def proposal_lines(self) -> list[str]:
        """One line per call, in batch order, each against the state the earlier calls leave."""
        if self.walked_lines is not None:
            return list(self.walked_lines)
        lines: list[str] = []
        added: set[str] = set()
        state = self.batch_state()
        queue = [s.address.lower() for s in self.default_queue]
        for call in self.calls:
            line = self._proposal_line(call, added, state, queue)
            if line:
                lines.append(line)
        return lines

    def batch_state(self) -> VaultBatchState:
        """A running copy of the vault's debt accounting, before the first call."""
        state = VaultBatchState(
            idle=self.total_idle,
            minimum_total_idle=self.minimum_total_idle or 0,
            shutdown=self.is_shutdown,
        )
        for strategy in (*self.default_queue, *self.other_strategies):
            if strategy.active:
                state.add_strategy(strategy.address, strategy.max_deposit, strategy.max_withdraw)
                state.set_max_debt(strategy.address, strategy.max_debt)
                state.strategies[strategy.address.lower()].current_debt = strategy.current_debt
        return state

    def _proposal_line(self, call: DecodedCall, added: set[str], state: VaultBatchState, queue: list[str]) -> str:
        """Describe one call against the vault state and default queue the earlier calls leave."""
        params = call.params
        name = call.function_name
        first = params[0][1] if params else None
        strategy = self.strategy(first) if isinstance(first, str) and first.startswith("0x") else None
        label = strategy.display if strategy else str(first)

        if name == "add_strategy" and strategy:
            add_to_queue = bool(params[1][1]) if len(params) > 1 else True
            added.add(strategy.address.lower())
            if not strategy.active:
                state.add_strategy(strategy.address, strategy.max_deposit, strategy.max_withdraw)
            status = "ALREADY ACTIVE — the call reverts" if strategy.active else "not yet active"
            parts = [f"add_strategy({label}): {status}"]
            if strategy.asset is not None:
                matches = strategy.asset.lower() == self.asset_address.lower()
                parts.append(
                    f"strategy asset {'matches' if matches else 'DOES NOT match — the call reverts on'} "
                    f"the vault asset {self.asset_symbol}"
                )
            if add_to_queue:
                queue_len = len(queue)
                parts.append(
                    f"add_to_queue=True: appended to the default queue (holds {queue_len}/{MAX_QUEUE} before this call)"
                    if queue_len < MAX_QUEUE
                    else f"add_to_queue=True but the default queue is full ({MAX_QUEUE}), so it is NOT appended"
                )
                if queue_len < MAX_QUEUE and not strategy.active:
                    queue.append(strategy.address.lower())
            else:
                withdrawals = (
                    "use_default_queue is True, so no withdrawal can pull from it — only update_debt recalls funds"
                    if self.use_default_queue
                    else "default-queue withdrawals will not pull from it unless a withdrawer passes a custom queue"
                )
                parts.append(
                    f"add_to_queue=False: stays OUT of the default queue — it can receive debt via update_debt; "
                    f"{withdrawals}"
                )
            if strategy.is_vault:
                parts.extend(self._allocator_notes(strategy))
            elif strategy.total_assets is not None:
                parts.append(f"strategy totalAssets {self.amount(strategy.total_assets)}")
            return "; ".join(parts) + "."

        if name == "update_max_debt_for_strategy" and strategy and len(params) > 1:
            new_max = int(params[1][1])
            running = state.get(strategy.address)
            before = "added earlier in this batch" if strategy.address.lower() in added else None
            current = before or (
                f"current max_debt {self.amount(running.max_debt)}, current_debt {self.amount(running.current_debt)}"
                if running is not None and running.active
                else "strategy not active"
            )
            state.set_max_debt(strategy.address, new_max)
            parts = [f"update_max_debt_for_strategy({label}): {self.amount(new_max)} ({current})"]
            parts.append(f"= {self.share_of_vault(new_max)} ({self.amount(self.total_assets)})")
            if new_max != MAX_UINT256 and new_max > self.total_assets:
                parts.append("a ceiling above the vault's size — update_debt can only allocate what the vault holds")
            peers = {s.max_debt for s in self.default_queue if s.address.lower() != strategy.address.lower()}
            if peers == {new_max}:
                parts.append("same max_debt as every other default-queue strategy")
            if self.deposit_limit is not None and new_max == self.deposit_limit:
                parts.append("equal to the vault's deposit_limit")
            return "; ".join(parts) + "."

        if name == "update_debt" and strategy and len(params) > 1:
            move = state.update_debt(strategy.address, int(params[1][1]))
            return self._update_debt_line(label, move, strategy.address.lower() in queue)

        if name in ("revoke_strategy", "force_revoke_strategy") and strategy:
            running = state.get(strategy.address)
            debt = running.current_debt if running is not None else strategy.current_debt
            state.revoke(strategy.address)
            if strategy.address.lower() in queue:
                queue.remove(strategy.address.lower())
            if name == "revoke_strategy":
                return (
                    f"revoke_strategy({label}): removes it; reverts unless current_debt is 0 (now {self.amount(debt)})."
                )
            return (
                f"force_revoke_strategy({label}): removes it and WRITES OFF its current_debt "
                f"{self.amount(debt)} as a loss to depositors ({self.share_of_vault(debt)})."
            )

        if name == "set_deposit_limit" and params:
            current = self.optional_amount(self.deposit_limit)
            return f"set_deposit_limit: {current} → {self.amount(int(params[0][1]))}."

        if name == "set_minimum_total_idle" and params:
            current = self.amount(state.minimum_total_idle)
            state.minimum_total_idle = int(params[0][1])
            return f"set_minimum_total_idle: {current} → {self.amount(int(params[0][1]))}."

        if name == "set_use_default_queue" and params:
            return f"set_use_default_queue: {self.use_default_queue} → {bool(params[0][1])}."

        if name == "set_default_queue" and params and isinstance(first, (list, tuple)):
            return self._set_queue_line([str(a).lower() for a in first], state, queue)

        return ""

    def _allocator_notes(self, strategy: StrategyState) -> list[str]:
        """What a strategy that is itself a V3 vault does with the debt it receives."""
        subs = ", ".join(strategy.sub_strategies) or "no strategies"
        notes = [
            f"the strategy is itself a Yearn V3 vault (totalAssets {self.optional_amount(strategy.total_assets)}"
            f"; allocates to: {subs})"
        ]
        if strategy.auto_allocate and strategy.sub_strategies:
            notes.append(
                f"its auto_allocate is True, so every deposit into it is forwarded at once to its first "
                f"queue strategy {strategy.sub_strategies[0]}"
            )
        shared = [
            self._name_of(address)
            for address in strategy.sub_strategy_addresses
            if (held := self.strategy(address)) is not None and held.active
        ]
        if shared:
            notes.append(
                f"it allocates to {', '.join(shared)}, which this vault already funds directly — debt routed "
                "through it is an extra vault layer over the same exposure, not diversification"
            )
        return notes

    def _set_queue_line(self, new_queue: list[str], state: VaultBatchState, queue: list[str]) -> str:
        """Compare the new default queue with the one the earlier calls in the batch leave."""
        before = [self._name_of(a) for a in queue]
        after = [self._name_of(a) for a in new_queue]
        line = f"set_default_queue: [{', '.join(before)}] → [{', '.join(after)}]"
        if [s.address.lower() for s in self.default_queue] != queue:
            line += " (the queue before this call includes changes made earlier in this batch)"
        removed: list[str] = []
        for address in queue:
            if address in new_queue:
                continue
            running = state.get(address)
            debt = running.current_debt if running is not None and running.active else 0
            note = (
                f" — still holds {self.amount(debt)} of debt; ordinary withdrawals will no longer pull from it"
                if debt
                else ""
            )
            removed.append(f"{self._name_of(address)}{note}")
        queue[:] = new_queue
        return line + (f"; removed from the queue: {'; '.join(removed)}." if removed else ".")

    def _update_debt_line(self, label: str, move: DebtMove, in_queue: bool = True) -> str:
        """Expected effect of one update_debt, from the running batch state."""
        target = "max (allocate all available)" if move.target == MAX_UINT256 else self.amount(move.target)
        head = f"update_debt({label}) target_debt {target}"
        if move.reverts:
            return f"{head}: REVERTS — {move.reverts}."
        if move.moved == 0:
            reason = f" — limited by {move.limited_by}" if move.limited_by else ""
            return f"{head}: moves nothing{reason}; current_debt stays {self.amount(move.debt_before)}."

        direction = "deposits" if move.moved > 0 else "withdraws"
        limit = f", limited by {move.limited_by}" if move.limited_by else ""
        line = (
            f"{head}: MOVES FUNDS NOW — {direction} {self.amount(abs(move.moved))}{limit}; "
            f"current_debt {self.amount(move.debt_before)} → {self.amount(move.debt_after)}; "
            f"vault idle {self.amount(move.idle_before)} → {self.amount(move.idle_after)} (expected)."
        )
        if move.moved > 0 and not in_queue:
            reach = (
                "use_default_queue is True, so ordinary withdrawals cannot pull it — only update_debt recalls it"
                if self.use_default_queue
                else "ordinary withdrawals cannot pull it unless a withdrawer passes a custom queue"
            )
            line += (
                f" The strategy is not in the default queue: its debt after this call "
                f"({self.amount(move.debt_after)}, {self.share_of_vault(move.debt_after)}) is outside the "
                f"withdrawal path — {reach}."
            )
        return line

    def inflow_line(self, parent: str, amount: int, state: VaultBatchState, queue: list[str]) -> str:
        """This vault's side of a parent vault's deposit into it, applied to ``state``."""
        idle_before = state.idle
        first = queue[0] if self.auto_allocate and queue else None
        move = state.receive(amount, first)
        head = (
            f"{parent} deposits {self.amount(amount)} into this vault (through its update_debt earlier in this batch)"
        )
        line = f"{head}: vault idle {self.amount(idle_before)} → {self.amount(idle_before + amount)}"
        if move is None or first is None:
            return line + "."
        if move.moved > 0:
            limit = f", limited by {move.limited_by}" if move.limited_by else ""
            return (
                f"{line}; auto_allocate then deposits {self.amount(move.moved)} into {self._name_of(first)}{limit} "
                f"— current_debt {self.amount(move.debt_before)} → {self.amount(move.debt_after)}; vault idle "
                f"{self.amount(move.idle_after)}."
            )
        reason = f" — limited by {move.limited_by}" if move.limited_by else ""
        return f"{line}; auto_allocate moves nothing into {self._name_of(first)}{reason}."

    def outflow_line(self, parent: str, amount: int, state: VaultBatchState, queue: list[str]) -> str:
        """This vault's side of a parent vault's redemption from it, applied to ``state``."""
        from_idle, pulls, short = state.redeem(amount, queue)
        sources = [f"idle {self.amount(from_idle)}"] if from_idle else []
        sources.extend(
            f"{self._name_of(address)} current_debt {self.amount(before)} → {self.amount(after)}"
            for address, before, after in pulls
        )
        line = (
            f"{parent} redeems {self.amount(amount)} from this vault (through its update_debt earlier in this batch): "
        )
        line += (
            "served, in default-queue order, from " + "; ".join(sources) if sources else "no queue strategy serves it"
        )
        if short:
            line += f"; {self.amount(short)} could not be traced to a queue strategy with redeemable funds"
        return line + "."

    def optional_amount(self, raw: int | None) -> str:
        """Render an amount that may not have been readable on this vault version."""
        return "unreadable" if raw is None else self.amount(raw)

    def _name_of(self, address: str) -> str:
        """Strategy name when known on this vault, else the raw address."""
        strategy = self.strategy(address)
        return strategy.display if strategy else address


def _strategy_addresses(call: DecodedCall) -> list[str]:
    """Strategy addresses a vault call names: its first address, or an address[] queue."""
    if not call.params:
        return []
    type_str, value = call.params[0]
    if type_str == "address" and isinstance(value, str):
        return [value]
    if type_str == "address[]" and isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _optional_call(fn: Any) -> Any:
    """Call a view function, returning None if it reverts or doesn't exist."""
    try:
        return fn.call()
    except Exception:  # noqa: BLE001 - optional getter; absence is expected on some versions
        return None


def _token_name(chain_id: int, address: str) -> str | None:
    """On-chain ``name()`` of a vault/strategy share token, clone-aware and cached."""
    meta = fetch_erc20_metadata(chain_id, address)
    return meta.name if meta else None


def _read_strategy_details(client: Any, chain_id: int, address: str, vault: str) -> dict[str, Any]:
    """Asset, size, deposit/redeem limits for the vault, and — for an allocator vault — its own queue."""
    contract = client.get_contract(address, _VAULT_ABI)
    asset = _optional_call(contract.functions.asset())
    total_assets = _optional_call(contract.functions.totalAssets())
    sub_queue = _optional_call(contract.functions.get_default_queue())
    is_vault = isinstance(sub_queue, (list, tuple))
    sub_addresses = tuple(to_checksum_address(str(a)) for a in sub_queue) if is_vault else ()
    subs = tuple(_token_name(chain_id, a) or a for a in sub_addresses)
    auto_allocate = _optional_call(contract.functions.auto_allocate()) if is_vault else None
    max_deposit = _optional_call(contract.functions.maxDeposit(vault))
    max_redeem = _optional_call(contract.functions.maxRedeem(vault))
    max_withdraw = (
        _optional_call(contract.functions.convertToAssets(max_redeem)) if isinstance(max_redeem, int) else None
    )
    return {
        "asset": to_checksum_address(str(asset)) if isinstance(asset, str) else None,
        "total_assets": int(total_assets) if isinstance(total_assets, int) else None,
        "is_vault": is_vault,
        "sub_strategies": subs,
        "sub_strategy_addresses": sub_addresses,
        "auto_allocate": auto_allocate if isinstance(auto_allocate, bool) else None,
        "max_deposit": int(max_deposit) if isinstance(max_deposit, int) else None,
        "max_withdraw": int(max_withdraw) if isinstance(max_withdraw, int) else None,
    }


def _read_withdraw_limit(client: Any, address: str, vault: str) -> int | None:
    """``convertToAssets(maxRedeem(vault))`` on a strategy: what the vault can pull from it now."""
    contract = client.get_contract(address, _VAULT_ABI)
    max_redeem = _optional_call(contract.functions.maxRedeem(vault))
    if not isinstance(max_redeem, int):
        return None
    assets = _optional_call(contract.functions.convertToAssets(max_redeem))
    return int(assets) if isinstance(assets, int) else None


def _read_vault_context(
    chain_id: int, vault: str, calls: list[DecodedCall], queue_limits: bool = False
) -> YearnV3VaultContext | None:
    """Read a V3 vault's state and the strategies its calls touch; None if not a V3 vault.

    ``queue_limits`` also reads what every default-queue strategy can return now,
    for a vault that a parent vault in the same batch redeems from.
    """
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    contract = client.get_contract(vault, _VAULT_ABI)
    try:
        with client.batch_requests() as batch:
            for getter in (
                "apiVersion",
                "asset",
                "name",
                "symbol",
                "totalAssets",
                "totalDebt",
                "totalIdle",
                "get_default_queue",
                "isShutdown",
            ):
                batch.add(contract.functions[getter]())
            (api_version, asset, name, symbol, total_assets, total_debt, total_idle, queue, is_shutdown) = (
                client.execute_batch(batch)
            )
    except Exception as error:  # noqa: BLE001 - not a V3 vault (or RPC down); skip quietly
        logger.debug("Not a readable Yearn V3 vault %s: %s", vault, error)
        return None
    if not str(api_version).startswith("3."):
        return None

    asset = to_checksum_address(str(asset))
    asset_meta = fetch_erc20_metadata(chain_id, asset)
    if asset_meta is None:
        logger.info("Yearn V3 vault %s: asset metadata unavailable for %s", vault, asset)
        return None

    queue_addresses = [to_checksum_address(str(a)) for a in queue]
    named = [to_checksum_address(a) for call in calls for a in _strategy_addresses(call)]
    others = [a for a in dict.fromkeys(named) if a not in queue_addresses]
    everyone = [*queue_addresses, *others]

    with client.batch_requests() as batch:
        for address in everyone:
            batch.add(contract.functions.strategies(address))
        params = client.execute_batch(batch) if everyone else []

    named_set = set(named)
    states: dict[str, StrategyState] = {}
    for address, (activation, _last_report, current_debt, max_debt) in zip(everyone, params):
        details: dict[str, Any] = (
            _read_strategy_details(client, chain_id, address, vault) if address in named_set else {}
        )
        if queue_limits and not details:
            details = {"max_withdraw": _read_withdraw_limit(client, address, vault)}
        states[address] = StrategyState(
            address=address,
            name=_token_name(chain_id, address),
            activation=int(activation),
            current_debt=int(current_debt),
            max_debt=int(max_debt),
            **details,
        )

    deposit_limit = _optional_call(contract.functions.deposit_limit())
    minimum_total_idle = _optional_call(contract.functions.minimum_total_idle())
    use_default_queue = _optional_call(contract.functions.use_default_queue())
    module = _optional_call(contract.functions.withdraw_limit_module())
    auto_allocate = _optional_call(contract.functions.auto_allocate())
    return YearnV3VaultContext(
        vault_address=vault,
        name=str(name),
        symbol=str(symbol),
        api_version=str(api_version),
        asset_address=asset,
        asset_symbol=asset_meta.symbol,
        asset_decimals=asset_meta.decimals,
        total_assets=int(total_assets),
        total_debt=int(total_debt),
        total_idle=int(total_idle),
        is_shutdown=bool(is_shutdown),
        deposit_limit=int(deposit_limit) if isinstance(deposit_limit, int) else None,
        minimum_total_idle=int(minimum_total_idle) if isinstance(minimum_total_idle, int) else None,
        use_default_queue=bool(use_default_queue) if isinstance(use_default_queue, bool) else None,
        default_queue=tuple(states[a] for a in queue_addresses),
        other_strategies=tuple(states[a] for a in others),
        calls=tuple(calls),
        auto_allocate=auto_allocate if isinstance(auto_allocate, bool) else None,
        withdraw_limit_module=(
            to_checksum_address(module) if isinstance(module, str) and int(module, 16) != 0 else None
        ),
    )


def _as_vault_call(target: str, call: DecodedCall) -> tuple[str, DecodedCall, str | None] | None:
    """(vault, vault-shaped call, DebtAllocator or None) for a call this adapter explains, else None."""
    if call.signature == ALLOCATOR_UPDATE_DEBT and len(call.params) == 3:
        vault = call.params[0][1]
        if not isinstance(vault, str):
            return None
        forwarded = DecodedCall("update_debt", "update_debt(address,uint256)", list(call.params[1:]))
        return to_checksum_address(vault), forwarded, to_checksum_address(target)
    if call.function_name in VAULT_CALLS:
        return to_checksum_address(target), call, None
    return None


@dataclass
class _Run:
    """One vault's running state during the global batch walk."""

    state: VaultBatchState
    queue: list[str]
    added: set[str] = field(default_factory=set)
    # Net assets parent vaults moved into (positive) or out of (negative) this vault.
    inflow: int = 0
    # Per inner vault: an owner-specific redemption limit observed before the batch, less what was redeemed.
    redeem_caps: dict[str, int] = field(default_factory=dict)
    # Inner vaults whose owner-specific limit a deposit in this batch made unknowable.
    unknown_limits: set[str] = field(default_factory=set)
    # Set once a call's amount is unknowable: from then on this vault's balances are not forecast.
    unforecast: str = ""


def walk_batch(
    contexts: dict[str, YearnV3VaultContext], ordered: list[tuple[str, DecodedCall]]
) -> tuple[dict[str, list[str]], dict[str, _Run]]:
    """Describe every call in global batch order, passing flows between vaults that hold one another.

    Args:
        contexts: Vault contexts keyed by lowercase address, including vaults no call targets
            but that a called vault holds as a strategy.
        ordered: (vault, vault-shaped call) in execution order.

    Returns:
        Lines per vault (lowercase address) and each vault's final running state.
    """
    runs = {
        key: _Run(context.batch_state(), [s.address.lower() for s in context.default_queue])
        for key, context in contexts.items()
    }
    for key in runs:
        _observe_redeem_caps(key, contexts, runs)
    lines: dict[str, list[str]] = {key: [] for key in contexts}
    for vault, call in ordered:
        key = vault.lower()
        context, run = contexts.get(key), runs.get(key)
        if context is None or run is None:
            continue
        inner_key = str(call.params[0][1]).lower() if call.params else ""
        inner, inner_run = contexts.get(inner_key), runs.get(inner_key)
        if run.unforecast:
            lines[key].append(_unforecast_line(context, call, run.unforecast))
            # The call still moves debt into or out of the inner vault, by an amount that is not known.
            if call.function_name == "update_debt" and inner is not None and inner_run is not None:
                inner_run.unforecast = inner_run.unforecast or run.unforecast
                lines[inner_key].append(
                    f"{context.name} ({context.symbol}) moves debt into or out of this vault by an amount that is "
                    f"not forecast: {run.unforecast}"
                )
            continue
        reason = _unknown_redemption(context, call, run, inner, inner_run)
        if reason:
            # Crediting a guessed amount would make every later balance in the batch a guess too.
            lines[key].append(f"{reason} This call and every balance after it in this batch are not forecast.")
            run.unforecast = reason
            if inner_run is not None:
                inner_run.unforecast = inner_run.unforecast or reason
            continue
        _refresh_inner_limits(key, contexts, runs)
        run.state.last_move = None
        line = context._proposal_line(call, run.added, run.state, run.queue)
        if line:
            lines[key].append(line)
        move = run.state.last_move
        if move is None or move.moved == 0 or inner is None or inner_run is None:
            continue
        parent = f"{context.name} ({context.symbol})"
        if inner_run.unforecast:
            lines[inner_key].append(
                f"{parent} moves {inner.amount(abs(move.moved))} {'into' if move.moved > 0 else 'out of'} this vault: "
                f"not forecast — {inner_run.unforecast}"
            )
            continue
        inner_run.inflow += move.moved
        if move.moved < 0 and inner_key in run.redeem_caps:
            run.redeem_caps[inner_key] = max(run.redeem_caps[inner_key] + move.moved, 0)
        elif move.moved > 0 and inner_key in run.redeem_caps:
            # How an owner-specific limit responds to a deposit is unknown: stop forecasting it.
            del run.redeem_caps[inner_key]
            run.unknown_limits.add(inner_key)
        if move.moved > 0:
            lines[inner_key].append(inner.inflow_line(parent, move.moved, inner_run.state, inner_run.queue))
        else:
            lines[inner_key].append(inner.outflow_line(parent, -move.moved, inner_run.state, inner_run.queue))
    for key in runs:
        _refresh_inner_limits(key, contexts, runs)
    return lines, runs


def _unknown_redemption(
    context: YearnV3VaultContext,
    call: DecodedCall,
    run: _Run,
    inner: YearnV3VaultContext | None,
    inner_run: _Run | None,
) -> str:
    """Why a withdrawal from an inner vault has an unknowable amount, or "".

    Either the vault's own withdraw limit there was a restriction that a deposit in this
    batch invalidated, or the inner vault's state is already unforecast.
    """
    if call.function_name != "update_debt" or inner is None or inner_run is None or len(call.params) < 2:
        return ""
    held = run.state.get(inner.vault_address)
    target = call.params[1][1]
    if held is None or not isinstance(target, int) or target >= held.current_debt:
        return ""
    head = (
        f"update_debt({inner.name}) target_debt {context.amount(target)} would withdraw up to "
        f"{context.amount(held.current_debt - target)}, but"
    )
    if inner.vault_address.lower() in run.unknown_limits:
        return (
            f"{head} {inner.name}'s withdraw limit for this vault was below its position before the batch and this "
            "batch has deposited into it since, so how much it can redeem is unknown."
        )
    if inner_run.unforecast:
        return f"{head} {inner.name}'s balances are not forecast at this point ({inner_run.unforecast})."
    return ""


def _unforecast_line(context: YearnV3VaultContext, call: DecodedCall, reason: str) -> str:
    """A call on a vault whose balances depend on an earlier call with an unknown amount."""
    first = call.params[0][1] if call.params else None
    subject = context._name_of(first) if isinstance(first, str) and first.startswith("0x") else ""
    return f"{call.function_name}({subject}): not forecast — an earlier call in this batch left an unknown amount."


def _refresh_inner_limits(key: str, contexts: dict[str, YearnV3VaultContext], runs: dict[str, _Run]) -> None:
    """Cap what vault ``key`` can redeem from each inner vault at that vault's exit liquidity now.

    The pre-batch ``convertToAssets(maxRedeem(vault))`` goes stale once an earlier
    call moves the inner vault's idle or queue debt: an inner vault that sent its
    idle to an off-queue strategy can no longer pay a parent's redemption. On-chain
    ``maxRedeem`` is the smaller of the position and the inner vault's liquidity,
    and a vault never withdraws more than ``current_debt``, so the limit becomes
    ``min(current_debt, inner exit liquidity)`` — further capped by any owner-specific
    limit observed before the batch (``_observe_redeem_caps``). Unknown inner limits
    keep the snapshot.
    """
    run = runs[key]
    for inner_key, inner_run in runs.items():
        held = run.state.get(inner_key) if inner_key != key else None
        if held is None or not held.active:
            continue
        liquidity = _exit_liquidity(contexts[inner_key], inner_run)
        if liquidity is None:
            continue
        limit = min(held.current_debt, liquidity)
        cap = run.redeem_caps.get(inner_key)
        held.max_withdraw = limit if cap is None else min(limit, cap)


def _observe_redeem_caps(key: str, contexts: dict[str, YearnV3VaultContext], runs: dict[str, _Run]) -> None:
    """Keep each pre-batch redemption limit that the position and liquidity do not explain.

    ``maxRedeem`` is owner-specific: a ``withdraw_limit_module`` can cap one holder
    below what the vault's liquidity allows, even to 0. Refreshing from liquidity alone
    would raise such a limit and predict a withdrawal that cannot happen. A snapshot
    below ``min(current_debt, inner exit liquidity)`` is kept as a cap for the batch.
    A snapshot that the position explains is not a restriction — an empty position
    reads 0 under any module — since ``current_debt`` already tracks the position.
    """
    run = runs[key]
    for inner_key in runs:
        held = run.state.get(inner_key) if inner_key != key else None
        if held is None or not held.active or held.max_withdraw is None:
            continue
        inner = contexts[inner_key]
        liquidity = _exit_liquidity(inner, None)
        explained = min(held.current_debt, liquidity) if liquidity is not None else None
        if explained is not None and held.max_withdraw < explained:
            run.redeem_caps[inner_key] = held.max_withdraw


def _strategy_debts(context: YearnV3VaultContext, run: _Run | None) -> dict[str, int]:
    """Current debt per strategy (lowercase address): after the batch when ``run`` is given."""
    debts: dict[str, int] = {}
    for strategy in (*context.default_queue, *context.other_strategies):
        key = strategy.address.lower()
        running = run.state.get(key) if run is not None else None
        debts[key] = running.current_debt if running is not None else strategy.current_debt
    return debts


def _exposure(
    context: YearnV3VaultContext,
    contexts: dict[str, YearnV3VaultContext],
    runs: dict[str, _Run] | None,
) -> dict[str, int]:
    """Assets per underlying strategy: direct debt plus a pro-rata share of each allocator vault held.

    A held vault's known strategies are its default queue plus those the batch names; it
    keeps no on-chain list of the rest. Its idle (``idle:<vault>``) and the remainder of its
    totalAssets (``unlisted:<vault>``) are kept as their own buckets, so the shares still
    add up to the position instead of silently dropping off-queue holdings.
    """
    exposure: dict[str, int] = {}
    own = _strategy_debts(context, runs.get(context.vault_address.lower()) if runs else None)
    for key, debt in own.items():
        inner = contexts.get(key)
        if inner is None:
            exposure[key] = exposure.get(key, 0) + debt
            continue
        run = runs.get(key) if runs else None
        inner_total = inner.total_assets + (run.inflow if run else 0)
        if inner_total <= 0:
            continue
        sub_debts = _strategy_debts(inner, run)
        idle = run.state.idle if run else inner.total_idle
        buckets = {**sub_debts, f"idle:{key}": idle, f"unlisted:{key}": inner_total - idle - sum(sub_debts.values())}
        for sub, sub_debt in buckets.items():
            if sub_debt > 0:
                exposure[sub] = exposure.get(sub, 0) + debt * sub_debt // inner_total
    return exposure


def _unlisted_holdings(inner: YearnV3VaultContext) -> int:
    """Pre-batch assets of a held vault in strategies neither in its default queue nor named by the batch."""
    known = sum(s.current_debt for s in (*inner.default_queue, *inner.other_strategies))
    return max(inner.total_assets - inner.total_idle - known, 0)


def exposure_lines(
    context: YearnV3VaultContext, contexts: dict[str, YearnV3VaultContext], runs: dict[str, _Run]
) -> list[str]:
    """Look-through exposure before → after, when the vault holds another vault the batch touches."""
    held = [
        contexts[s.address.lower()]
        for s in (*context.default_queue, *context.other_strategies)
        if s.address.lower() in contexts and s.address.lower() != context.vault_address.lower()
    ]
    if not held:
        return []
    before, after = _exposure(context, contexts, None), _exposure(context, contexts, runs)
    names: dict[str, str] = {}
    for vault in (context, *held):
        names.update({s.address.lower(): s.display for s in (*vault.default_queue, *vault.other_strategies)})
        key = vault.vault_address.lower()
        names[f"idle:{key}"] = f"idle in {vault.name}"
        names[f"unlisted:{key}"] = f"{vault.name} holdings outside its default queue and this batch"
    rows = sorted(
        (key for key in {*before, *after} if before.get(key, 0) != after.get(key, 0)),
        key=lambda key: abs(after.get(key, 0) - before.get(key, 0)),
        reverse=True,
    )[:MAX_EXPOSURE_LINES]
    if not rows:
        return []
    through = ", ".join(f"{v.name} ({v.symbol})" for v in held)

    def cell(raw: int) -> str:
        share = raw / context.total_assets * 100 if context.total_assets else 0.0
        return f"{context.amount(raw)} ({share:.1f}%)"

    entries = [f"{names.get(key, key)} {cell(before.get(key, 0))} → {cell(after.get(key, 0))}" for key in rows]
    line = (
        f"Look-through exposure of {context.name} ({context.symbol}) — direct debt plus its pro-rata share of "
        f"{through}, before → after this batch, as a share of its totalAssets (changed strategies only): "
        + "; ".join(entries)
        + "."
    )
    unlisted = [(vault, amount) for vault in held if (amount := _unlisted_holdings(vault))]
    if unlisted:
        line += " " + " ".join(
            f"{vault.name} holds {vault.amount(amount)} in strategies outside its default queue that this batch "
            "does not name; V3 vaults keep no on-chain list of them, so they appear as one bucket and may "
            "include strategies listed above — exposure to those is understated by up to that bucket."
            for vault, amount in unlisted
        )
    return [line]


def resolve_yearn_v3_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[YearnV3VaultContext]:
    """Resolve Yearn V3 vault context for every vault a strategy-management call targets.

    Calls on the vault and ``DebtAllocator.update_debt(vault, …)`` both count.
    ``protocol`` is unused: the same vault code is governed by many parties, so
    the adapter is selected by call shape and confirmed by ``apiVersion()``.
    """
    del protocol
    ordered: list[tuple[str, DecodedCall]] = []
    calls_by_vault: dict[str, list[DecodedCall]] = {}
    allocators: dict[str, dict[str, None]] = {}
    # (allocator, vault, strategy) for every allocator update_debt, in order.
    allocated: list[tuple[str, str, str]] = []
    for target, call in targets_and_calls:
        try:
            mapped = _as_vault_call(target, call)
        except ValueError:
            continue
        if mapped is None:
            continue
        vault, vault_call, allocator = mapped
        ordered.append((vault, vault_call))
        calls_by_vault.setdefault(vault, []).append(vault_call)
        if allocator:
            allocators.setdefault(vault, {})[allocator] = None
            allocated.append((allocator, vault, to_checksum_address(str(vault_call.params[0][1]))))
    if not calls_by_vault:
        return []

    named = {a.lower() for _, call in ordered for a in _strategy_addresses(call)}
    contexts: dict[str, YearnV3VaultContext] = {}
    for vault, calls in calls_by_vault.items():
        try:
            # What each queue strategy can return is read whenever debt moves, for exit liquidity.
            moves = any(call.function_name == "update_debt" for call in calls)
            context = _read_vault_context(chain_id, vault, calls, queue_limits=moves or vault.lower() in named)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Yearn V3 context resolution failed for %s: %s", vault, error)
            continue
        if context is not None:
            contexts[vault.lower()] = context
    if not contexts:
        return []

    # Vaults a called vault moves debt into or out of, read so their side of the flow is shown.
    support: dict[str, YearnV3VaultContext] = {}
    for context in list(contexts.values()):
        for strategy in (*context.default_queue, *context.other_strategies):
            key = strategy.address.lower()
            if strategy.is_vault and key in named and key not in contexts and key not in support:
                try:
                    inner = _read_vault_context(chain_id, strategy.address, [], queue_limits=True)
                except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
                    logger.info("Yearn V3 inner vault read failed for %s: %s", strategy.address, error)
                    continue
                if inner is not None:
                    support[key] = inner

    everything = {**contexts, **support}
    lines, runs = walk_batch(everything, ordered)
    losses = _allocator_losses(chain_id, {a for found in allocators.values() for a in found})
    configs = _allocator_configs(chain_id, allocated)
    return [
        replace(
            context,
            walked_lines=tuple(lines[key]),
            outcome_lines=(
                (f"Liquidity, exposure and debt ratios after this batch are not forecast: {runs[key].unforecast}",)
                if runs[key].unforecast
                else (
                    *liquidity_lines(context, runs.get(key)),
                    *exposure_lines(context, everything, runs),
                    *allocator_config_lines(context, runs.get(key), configs),
                )
            ),
            debt_allocators=tuple((a, losses.get(a)) for a in allocators.get(context.vault_address, {})),
        )
        for key, context in contexts.items()
    ]


# DebtAllocator.getStrategyConfig: (added, targetRatio bps, maxRatio bps, lastUpdate, open).
StrategyConfig = tuple[bool, int, int, int, int]


def _allocator_configs(
    chain_id: int, allocated: list[tuple[str, str, str]]
) -> dict[tuple[str, str], tuple[str, StrategyConfig | None]]:
    """Each allocator-moved (vault, strategy) pair's config, keyed by lowercase addresses; None when unreadable."""
    if not allocated:
        return {}
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    configs: dict[tuple[str, str], tuple[str, StrategyConfig | None]] = {}
    for allocator, vault, strategy in allocated:
        key = (vault.lower(), strategy.lower())
        if key in configs:
            continue
        value = call_view(
            client,
            allocator,
            "getStrategyConfig(address,address)",
            "(bool,uint16,uint16,uint96,uint96)",
            (vault, strategy),
        )
        config = tuple(value) if isinstance(value, tuple) and len(value) == 5 else None
        configs[key] = (allocator, config)  # type: ignore[assignment]
    return configs


def allocator_config_lines(
    context: YearnV3VaultContext,
    run: _Run | None,
    configs: dict[tuple[str, str], tuple[str, StrategyConfig | None]],
) -> list[str]:
    """How the DebtAllocator's own config for each moved strategy compares with the debt the batch leaves."""
    lines: list[str] = []
    debts = _strategy_debts(context, run)
    for (vault, strategy), (_allocator, config) in configs.items():
        if vault != context.vault_address.lower() or config is None:
            continue
        name = context._name_of(strategy)
        debt = debts.get(strategy, 0)
        if not config[0]:
            lines.append(
                f"DebtAllocator has no config for {name} on this vault (added = false): its automated debt updates "
                f"ignore the {context.amount(debt)} position — only a manual update_debt moves it."
            )
            continue
        target, maximum = config[1], config[2]
        total = context.total_assets + (run.inflow if run is not None else 0)
        ratio = debt / total * 10_000 if total > 0 else 0
        line = (
            f"DebtAllocator config for {name}: target {target / 100:g}% / max {maximum / 100:g}% of totalAssets; "
            f"after this batch its debt is {context.amount(debt)} ({ratio / 100:.1f}%)"
        )
        if ratio > maximum:
            line += " — above the max, so automated updates may pull it back"
        elif ratio < target:
            line += " — below the target, so automated updates may push idle back into it"
        lines.append(line + ".")
    return lines


def _exit_liquidity(context: YearnV3VaultContext, run: _Run | None) -> int | None:
    """Idle plus what each default-queue strategy can return now; None when a limit is unknown."""
    state = run.state if run is not None else context.batch_state()
    queue = run.queue if run is not None else [s.address.lower() for s in context.default_queue]
    total = state.idle
    for key in queue:
        strategy = state.get(key)
        if strategy is None or not strategy.active:
            continue
        if strategy.max_withdraw is None:
            return None
        total += min(strategy.current_debt, strategy.max_withdraw)
    return total


def liquidity_lines(context: YearnV3VaultContext, run: _Run | None) -> list[str]:
    """Exit liquidity through the default queue, before → after, when the batch changes it."""
    before, after = _exit_liquidity(context, None), _exit_liquidity(context, run)
    if before is None or after is None or before == after:
        return []

    def cell(raw: int, total: int) -> str:
        share = raw / total * 100 if total > 0 else 0.0
        return f"{context.amount(raw)} ({share:.1f}% of totalAssets)"

    total_after = context.total_assets + (run.inflow if run is not None else 0)

    return [
        f"Exit liquidity through the default queue (idle + what each queue strategy can return now; ordinary "
        f"withdraw/redeem cannot get more without a custom queue or loss): {cell(before, context.total_assets)} → "
        f"{cell(after, total_after)}. "
        "Strategies outside the queue and queue strategies that cannot return funds now do not count."
    ]


def _allocator_losses(chain_id: int, allocators: set[str]) -> dict[str, int | None]:
    """``maxDebtUpdateLoss()`` (bps) of each DebtAllocator; None when unreadable."""
    if not allocators:
        return {}
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    losses: dict[str, int | None] = {}
    for allocator in sorted(allocators):
        value = call_view(client, allocator, "maxDebtUpdateLoss()", "uint256")
        losses[allocator] = value if isinstance(value, int) else None
    return losses


def _queue_line(context: YearnV3VaultContext, strategy: StrategyState) -> str:
    """One default-queue entry for the prompt: name, address, debt and cap."""
    return f"{strategy.display} {strategy.address}: current_debt {context.amount(strategy.current_debt)} / max_debt {context.amount(strategy.max_debt)}"


def _allocator_lines(context: YearnV3VaultContext, render: Any) -> list[str]:
    """How the DebtAllocator(s) the calls go through reach the vault."""
    lines = []
    for allocator, loss in context.debt_allocators:
        loss_text = f"{loss} bps ({loss / 100:g}%)" if loss is not None else "unreadable"
        lines.append(
            f"update_debt calls on this vault come through DebtAllocator {render(allocator)}: keeper-gated, it "
            f"forwards to vault.update_debt(strategy, target_debt, maxDebtUpdateLoss = {loss_text}) and first runs "
            "process_report when the target is 0."
        )
    return lines


# How V3 vault calls behave, from the vault source; stated once per prompt, not per vault.
V3_SEMANTICS = (
    "Yearn V3 vault semantics: add_strategy(strategy, add_to_queue) registers the strategy with max_debt 0; "
    "add_to_queue=True appends it to the default queue (used for withdrawals), False leaves it out. "
    "update_max_debt_for_strategy only sets a ceiling: funds move only when a DEBT_MANAGER (typically a debt "
    "allocator) calls update_debt. update_debt(strategy, target_debt) is a TARGET, not an amount: a deposit is "
    "capped by the strategy's max_debt, its maxDeposit and the vault's idle above minimum_total_idle, so a target "
    "above what the vault holds simply allocates everything available — it is not a funding gap. Withdrawals "
    "(withdraw/redeem) pull from strategies in default_queue order unless the withdrawer passes a custom queue; "
    "use_default_queue=True forces the default queue even then, and False only permits custom queues — the "
    "default queue still applies to ordinary withdrawals. Lines marked as moves, liquidity, exposure or ratios "
    "after the batch are this adapter's model of the calls, run in batch order."
)


def format_yearn_v3_prompt(contexts: list[YearnV3VaultContext]) -> str:
    """Render verified Yearn V3 vault context for the LLM prompt; shared semantics are stated once."""
    sections: list[str] = [V3_SEMANTICS] if contexts else []
    for context in contexts:
        lines = [
            f"Yearn V3 vault {context.vault_address}: {context.name} ({context.symbol}), API {context.api_version}, "
            f"asset {context.asset_symbol} {context.asset_address}",
            context.units_line(),
            f"State now: totalAssets {context.amount(context.total_assets)} (debt {context.amount(context.total_debt)}, "
            f"idle {context.amount(context.total_idle)}); deposit_limit {context.optional_amount(context.deposit_limit)}; "
            f"use_default_queue {context.use_default_queue}; shutdown {str(context.is_shutdown).lower()}",
            *_allocator_lines(context, lambda a: a),
            f"Default queue ({len(context.default_queue)}/{MAX_QUEUE}):",
            *(f"  {i}. {_queue_line(context, s)}" for i, s in enumerate(context.default_queue, start=1)),
            "Proposed by this transaction (in batch order, across every vault in it):",
            *(f"  - {line}" for line in context.proposal_lines()),
            *context.outcome_lines,
        ]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def format_yearn_v3_report(
    contexts: list[YearnV3VaultContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic Yearn V3 section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        lines = [
            f"**Yearn V3 vault:** {address_link(context.vault_address, chain_id, labels)} — API "
            f"`{context.api_version}`, asset `{context.asset_symbol}` ({context.asset_decimals} decimals)",
            f"- **Size:** `{context.amount(context.total_assets)}` total assets "
            f"(`{context.amount(context.total_debt)}` deployed, `{context.amount(context.total_idle)}` idle)",
            f"- **Deposit limit:** `{context.optional_amount(context.deposit_limit)}` · "
            f"**use_default_queue:** `{context.use_default_queue}` · **shutdown:** `{str(context.is_shutdown).lower()}`",
            f"- **Default queue** ({len(context.default_queue)}/{MAX_QUEUE}, current_debt / max_debt):",
        ]
        lines.extend(
            f"  {i}. {address_link(s.address, chain_id, labels)} — `{context.amount(s.current_debt)}` / "
            f"`{context.amount(s.max_debt)}`"
            for i, s in enumerate(context.default_queue, start=1)
        )
        lines.extend(f"- {line}" for line in _allocator_lines(context, lambda a: address_link(a, chain_id, labels)))
        lines.append("- **Proposed:**")
        lines.extend(f"  - {line}" for line in context.proposal_lines())
        lines.extend(f"- {line}" for line in context.outcome_lines)
        lines.append(
            "- _Units: `max_debt`, `target_debt`, `deposit_limit` and `minimum_total_idle` are in the vault asset. "
            "Funds move only via `update_debt`._"
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
