"""Resolve Yearn tokenized-strategy context for strategy-configuration calls.

A batch that reconfigures Yearn V3 tokenized strategies arrives as bare
setters — ``setProfitMaxUnlockTime(0)``, ``setEmergencyAdmin(0xe5e2…)``,
``setCustomStrategyTrigger(strategy, trigger)`` — and the generic state reader
misses most of their before-values, because the setters live in the shared
``TokenizedStrategy`` implementation rather than in the strategy's own source.
A report on two Grove compounders and a Spark compounder therefore called the
Groves' previous unlock times, admins and fee recipients "not provided", could
not say what the 6,279 ysUSDS burned by ``setProfitMaxUnlockTime(0)`` was, and
missed that every performance fee was already 0.

For calls whose target (or, for ``setCustomStrategyTrigger``, whose strategy
argument) answers the tokenized-strategy getters, this adapter reads the
strategy's settings and walks the batch in order, rendering each change as
old → new. A zero unlock time is explained in full: the strategy burns every
share it holds for itself, crediting not-yet-unlocked profit to holders at
once, and every later report's profit lands instantly — so who may deposit
matters, and the deposit gate is reported alongside.

It keys on call shape and on-chain getters, not on the alert's protocol: the
same strategy code is run by Yearn and by third parties.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.source_context import get_contract_label
from utils.web3_wrapper import ChainManager, Web3Client

logger = get_logger("utils.llm.yearn_strategy_context")

# Calls on the strategy itself that this adapter explains.
STRATEGY_CALLS = frozenset(
    {
        "setProfitMaxUnlockTime",
        "setEmergencyAdmin",
        "setPerformanceFeeRecipient",
        "setPerformanceFee",
        "setKeeper",
        "setOpen",
        "setOpenDeposits",
        "setAllowed",
        "report",
    }
)
# Called on a CommonReportTrigger with the strategy as its first argument.
TRIGGER_CALL = "setCustomStrategyTrigger"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# Renders an address: plain text with its label for the prompt, an explorer link for the report.
Render = Callable[[str], str]


@dataclass(frozen=True)
class TriggerInfo:
    """A custom report trigger and the minimum gap it enforces between reports, when it has one."""

    address: str
    min_report_delay: int | None = None


@dataclass
class YearnStrategyContext:
    """Settings of one tokenized strategy before the transaction, and the calls that change them."""

    address: str
    name: str
    api_version: str
    asset_symbol: str
    asset_decimals: int
    total_assets: int
    # ERC-4626 totalSupply, which TokenizedStrategy reports net of already-unlocked shares.
    total_supply: int
    # Shares the strategy holds for itself: still locking (balanceOf(self)) and unlocked but not yet burned.
    locked_shares: int
    unlocked_shares: int
    profit_max_unlock_time: int
    last_report: int
    performance_fee: int
    performance_fee_recipient: str
    management: str
    keeper: str
    emergency_admin: str
    is_shutdown: bool
    now: int
    # Public deposits: True open to anyone, False allowlist only, None no such gate; and its getter name.
    deposits_open: bool | None = None
    deposit_gate: str = ""
    # (call target, call) in batch order; a trigger call targets the CommonReportTrigger.
    calls: list[tuple[str, DecodedCall]] = field(default_factory=list)
    # Reads keyed by address: `allowed(x)` before the batch, and the trigger currently set on each trigger contract.
    allowed_before: dict[str, bool] = field(default_factory=dict)
    trigger_before: dict[str, str] = field(default_factory=dict)
    triggers: dict[str, TriggerInfo] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def addresses(self) -> list[str]:
        """Addresses this context introduces to the report."""
        found = [self.address, self.management, self.keeper, self.emergency_admin, self.performance_fee_recipient]
        for target, call in self.calls:
            found.append(target)
            found.extend(str(v) for t, v in call.params if t == "address")
        return [to_checksum_address(a) for a in dict.fromkeys(found) if a and int(a, 16) != 0]

    def amount(self, raw: int) -> str:
        """Render an asset amount with two truncated decimals."""
        scale = 10**self.asset_decimals
        whole, cents = raw // scale, (raw % scale) * 100 // scale
        if whole == 0 and cents == 0 and raw > 0:
            return f"<0.01 {self.asset_symbol}"
        return f"{whole:,}.{cents:02d} {self.asset_symbol}" if cents else f"{whole:,} {self.asset_symbol}"

    def shares(self, raw: int) -> str:
        """Render a share amount; strategy shares use the asset's decimals."""
        scale = 10**self.asset_decimals
        return f"{raw / scale:,.2f} shares"

    def deposit_gate_text(self, deposits_open: bool | None) -> str:
        """Who may deposit, in words."""
        if deposits_open is None:
            return "the deposit gate is not readable"
        if deposits_open:
            return f"deposits are open to anyone (`{self.deposit_gate}` = true)"
        return f"deposits are allowlist-only (`{self.deposit_gate}` = false)"

    def header_lines(self, render: Render) -> list[str]:
        """Strategy identity and settings before the transaction."""
        fee = f"{self.performance_fee / 100:g}%"
        return [
            f"Yearn tokenized strategy {render(self.address)}: {self.name}, API {self.api_version}, "
            f"asset {self.asset_symbol}; totalAssets {self.amount(self.total_assets)}; "
            f"shutdown {str(self.is_shutdown).lower()}.",
            f"Settings before this transaction: management {render(self.management)}; keeper {render(self.keeper)}; "
            f"emergencyAdmin {render(self.emergency_admin)}; performanceFee {fee} to "
            f"{render(self.performance_fee_recipient)}; profitMaxUnlockTime {_duration(self.profit_max_unlock_time)}; "
            f"last report {_utc(self.last_report)} ({_age(self.now - self.last_report)} ago); "
            f"{self.deposit_gate_text(self.deposits_open)}.",
        ]

    def proposal_lines(self, render: Render) -> list[str]:
        """Each call as old → new, applied in batch order so later calls see earlier ones."""
        unlock_time = self.profit_max_unlock_time
        admin, recipient, keeper, fee = (
            self.emergency_admin,
            self.performance_fee_recipient,
            self.keeper,
            self.performance_fee,
        )
        deposits_open = self.deposits_open
        allowed = dict(self.allowed_before)
        reported = False
        lines: list[str] = []
        for target, call in self.calls:
            name = call.function_name
            value = call.params[-1][1] if call.params else None
            if name == "setProfitMaxUnlockTime" and isinstance(value, int):
                lines.append(self._unlock_line(unlock_time, value, reported))
                unlock_time = value
            elif name == "setEmergencyAdmin" and isinstance(value, str):
                lines.append(
                    f"setEmergencyAdmin: {render(admin)} → {render(value)}. The emergency admin can shut the "
                    "strategy down and, once shut down, emergency-withdraw from the yield source; funds stay in the "
                    f"strategy for holders. Management stays {render(self.management)}."
                )
                admin = value
            elif name == "setPerformanceFeeRecipient" and isinstance(value, str):
                note = (
                    " The performance fee is 0, so the recipient receives nothing until the fee is raised."
                    if fee == 0
                    else f" It receives {fee / 100:g}% of each report's profit as shares."
                )
                lines.append(f"setPerformanceFeeRecipient: {render(recipient)} → {render(value)}.{note}")
                recipient = value
            elif name == "setPerformanceFee" and isinstance(value, int):
                lines.append(f"setPerformanceFee: {fee / 100:g}% → {value / 100:g}% of reported profit.")
                fee = value
            elif name == "setKeeper" and isinstance(value, str):
                lines.append(f"setKeeper: {render(keeper)} → {render(value)} (may call report() and tend()).")
                keeper = value
            elif name in ("setOpen", "setOpenDeposits") and isinstance(value, bool):
                line = f"{name}: {self.deposit_gate_text(deposits_open)} → {self.deposit_gate_text(value)}."
                if not value:
                    line += " Withdrawals are unaffected; holders not on `allowed` can no longer add to their position."
                lines.append(line)
                deposits_open = value
            elif name == "setAllowed" and len(call.params) == 2:
                account = str(call.params[0][1])
                before = allowed.get(account.lower())
                was = "unknown" if before is None else str(before).lower()
                lines.append(f"setAllowed: `allowed({render(account)})` {was} → {str(bool(value)).lower()}.")
                allowed[account.lower()] = bool(value)
            elif name == "report":
                timing = (
                    "credited to holders immediately (profitMaxUnlockTime is 0)"
                    if unlock_time == 0
                    else f"unlocked to holders linearly over {_duration(unlock_time)}"
                )
                lines.append(
                    f"report(): harvests and books profit or loss now; profit is {timing}. Previous report "
                    f"{_utc(self.last_report)}. Token transfers inside it (reward claims, auction kicks, "
                    "redeposits into the yield source) are the strategy managing its own position, not payments."
                )
                reported = True
            elif name == TRIGGER_CALL and len(call.params) == 2:
                lines.append(self._trigger_line(target, str(call.params[1][1]), render))
        return lines

    def _unlock_line(self, before: int, after: int, reported: bool) -> str:
        line = f"setProfitMaxUnlockTime: {_duration(before)} → {_duration(after)}."
        if after != 0:
            return f"{line} Profit from each report unlocks to holders linearly over {_duration(after)}."
        if before == 0:
            return line
        held = self.locked_shares + self.unlocked_shares
        effective = self.total_supply - self.locked_shares
        if held and effective > 0:
            credited = self.locked_shares * self.total_assets // self.total_supply if self.total_supply else 0
            bump = self.locked_shares / effective * 100
            basis = "before this transaction (a report earlier in the batch changes it)" if reported else "now"
            line += (
                f" Setting 0 burns every share the strategy holds for itself — {self.shares(held)} "
                f"({self.shares(self.locked_shares)} still locking, {self.shares(self.unlocked_shares)} already "
                f"unlocked), as of {basis} — so ~{self.amount(credited)} of not-yet-unlocked profit is credited to "
                f"holders at once (price per share +{bump:.3f}%)."
            )
        return (
            f"{line} From now on each report credits its profit instantly, so anyone able to deposit just "
            f"before a report captures part of it; {self.deposit_gate_text(self.deposits_open)} before this batch."
        )

    def _trigger_line(self, trigger_contract: str, trigger: str, render: Render) -> str:
        current = self.trigger_before.get(trigger_contract.lower())
        before = "unknown" if current is None else "default trigger" if int(current, 16) == 0 else render(current)
        info = self.triggers.get(trigger.lower())
        line = f"setCustomStrategyTrigger on {render(trigger_contract)}: {before} → {render(trigger)}."
        if info and info.min_report_delay is not None:
            line += (
                f" It lets keepers report only once {_duration(info.min_report_delay)} have passed since lastReport "
                f"(last report {_age(self.now - self.last_report)} ago)."
            )
        return line


def _duration(seconds: int) -> str:
    """``345600`` → ``4d (345600 s)``; 0 → ``0 (instant)``."""
    if seconds == 0:
        return "0 (instant)"
    days, rest = divmod(seconds, 86400)
    if days and not rest:
        text = f"{days}d"
    elif seconds >= 3600:
        text = f"{seconds / 3600:g}h"
    else:
        text = f"{seconds}s"
    return f"{text} ({seconds} s)" if seconds >= 3600 else text


def _age(seconds: int) -> str:
    """Elapsed time, rounded for prose: ``665196`` → ``7d 16h``, ``18420`` → ``5h 7m``."""
    days, rest = divmod(max(seconds, 0), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m"


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC") if timestamp else "never"


def _view(client: Web3Client, address: str, signature: str, output: str, args: tuple = ()) -> object | None:
    """eth_call one getter; None when it reverts, is missing or doesn't decode."""
    types = signature[signature.index("(") + 1 : -1]
    try:
        data = function_signature_to_4byte_selector(signature) + (
            abi_encode(types.split(","), list(args)) if args else b""
        )
        raw = client.eth.call({"to": to_checksum_address(address), "data": "0x" + data.hex()})
        return abi_decode([output], bytes(raw))[0] if raw else None
    except Exception:  # noqa: BLE001 - absent getters and reverts are expected
        return None


def _address(value: object) -> str | None:
    return to_checksum_address(str(value)) if isinstance(value, str) else None


def _read_strategy(chain_id: int, client: Web3Client, address: str) -> YearnStrategyContext | None:
    """Read a tokenized strategy's settings; None when ``address`` is not one."""
    api = _view(client, address, "apiVersion()", "string")
    management = _address(_view(client, address, "management()", "address"))
    last_report = _view(client, address, "lastReport()", "uint256")
    unlock_time = _view(client, address, "profitMaxUnlockTime()", "uint256")
    if not (isinstance(api, str) and api.startswith("3.")) or management is None:
        return None
    if not isinstance(last_report, int) or not isinstance(unlock_time, int):
        return None

    def uint(signature: str, *args: object) -> int:
        value = _view(client, address, signature, "uint256", args)
        return value if isinstance(value, int) else 0

    asset = _address(_view(client, address, "asset()", "address"))
    meta = fetch_erc20_metadata(chain_id, asset) if asset else None
    deposits_open, gate = None, ""
    for getter in ("openDeposits()", "open()"):
        value = _view(client, address, getter, "bool")
        if isinstance(value, bool):
            deposits_open, gate = value, getter[:-2]
            break
    fee = _view(client, address, "performanceFee()", "uint16")
    name = _view(client, address, "name()", "string")
    return YearnStrategyContext(
        address=to_checksum_address(address),
        name=name if isinstance(name, str) else address,
        api_version=api,
        asset_symbol=meta.symbol if meta else "assets",
        asset_decimals=meta.decimals if meta else 18,
        total_assets=uint("totalAssets()"),
        total_supply=uint("totalSupply()"),
        locked_shares=uint("balanceOf(address)", address),
        unlocked_shares=uint("unlockedShares()"),
        profit_max_unlock_time=unlock_time,
        last_report=last_report,
        performance_fee=fee if isinstance(fee, int) else 0,
        performance_fee_recipient=_address(_view(client, address, "performanceFeeRecipient()", "address"))
        or ZERO_ADDRESS,
        management=management,
        keeper=_address(_view(client, address, "keeper()", "address")) or ZERO_ADDRESS,
        emergency_admin=_address(_view(client, address, "emergencyAdmin()", "address")) or ZERO_ADDRESS,
        is_shutdown=_view(client, address, "isShutdown()", "bool") is True,
        now=int(client.eth.get_block("latest")["timestamp"]),
        deposits_open=deposits_open,
        deposit_gate=gate,
    )


def _strategy_of(target: str, call: DecodedCall) -> str | None:
    """The strategy a call configures: its target, or a trigger call's first argument."""
    if call.function_name in STRATEGY_CALLS:
        return target
    if call.function_name == TRIGGER_CALL and call.params and call.params[0][0] == "address":
        return str(call.params[0][1])
    return None


def _complete(chain_id: int, client: Web3Client, context: YearnStrategyContext) -> None:
    """Read what the calls themselves need: allowlist entries, current and new triggers, and labels."""
    for target, call in context.calls:
        if call.function_name == "setAllowed" and call.params:
            account = str(call.params[0][1])
            value = _view(client, context.address, "allowed(address)", "bool", (account,))
            if isinstance(value, bool):
                context.allowed_before[account.lower()] = value
        elif call.function_name == TRIGGER_CALL and len(call.params) == 2:
            current = _view(client, target, "customStrategyTrigger(address)", "address", (context.address,))
            if isinstance(current, str):
                context.trigger_before[target.lower()] = current
            trigger = str(call.params[1][1])
            delay = _view(client, trigger, "minReportDelay()", "uint256")
            context.triggers[trigger.lower()] = TriggerInfo(
                address=trigger, min_report_delay=delay if isinstance(delay, int) else None
            )
    for address in context.addresses:
        label = get_contract_label(chain_id, address)
        if label:
            context.labels[address] = label


def resolve_yearn_strategy_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[YearnStrategyContext]:
    """Resolve settings and old → new changes for every tokenized strategy the calls configure.

    ``protocol`` is unused: the strategy code is the same whoever runs it.
    """
    del protocol
    grouped: dict[str, list[tuple[str, DecodedCall]]] = {}
    for target, call in targets_and_calls:
        strategy = _strategy_of(target, call)
        if strategy is None:
            continue
        try:
            grouped.setdefault(to_checksum_address(strategy), []).append((to_checksum_address(target), call))
        except ValueError:
            continue
    if not grouped:
        return []
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))

    contexts: list[YearnStrategyContext] = []
    for strategy, calls in grouped.items():
        try:
            context = _read_strategy(chain_id, client, strategy)
            if context is None:
                continue
            context.calls = calls
            _complete(chain_id, client, context)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Yearn strategy context failed for %s: %s", strategy, error)
            continue
        contexts.append(context)
    return contexts


def format_yearn_strategy_prompt(contexts: list[YearnStrategyContext]) -> str:
    """Render verified tokenized-strategy settings and changes for the LLM prompt."""
    sections: list[str] = []
    for context in contexts:

        def render(address: str, labels: dict[str, str] = context.labels) -> str:
            checksum = to_checksum_address(address)
            label = labels.get(checksum)
            return f"{checksum} ({label})" if label else checksum

        lines = [
            *context.header_lines(render),
            "Proposed by this transaction (in batch order):",
            *(f"  - {line}" for line in context.proposal_lines(render)),
        ]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def format_yearn_strategy_report(
    contexts: list[YearnStrategyContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic tokenized-strategy section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        merged = {**context.labels, **labels}

        def render(address: str, merged: dict[str, str] = merged) -> str:
            return address_link(address, chain_id, merged)

        header, settings = context.header_lines(render)
        lines = [f"**{header}**", f"- {settings}", "- **Proposed:**"]
        lines.extend(f"  - {line}" for line in context.proposal_lines(render))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
