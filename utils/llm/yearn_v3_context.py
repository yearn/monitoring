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
"""

from dataclasses import dataclass, field
from typing import Any

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.report import address_link
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
    _fn("strategies", ["uint256", "uint256", "uint256", "uint256"], ["address"]),
]


@dataclass(frozen=True)
class StrategyState:
    """A strategy's registration on one vault, plus what it is."""

    address: str
    name: str | None
    activation: int
    current_debt: int
    max_debt: int
    in_default_queue: bool
    # Read only for strategies the transaction names; None when unreadable.
    asset: str | None = None
    total_assets: int | None = None
    # Set when the strategy is itself a V3 vault (an allocator), with the names
    # of the strategies it allocates to.
    is_vault: bool = False
    sub_strategies: tuple[str, ...] = ()

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
        scale = 10**self.asset_decimals
        whole, cents = raw // scale, (raw % scale) * 100 // scale
        if whole == 0 and cents == 0 and raw > 0:
            return f"<0.01 {self.asset_symbol}"
        rendered = f"{whole:,}" if cents == 0 else f"{whole:,}.{cents:02d}"
        return f"{rendered} {self.asset_symbol}"

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

    def semantics_line(self) -> str:
        """Deterministic unit and flag semantics from the vault source."""
        return (
            f"Units: max_debt, target_debt, deposit_limit and minimum_total_idle on this vault are denominated in "
            f"its asset {self.asset_symbol} ({self.asset_decimals} decimals) — verified, do not hedge about units. "
            "add_strategy(strategy, add_to_queue) registers the strategy with max_debt 0; add_to_queue=True "
            "appends it to the default queue (used for withdrawals), False leaves it out. "
            "update_max_debt_for_strategy only sets a ceiling: funds move only when a DEBT_MANAGER "
            "(typically a debt allocator) calls update_debt."
        )

    def proposal_lines(self) -> list[str]:
        """One line per call, in batch order, comparing proposed values with current state."""
        lines: list[str] = []
        added: set[str] = set()
        for call in self.calls:
            line = self._proposal_line(call, added)
            if line:
                lines.append(line)
        return lines

    def _proposal_line(self, call: DecodedCall, added: set[str]) -> str:
        """Describe one call against the vault's state before the transaction."""
        params = call.params
        name = call.function_name
        first = params[0][1] if params else None
        strategy = self.strategy(first) if isinstance(first, str) and first.startswith("0x") else None
        label = strategy.display if strategy else str(first)

        if name == "add_strategy" and strategy:
            add_to_queue = bool(params[1][1]) if len(params) > 1 else True
            added.add(strategy.address.lower())
            status = "ALREADY ACTIVE — the call reverts" if strategy.active else "not yet active"
            parts = [f"add_strategy({label}): {status}"]
            if strategy.asset is not None:
                matches = strategy.asset.lower() == self.asset_address.lower()
                parts.append(
                    f"strategy asset {'matches' if matches else 'DOES NOT match — the call reverts on'} "
                    f"the vault asset {self.asset_symbol}"
                )
            if add_to_queue:
                queue_len = len(self.default_queue)
                parts.append(
                    f"add_to_queue=True: appended to the default queue (holds {queue_len}/{MAX_QUEUE} before this batch)"
                    if queue_len < MAX_QUEUE
                    else f"add_to_queue=True but the default queue is full ({MAX_QUEUE}), so it is NOT appended"
                )
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
                subs = ", ".join(strategy.sub_strategies) or "no strategies"
                parts.append(
                    f"the strategy is itself a Yearn V3 vault (totalAssets {self.optional_amount(strategy.total_assets)}"
                    f"; allocates to: {subs})"
                )
            elif strategy.total_assets is not None:
                parts.append(f"strategy totalAssets {self.amount(strategy.total_assets)}")
            return "; ".join(parts) + "."

        if name == "update_max_debt_for_strategy" and strategy and len(params) > 1:
            new_max = int(params[1][1])
            before = "added earlier in this batch" if strategy.address.lower() in added else None
            current = before or (
                f"current max_debt {self.amount(strategy.max_debt)}, current_debt {self.amount(strategy.current_debt)}"
                if strategy.active
                else "strategy not active"
            )
            parts = [f"update_max_debt_for_strategy({label}): {self.amount(new_max)} ({current})"]
            parts.append(f"= {self.share_of_vault(new_max)} ({self.amount(self.total_assets)})")
            peers = {s.max_debt for s in self.default_queue if s.address.lower() != strategy.address.lower()}
            if peers == {new_max}:
                parts.append("same max_debt as every other default-queue strategy")
            if self.deposit_limit is not None and new_max == self.deposit_limit:
                parts.append("equal to the vault's deposit_limit")
            return "; ".join(parts) + "."

        if name == "update_debt" and strategy and len(params) > 1:
            target = int(params[1][1])
            if target == MAX_UINT256:
                return f"update_debt({label}): MOVES FUNDS NOW — allocates all available idle ({self.amount(self.total_idle)})."
            delta = target - strategy.current_debt
            direction = "deposits" if delta > 0 else "withdraws"
            return (
                f"update_debt({label}): MOVES FUNDS NOW — current_debt {self.amount(strategy.current_debt)} → "
                f"{self.amount(target)} ({direction} {self.amount(abs(delta))}; idle now {self.amount(self.total_idle)})."
            )

        if name == "revoke_strategy" and strategy:
            return (
                f"revoke_strategy({label}): removes it; reverts unless current_debt is 0 "
                f"(now {self.amount(strategy.current_debt)})."
            )

        if name == "force_revoke_strategy" and strategy:
            return (
                f"force_revoke_strategy({label}): removes it and WRITES OFF its current_debt "
                f"{self.amount(strategy.current_debt)} as a loss to depositors "
                f"({self.share_of_vault(strategy.current_debt)})."
            )

        if name == "set_deposit_limit" and params:
            current = self.optional_amount(self.deposit_limit)
            return f"set_deposit_limit: {current} → {self.amount(int(params[0][1]))}."

        if name == "set_minimum_total_idle" and params:
            current = self.optional_amount(self.minimum_total_idle)
            return f"set_minimum_total_idle: {current} → {self.amount(int(params[0][1]))}."

        if name == "set_use_default_queue" and params:
            return f"set_use_default_queue: {self.use_default_queue} → {bool(params[0][1])}."

        if name == "set_default_queue" and params and isinstance(first, (list, tuple)):
            new_queue = [self._name_of(str(a)) for a in first]
            current_queue = [s.display for s in self.default_queue]
            removed = [n for n in current_queue if n not in new_queue]
            line = f"set_default_queue: [{', '.join(current_queue)}] → [{', '.join(new_queue)}]"
            return line + (f"; removed from the queue: {', '.join(removed)}." if removed else ".")

        return ""

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


def _read_strategy_details(client: Any, chain_id: int, address: str) -> dict[str, Any]:
    """Asset, size, and — for an allocator vault — its own queue, for a named strategy."""
    contract = client.get_contract(address, _VAULT_ABI)
    asset = _optional_call(contract.functions.asset())
    total_assets = _optional_call(contract.functions.totalAssets())
    sub_queue = _optional_call(contract.functions.get_default_queue())
    is_vault = isinstance(sub_queue, (list, tuple))
    subs = tuple(_token_name(chain_id, str(a)) or str(a) for a in sub_queue) if is_vault else ()
    return {
        "asset": to_checksum_address(str(asset)) if isinstance(asset, str) else None,
        "total_assets": int(total_assets) if isinstance(total_assets, int) else None,
        "is_vault": is_vault,
        "sub_strategies": subs,
    }


def _read_vault_context(chain_id: int, vault: str, calls: list[DecodedCall]) -> YearnV3VaultContext | None:
    """Read a V3 vault's state and the strategies its calls touch; None if not a V3 vault."""
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
        details = _read_strategy_details(client, chain_id, address) if address in named_set else {}
        states[address] = StrategyState(
            address=address,
            name=_token_name(chain_id, address),
            activation=int(activation),
            current_debt=int(current_debt),
            max_debt=int(max_debt),
            in_default_queue=address in queue_addresses,
            **details,
        )

    deposit_limit = _optional_call(contract.functions.deposit_limit())
    minimum_total_idle = _optional_call(contract.functions.minimum_total_idle())
    use_default_queue = _optional_call(contract.functions.use_default_queue())
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
    )


def resolve_yearn_v3_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[YearnV3VaultContext]:
    """Resolve Yearn V3 vault context for every vault targeted by a strategy-management call.

    ``protocol`` is unused: the same vault code is governed by many parties, so
    the adapter is selected by call shape and confirmed by ``apiVersion()``.
    """
    del protocol
    calls_by_vault: dict[str, list[DecodedCall]] = {}
    for target, call in targets_and_calls:
        if call.function_name not in VAULT_CALLS:
            continue
        try:
            checksum = to_checksum_address(target)
        except ValueError:
            continue
        calls_by_vault.setdefault(checksum, []).append(call)

    contexts: list[YearnV3VaultContext] = []
    for vault, calls in calls_by_vault.items():
        try:
            context = _read_vault_context(chain_id, vault, calls)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Yearn V3 context resolution failed for %s: %s", vault, error)
            continue
        if context is not None:
            contexts.append(context)
    return contexts


def _queue_line(context: YearnV3VaultContext, strategy: StrategyState) -> str:
    """One default-queue entry for the prompt: name, address, debt and cap."""
    return f"{strategy.display} {strategy.address}: current_debt {context.amount(strategy.current_debt)} / max_debt {context.amount(strategy.max_debt)}"


def format_yearn_v3_prompt(contexts: list[YearnV3VaultContext]) -> str:
    """Render verified Yearn V3 vault context for the LLM prompt."""
    sections: list[str] = []
    for context in contexts:
        lines = [
            f"Yearn V3 vault {context.vault_address}: {context.name} ({context.symbol}), API {context.api_version}, "
            f"asset {context.asset_symbol} {context.asset_address}",
            context.semantics_line(),
            f"State now: totalAssets {context.amount(context.total_assets)} (debt {context.amount(context.total_debt)}, "
            f"idle {context.amount(context.total_idle)}); deposit_limit {context.optional_amount(context.deposit_limit)}; "
            f"use_default_queue {context.use_default_queue}; shutdown {str(context.is_shutdown).lower()}",
            f"Default queue ({len(context.default_queue)}/{MAX_QUEUE}):",
            *(f"  {i}. {_queue_line(context, s)}" for i, s in enumerate(context.default_queue, start=1)),
            "Proposed by this transaction (in batch order):",
            *(f"  - {line}" for line in context.proposal_lines()),
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
        lines.append("- **Proposed:**")
        lines.extend(f"  - {line}" for line in context.proposal_lines())
        lines.append(
            "- _Units: `max_debt`, `target_debt`, `deposit_limit` and `minimum_total_idle` are in the vault asset. "
            "Funds move only via `update_debt`._"
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
