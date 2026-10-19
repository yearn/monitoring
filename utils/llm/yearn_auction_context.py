"""Resolve Yearn Auction context for reward-selling strategies and direct auction calls.

Yearn strategies sell their reward tokens through a periphery ``Auction``: a
Dutch auction that offers the auction's whole balance of a token, starts the
lot at ``startingPrice`` and decays it until a taker buys or the per-token price
falls below ``minimumPrice``. None of that is visible in the calldata. A batch
that ran ``setMinimumAuctionPrice`` and ``report()`` on two Grove compounders was
reported as leaving the auction's pricing untouched (the strategy's setter also
sets the Auction's floor), as kicking a 284k GROVE lot (the auction already held
698k GROVE unsold from an expired lot, so the lot was 983k), and as unable to
say what the 880 USDS booked as profit was (proceeds of a lot that sold out).

For calls on a strategy that answers ``auction()``, and for calls on an Auction
itself, this adapter reads the auction's pricing, every enabled lot's pending
balance, the strategy's idle proceeds and the token's market price, and walks
the batch in order: forwarded or direct setters as old → new, kicks with the
lot they would offer and what it clears for at the floor.

Strategy setters count as auction setters only when their verified body passes
the argument straight to the Auction (``Auction(auction).setMinimumPrice(x)``);
a name alone is never trusted. Keys on call shape and on-chain getters, not on
the alert's protocol.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.defillama import defillama_key, fetch_prices
from utils.erc20_metadata import fetch_erc20_metadata
from utils.eth_view import call_view
from utils.formatting import format_asset_amount, format_decimal_amount, format_duration, format_utc
from utils.formatting import normalize_token_amount as normalize
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.source_context import (
    extract_short_function_body,
    fetch_function_input_names,
    fetch_source,
    get_contract_label,
)
from utils.web3_wrapper import ChainManager, Web3Client

logger = get_logger("utils.llm.yearn_auction_context")

# Auction setters and the setting each one writes.
AUCTION_SETTERS: dict[str, str] = {
    "setMinimumPrice": "minimumPrice",
    "setStartingPrice": "startingPrice",
    "setStepDecayRate": "stepDecayRate",
    "setStepDuration": "stepDuration",
    "setReceiver": "receiver",
}
# Calls made on the Auction itself.
AUCTION_CALLS = frozenset({*AUCTION_SETTERS, "kick", "sweep", "enable", "disable", "setGovernanceOnlyKick"})
# Strategy calls that can kick the strategy's auction.
KICKING_CALLS = frozenset({"report", "kickAuction"})
MIN_AMOUNT_CALL = "setMinAmountToSell"
SET_AUCTION_CALL = "setAuction"
# A strategy body forwarding one of its arguments to the Auction: `Auction(auction).setMinimumPrice(_price)`.
_FORWARD_RE = re.compile(r"\.\s*(" + "|".join([*AUCTION_SETTERS, "enable", "disable"]) + r")\s*\(\s*(\w+)\s*\)")
# Enabled tokens read per auction; Yearn auctions sell one or two reward tokens.
MAX_LOTS = 4
WAD = 10**18

# Renders an address: plain text with its label for the prompt, an explorer link for the report.
Render = Callable[[str], str]


@dataclass
class Lot:
    """One token the auction sells, as it stands before the transaction."""

    token: str
    symbol: str
    decimals: int
    in_auction: int
    kicked: int
    active: bool
    available: int
    # The strategy's side: tokens it holds, rewards it can claim, and the balance it must exceed to kick.
    strategy_held: int | None = None
    claimable: int | None = None
    min_amount_to_sell: int | None = None
    # Market price of one token in want, from DeFiLlama USD prices.
    market_price: Decimal | None = None

    def amount(self, raw: int) -> str:
        """Render a token amount."""
        return format_asset_amount(raw, self.decimals, self.symbol)


@dataclass
class AuctionCall:
    """A call that touches the auction, directly or through its strategy."""

    target: str
    call: DecodedCall
    # "auction" (on the Auction), "forward" (strategy setter verified to forward), "kick", "min_amount",
    # "set_auction" (the strategy switches auction contracts).
    kind: str
    # Auction function the call reaches and the argument it passes.
    function: str = ""
    value: object = None
    # False when a report() could not be confirmed, from the strategy's verified source, to kick.
    confirmed: bool = True


@dataclass
class YearnAuctionContext:
    """An Auction's settings and lots before the transaction, and the calls that change them."""

    address: str
    version: str
    want: str
    want_symbol: str
    want_decimals: int
    receiver: str
    governance: str
    governance_only_kick: bool | None
    minimum_price: int
    starting_price: int
    step_decay_rate: int | None
    step_duration: int | None
    auction_length: int | None
    now: int
    lots: list[Lot] = field(default_factory=list)
    strategy: str | None = None
    strategy_total_assets: int | None = None
    strategy_idle_want: int | None = None
    calls: list[AuctionCall] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def addresses(self) -> list[str]:
        """Addresses this context introduces to the report."""
        found = [self.address, self.want, self.receiver, self.governance, self.strategy or ""]
        found.extend(lot.token for lot in self.lots)
        for entry in self.calls:
            found.append(entry.target)
            if isinstance(entry.value, str):
                found.append(entry.value)
        return [to_checksum_address(a) for a in dict.fromkeys(found) if a and a.startswith("0x") and int(a, 16) != 0]

    def want_amount(self, amount: Decimal) -> str:
        """Render a normalized want amount with two decimals."""
        return f"{amount:,.2f} {self.want_symbol}"

    def price(self, raw: int, lot: Lot | None = None) -> str:
        """A 1e18-scaled per-token price in want: ``0.006 USDS per GROVE``."""
        unit = f" per {lot.symbol}" if lot else " per token"
        return f"{format_decimal_amount(normalize(raw, 18))} {self.want_symbol}{unit}"

    def lot_for(self, token: object) -> Lot | None:
        """The lot selling ``token``; the only lot when ``token`` is not given."""
        if isinstance(token, str):
            return next((lot for lot in self.lots if lot.token.lower() == token.lower()), None)
        return self.lots[0] if len(self.lots) == 1 else None

    def header_lines(self, render: Render) -> list[str]:
        """Auction identity, pricing and pending lots before the transaction."""
        receiver = render(self.receiver)
        if self.strategy and self.receiver.lower() == self.strategy.lower():
            receiver += " (the strategy itself)"
        sells = ", ".join(lot.symbol for lot in self.lots) or "no enabled token"
        kick_gate = "; only governance may kick" if self.governance_only_kick else ""
        owner = f" for strategy {render(self.strategy)}" if self.strategy else ""
        lines = [
            f"Yearn Auction {render(self.address)}{owner} (version {self.version or 'unknown'}): sells {sells} for "
            f"{self.want_symbol}; proceeds go to receiver {receiver}; governance {render(self.governance)}"
            f"{kick_gate}.",
            f"Pricing before this transaction: {self._pricing_text()}",
        ]
        lines.extend(self._lot_line(lot, render) for lot in self.lots)
        if self.strategy_idle_want:
            lines.append(self._idle_line())
        return lines

    def _pricing_text(self) -> str:
        lot = self.lot_for(None)
        decay = (
            f"drops {self.step_decay_rate / 100:g}% ({self.step_decay_rate} bps) every "
            f"{format_duration(self.step_duration)}"
            if self.step_decay_rate is not None and self.step_duration
            else "decays on the auction's step schedule"
        )
        length = format_duration(self.auction_length) if self.auction_length else "the auction length"
        return (
            "Dutch auction. Each kick offers the auction's entire balance of the token — anything left unsold "
            "from an earlier lot plus what is sent in. The whole lot's price starts at startingPrice = "
            f"{self.want_amount(normalize(self.starting_price, 18))} and {decay}. The auction ends after {length}, "
            f"or as soon as the per-token price falls below minimumPrice = {self.price(self.minimum_price, lot)} "
            f"(raw {self.minimum_price}, 1e18-scaled); unsold tokens then stay in the auction until the next kick."
        )

    def _lot_line(self, lot: Lot, render: Render) -> str:
        if lot.active:
            state = f"live — {lot.amount(lot.available)} available, kicked {format_utc(lot.kicked)}"
        elif lot.in_auction and lot.kicked:
            state = (
                f"{lot.amount(lot.in_auction)} unsold in the auction from the lot kicked {format_utc(lot.kicked)}, "
                f"which ended ({format_duration(self.now - lot.kicked)} ago) without selling out"
            )
        elif lot.in_auction:
            state = f"{lot.amount(lot.in_auction)} in the auction, never kicked"
        else:
            state = "no lot pending (kicked = 0: never kicked, or the last lot sold out)"
        parts = [f"{lot.symbol} {render(lot.token)}: {state}"]
        if lot.strategy_held is not None:
            parts.append(f"the strategy holds {lot.amount(lot.strategy_held)}")
        if lot.claimable is not None:
            parts.append(f"it can claim {lot.amount(lot.claimable)} more now (claimableRewards())")
        if lot.min_amount_to_sell is not None:
            parts.append(f"it kicks only above minAmountToSell = {lot.amount(lot.min_amount_to_sell)}")
        line = "; ".join(parts) + "."
        if lot.market_price is not None:
            line += (
                f" Market (DeFiLlama, live): 1 {lot.symbol} ≈ {_sig(lot.market_price)} {self.want_symbol}; "
                f"minimumPrice is {_versus(normalize(self.minimum_price, 18), lot.market_price)}."
            )
        return line

    def _idle_line(self) -> str:
        idle = self.strategy_idle_want or 0
        line = (
            f"The strategy holds {format_asset_amount(idle, self.want_decimals, self.want_symbol)} idle — sale "
            "proceeds the auction paid it as receiver, not yet deployed."
        )
        if self.strategy_total_assets:
            share = idle / self.strategy_total_assets * 100
            line += (
                f" A report() counts them in totalAssets: if they arrived after the last report (lots sell after the "
                f"report that kicked them), that is {share:.3f}% of totalAssets booked as profit."
            )
        return line

    def proposal_lines(self, render: Render) -> list[str]:
        """Each call in batch order; settings and lots carry forward so later calls see earlier ones."""
        settings: dict[str, object] = {
            "minimumPrice": self.minimum_price,
            "startingPrice": self.starting_price,
            "stepDecayRate": self.step_decay_rate,
            "stepDuration": self.step_duration,
            "receiver": self.receiver,
        }
        kicked: set[str] = set()
        lines: list[str] = []
        for entry in self.calls:
            name = entry.call.function_name
            if entry.kind == "kick":
                lines.extend(self._kick_lines(entry, settings, kicked))
            elif entry.kind == "min_amount":
                lines.append(self._min_amount_line(entry))
            elif entry.kind == "set_auction":
                lines.append(
                    f"{name}: the strategy's auction {render(self.address)} → {render(str(entry.value))}. Rewards "
                    "kicked after this sell through the new auction, whose settings are not shown here."
                )
            elif entry.function in AUCTION_SETTERS:
                via = (
                    f"{name} on the strategy forwards to Auction.{entry.function} (verified body)"
                    if entry.kind == "forward"
                    else entry.function
                )
                lines.append(f"{via}: {self._setting_change(entry.function, settings, entry.value, render)}")
                settings[AUCTION_SETTERS[entry.function]] = entry.value
            elif entry.function in ("enable", "disable"):
                lot = self.lot_for(entry.value)
                token = lot.symbol if lot else render(str(entry.value))
                verb = "starts selling" if entry.function == "enable" else "stops selling"
                lines.append(f"{name}: the auction {verb} {token}.")
            elif entry.function == "sweep":
                lot = self.lot_for(entry.value)
                held = f" ({lot.amount(lot.in_auction)})" if lot else ""
                lines.append(
                    f"sweep: sends the auction's entire balance of {lot.symbol if lot else render(str(entry.value))}"
                    f"{held} to the caller (governance) — those tokens leave the sale."
                )
            elif entry.function == "setGovernanceOnlyKick":
                lines.append(f"setGovernanceOnlyKick: {self.governance_only_kick} → {entry.value}.")
        return lines

    def _setting_change(self, function: str, settings: dict[str, object], value: object, render: Render) -> str:
        """``setting old → new`` for an Auction setter, with the floor placed against the market."""
        setting = AUCTION_SETTERS[function]
        before = settings[setting]
        if setting == "receiver" and isinstance(value, str):
            note = (
                ""
                if self.strategy and value.lower() == self.strategy.lower()
                else " Sale proceeds stop going to the strategy."
            )
            return f"receiver {render(str(before))} → {render(value)}.{note}"
        if not isinstance(value, int):
            return f"{setting} → {value}."
        old = before if isinstance(before, int) else None
        if setting == "minimumPrice":
            lot = self.lot_for(None)
            line = f"minimumPrice {self.price(old, lot) if old is not None else 'unknown'} → {self.price(value, lot)}"
            line += f" ({_change(old, value)})." if old else "."
            if lot and lot.market_price is not None:
                line += (
                    f" Against the market (1 {lot.symbol} ≈ {_sig(lot.market_price)} {self.want_symbol}) the floor "
                    f"moves from {_versus(normalize(old or 0, 18), lot.market_price)} to "
                    f"{_versus(normalize(value, 18), lot.market_price)}. Takers do not buy above market, so a floor above it "
                    "leaves lots unsold; below it, the gap is the most a sale can give away."
                )
            return line
        if setting == "startingPrice":
            before_text = self.want_amount(normalize(old, 18)) if old is not None else "unknown"
            return f"startingPrice {before_text} → {self.want_amount(normalize(value, 18))} for the whole lot."
        if setting == "stepDecayRate":
            return f"stepDecayRate {old} → {value} bps per step."
        return f"stepDuration {format_duration(old) if old else 'unknown'} → {format_duration(value)}."

    def _kick_lines(self, entry: AuctionCall, settings: dict[str, object], kicked: set[str]) -> list[str]:
        name = entry.call.function_name
        lots = [self.lot_for(entry.value)] if entry.value is not None else self.lots
        lines: list[str] = []
        floor = settings["minimumPrice"]
        for lot in lots:
            if lot is None:
                continue
            if lot.token in kicked:
                lines.append(f"{name}: the {lot.symbol} lot was kicked earlier in this batch and is still live.")
                continue
            if lot.active:
                lines.append(f"{name}: a {lot.symbol} lot is live, so no new {lot.symbol} lot is kicked.")
                continue
            kicked.add(lot.token)
            from_strategy = entry.target.lower() != self.address.lower()
            held = (lot.strategy_held or 0) if from_strategy else 0
            claimable = (lot.claimable or 0) if from_strategy else 0
            size = lot.in_auction + held + claimable
            parts = [f"{lot.amount(lot.in_auction)} already in the auction"]
            if from_strategy:
                parts.append(f"{lot.amount(held)} the strategy holds")
                if lot.claimable is not None:
                    parts.append(f"{lot.amount(claimable)} it claims")
            gate = ""
            if from_strategy and lot.min_amount_to_sell is not None:
                gate = (
                    f" The strategy kicks only if its own {lot.symbol} balance after claiming exceeds "
                    f"{lot.amount(lot.min_amount_to_sell)}"
                )
                if lot.claimable is not None and held + claimable <= lot.min_amount_to_sell:
                    gate += f" — ≈ {lot.amount(held + claimable)} now, so it likely does not kick"
                gate += "."
            verb = (
                f"kicks the {lot.symbol} auction"
                if entry.confirmed
                else f"may kick the {lot.symbol} auction (its harvest is not confirmed to kick from verified source); "
                "if it does, it kicks it"
            )
            line = (
                f"{name}: {verb}. The lot is the auction's whole balance — ≈ "
                f"{lot.amount(size)} ({' + '.join(parts)}; rewards keep accruing until execution, and the "
                f"AuctionKicked event's `available` is the exact lot).{gate}"
            )
            if isinstance(floor, int) and floor:
                proceeds = normalize(size, lot.decimals) * normalize(floor, 18)
                line += (
                    f" At the minimumPrice then in force ({self.price(floor, lot)}) the full lot clears for at least "
                    f"{self.want_amount(proceeds)}"
                )
                if lot.market_price is not None:
                    line += f"; at market it is worth ≈ {self.want_amount(normalize(size, lot.decimals) * lot.market_price)}"
                line += "."
            line += self._time_to_floor(size, lot, floor)
            lines.append(line)
        return lines

    def _time_to_floor(self, size: int, lot: Lot, floor: object) -> str:
        """How long the decay takes to reach the floor for a lot of ``size`` tokens, or ""."""
        if not (isinstance(floor, int) and floor and size and self.step_decay_rate and self.step_duration):
            return ""
        start = normalize(self.starting_price, 18) / normalize(size, lot.decimals)
        ratio = float(normalize(floor, 18) / start)
        if ratio >= 1:
            return (
                f" The lot's starting price ({_sig(start)} {self.want_symbol} per {lot.symbol}) is already at or "
                "below the floor, so it is never live."
            )
        steps = math.ceil(math.log(ratio) / math.log(1 - self.step_decay_rate / 10_000))
        seconds = steps * self.step_duration
        if self.auction_length and seconds >= self.auction_length:
            return f" It opens at {_sig(start)} {self.want_symbol} per {lot.symbol} and decays for the full auction."
        return (
            f" It opens at {_sig(start)} {self.want_symbol} per {lot.symbol} and reaches the floor after "
            f"~{format_duration(seconds)}; if no taker buys by then, the rest stays unsold."
        )

    def _min_amount_line(self, entry: AuctionCall) -> str:
        params = entry.call.params
        token = str(params[0][1]) if params else ""
        value = params[-1][1] if params else None
        lot = self.lot_for(token)
        if lot is None or not isinstance(value, int):
            return f"{entry.call.function_name}: minimum kick size for {token} → {value}."
        before = lot.amount(lot.min_amount_to_sell) if lot.min_amount_to_sell is not None else "unknown"
        return f"{entry.call.function_name}: the strategy kicks {lot.symbol} only above {before} → {lot.amount(value)}."


def _sig(value: Decimal) -> str:
    """Four significant digits: ``0.005751``."""
    return f"{float(value):.4g}"


def _change(old: int | None, new: int) -> str:
    """Relative change: ``-16.06%``."""
    if not old:
        return "new"
    return f"{(new - old) / old * 100:+.2f}%"


def _versus(price: Decimal, market: Decimal) -> str:
    """Where a price sits against the market: ``12.43% below market``."""
    if market <= 0:
        return "not comparable to market"
    diff = float((price - market) / market * 100)
    side = "above" if diff > 0 else "below"
    return f"{abs(diff):.2f}% {side} market"


def _address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return to_checksum_address(value)
    except ValueError:
        return None


def _uint(client: Web3Client, address: str, signature: str, *args: object) -> int | None:
    value = call_view(client, address, signature, "uint256", args)
    return value if isinstance(value, int) else None


def _is_auction(client: Web3Client, address: str) -> bool:
    """True when ``address`` answers the Yearn Auction getters this adapter reads."""
    return (
        _address(call_view(client, address, "want()", "address")) is not None
        and _uint(client, address, "startingPrice()") is not None
        and isinstance(call_view(client, address, "getAllEnabledAuctions()", "address[]"), (list, tuple))
    )


def _forwarded(chain_id: int, strategy: str, call: DecodedCall) -> tuple[str, object] | None:
    """(Auction function, argument value) when the strategy's verified body forwards an argument verbatim."""
    fetched = fetch_source(chain_id, strategy)
    if fetched is None:
        return None
    match = _FORWARD_RE.search(extract_short_function_body(fetched[1], call.function_name))
    if match is None:
        return None
    names = fetch_function_input_names(chain_id, strategy, call.function_name, call.signature) or []
    if match.group(2) not in names:
        return None
    index = names.index(match.group(2))
    return (match.group(1), call.params[index][1]) if index < len(call.params) else None


def _harvest_kicks(chain_id: int, strategy: str) -> bool:
    """Whether the strategy's verified ``_harvestAndReport`` body kicks an auction.

    Only a positive is trusted: a harvest that kicks through a helper, or one too
    long to show, reads as unconfirmed rather than as "does not kick".
    """
    fetched = fetch_source(chain_id, strategy)
    body = extract_short_function_body(fetched[1], "_harvestAndReport") if fetched else ""
    return "kick" in body.lower()


def _is_candidate(name: str) -> bool:
    """Whether a call name can touch an auction; the on-chain probes decide."""
    return name in AUCTION_CALLS or name in KICKING_CALLS or name == MIN_AMOUNT_CALL or "auction" in name.lower()


@dataclass
class _Probe:
    """Memoized on-chain probes: a batch repeats the same targets."""

    client: Web3Client
    auctions: dict[str, bool] = field(default_factory=dict)
    strategies: dict[str, str | None] = field(default_factory=dict)

    def is_auction(self, address: str) -> bool:
        if address not in self.auctions:
            self.auctions[address] = _is_auction(self.client, address)
        return self.auctions[address]

    def auction_of(self, strategy: str) -> str | None:
        if strategy not in self.strategies:
            auction = _address(call_view(self.client, strategy, "auction()", "address"))
            valid = auction is not None and int(auction, 16) != 0 and self.is_auction(auction)
            self.strategies[strategy] = auction if valid else None
        return self.strategies[strategy]


def _classify(chain_id: int, probe: _Probe, target: str, call: DecodedCall) -> tuple[str, AuctionCall] | None:
    """(auction address, call entry) for a call that touches an auction, else None."""
    name = call.function_name
    if name in AUCTION_CALLS and probe.is_auction(target):
        value = call.params[-1][1] if call.params else None
        kind = "kick" if name == "kick" else "auction"
        return target, AuctionCall(target, call, kind, function=name, value=value)
    auction = probe.auction_of(target)
    if auction is None:
        return None
    if name == SET_AUCTION_CALL and call.params and call.params[0][0] == "address":
        return auction, AuctionCall(target, call, "set_auction", value=str(call.params[0][1]))
    if name in KICKING_CALLS:
        token = call.params[0][1] if name == "kickAuction" and call.params else None
        confirmed = name == "kickAuction" or _harvest_kicks(chain_id, target)
        return auction, AuctionCall(target, call, "kick", value=token, confirmed=confirmed)
    if name == MIN_AMOUNT_CALL:
        return auction, AuctionCall(target, call, "min_amount")
    forwarded = _forwarded(chain_id, target, call)
    if forwarded is None:
        return None
    return auction, AuctionCall(target, call, "forward", function=forwarded[0], value=forwarded[1])


def _read_lot(
    chain_id: int, client: Web3Client, auction: str, token: str, strategy: str | None, single: bool
) -> Lot | None:
    """One enabled token's pending balance and state, plus the strategy's side when known."""
    meta = fetch_erc20_metadata(chain_id, token)
    if meta is None:
        return None
    lot = Lot(
        token=token,
        symbol=meta.symbol,
        decimals=meta.decimals,
        in_auction=_uint(client, token, "balanceOf(address)", auction) or 0,
        kicked=_uint(client, auction, "kicked(address)", token) or 0,
        active=call_view(client, auction, "isActive(address)", "bool", (token,)) is True,
        available=_uint(client, auction, "available(address)", token) or 0,
    )
    if strategy:
        lot.strategy_held = _uint(client, token, "balanceOf(address)", strategy)
        lot.min_amount_to_sell = _uint(client, strategy, "minAmountToSell(address)", token)
        # claimableRewards() has no token argument, so it belongs to the lot only when there is one.
        if single:
            lot.claimable = _uint(client, strategy, "claimableRewards()")
    return lot


def _read_auction(chain_id: int, client: Web3Client, auction: str, strategy: str | None) -> YearnAuctionContext | None:
    """Read an Auction's settings and lots; None when it lacks the getters this adapter needs."""
    want = _address(call_view(client, auction, "want()", "address"))
    starting_price = _uint(client, auction, "startingPrice()")
    tokens = call_view(client, auction, "getAllEnabledAuctions()", "address[]")
    if want is None or starting_price is None or not isinstance(tokens, (list, tuple)):
        return None
    receiver = _address(call_view(client, auction, "receiver()", "address")) or ""
    if strategy is None and receiver and _address(call_view(client, receiver, "auction()", "address")) == auction:
        strategy = receiver
    want_meta = fetch_erc20_metadata(chain_id, want)
    version = call_view(client, auction, "version()", "string")
    only_gov = call_view(client, auction, "governanceOnlyKick()", "bool")
    context = YearnAuctionContext(
        address=auction,
        version=version if isinstance(version, str) else "",
        want=want,
        want_symbol=want_meta.symbol if want_meta else "want",
        want_decimals=want_meta.decimals if want_meta else 18,
        receiver=receiver,
        governance=_address(call_view(client, auction, "governance()", "address")) or "",
        governance_only_kick=only_gov if isinstance(only_gov, bool) else None,
        minimum_price=_uint(client, auction, "minimumPrice()") or 0,
        starting_price=starting_price,
        step_decay_rate=_uint(client, auction, "stepDecayRate()"),
        step_duration=_uint(client, auction, "stepDuration()"),
        auction_length=_uint(client, auction, "auctionLength()"),
        now=int(client.eth.get_block("latest")["timestamp"]),
        strategy=strategy,
    )
    checksummed = [a for a in (_address(t) for t in tokens[:MAX_LOTS]) if a]
    for token in checksummed:
        lot = _read_lot(chain_id, client, auction, token, strategy, single=len(checksummed) == 1)
        if lot is not None:
            context.lots.append(lot)
    if strategy:
        context.strategy_total_assets = _uint(client, strategy, "totalAssets()")
        context.strategy_idle_want = _uint(client, want, "balanceOf(address)", strategy)
    _attach_market_prices(chain_id, context)
    return context


def _attach_market_prices(chain_id: int, context: YearnAuctionContext) -> None:
    """Price each lot in want from DeFiLlama USD prices; best-effort, a miss leaves it None."""
    want_key = defillama_key(chain_id, context.want)
    keys = {lot.token: defillama_key(chain_id, lot.token) for lot in context.lots}
    if want_key is None or not context.lots:
        return
    try:
        prices = fetch_prices([want_key, *(k for k in keys.values() if k)])
    except Exception as error:  # noqa: BLE001 - a market price is an enrichment
        logger.info("DeFiLlama prices failed for auction %s: %s", context.address, error)
        return
    want_usd = prices.get(want_key)
    if not want_usd:
        return
    for lot in context.lots:
        key = keys.get(lot.token)
        token_usd = prices.get(key) if key else None
        if token_usd:
            lot.market_price = token_usd / want_usd


def resolve_yearn_auction_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[YearnAuctionContext]:
    """Resolve pricing, lots and old → new changes for every Yearn Auction the calls touch.

    ``protocol`` is unused: the auction code is the same whoever runs it.
    """
    del protocol
    candidates = [(target, call) for target, call in targets_and_calls if _is_candidate(call.function_name)]
    if not candidates:
        return []
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    probe = _Probe(client)

    grouped: dict[str, list[AuctionCall]] = {}
    strategies: dict[str, str] = {}
    for target, call in candidates:
        try:
            classified = _classify(chain_id, probe, to_checksum_address(target), call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Yearn auction classification failed for %s: %s", target, error)
            continue
        if classified is None:
            continue
        auction, entry = classified
        grouped.setdefault(auction, []).append(entry)
        if entry.target.lower() != auction.lower():
            strategies.setdefault(auction, entry.target)

    contexts: list[YearnAuctionContext] = []
    for auction, calls in grouped.items():
        try:
            context = _read_auction(chain_id, client, auction, strategies.get(auction))
            if context is None:
                continue
            context.calls = calls
            for address in context.addresses:
                label = get_contract_label(chain_id, address)
                if label:
                    context.labels[address] = label
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Yearn auction context failed for %s: %s", auction, error)
            continue
        contexts.append(context)
    return contexts


def format_yearn_auction_prompt(contexts: list[YearnAuctionContext]) -> str:
    """Render verified auction settings, lots and changes for the LLM prompt."""
    sections: list[str] = []
    for context in contexts:

        def render(address: str, labels: dict[str, str] = context.labels) -> str:
            checksum = _address(address) or address
            label = labels.get(checksum)
            return f"{checksum} ({label})" if label else checksum

        lines = [
            *context.header_lines(render),
            "Proposed by this transaction (in batch order):",
            *(f"  - {line}" for line in context.proposal_lines(render)),
        ]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def format_yearn_auction_report(
    contexts: list[YearnAuctionContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic auction section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        merged = {**context.labels, **labels}

        def render(address: str, merged: dict[str, str] = merged) -> str:
            return address_link(address, chain_id, merged)

        header, *rest = context.header_lines(render)
        lines = [f"**{header}**", *(f"- {line}" for line in rest), "- **Proposed:**"]
        lines.extend(f"  - {line}" for line in context.proposal_lines(render))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
