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
    # auctions(token).initialAvailable: the size of the last kicked lot; None when unreadable.
    initial_available: int | None = None

    def amount(self, raw: int) -> str:
        """Render a token amount."""
        return format_asset_amount(raw, self.decimals, self.symbol)


@dataclass
class _StrategySide:
    """A strategy's own balance, claimable rewards and kick threshold for one token.

    One object per (strategy, token), shared by every auction the strategy uses in the
    batch, so a ``setAuction`` hands the running balances to the next auction.
    """

    held: int | None = None
    claimable: int | None = None
    min_amount: int | None = None

    def fill(self, lot: Lot) -> None:
        """Take the pre-batch values this side still lacks from a lot's snapshot."""
        if self.held is None:
            self.held = lot.strategy_held
        if self.claimable is None:
            self.claimable = lot.claimable
        if self.min_amount is None:
            self.min_amount = lot.min_amount_to_sell


@dataclass
class _LotRun:
    """A lot as the calls so far leave it: the auction's balance and kick, and the strategy's side."""

    in_auction: int
    kicked: int
    initial_available: int | None
    side: _StrategySide
    kicked_in_batch: bool = False
    # False once `disable` deletes the lot's state; the auction no longer sells or counts it.
    enabled: bool = True

    @classmethod
    def of(cls, lot: Lot, side: _StrategySide) -> "_LotRun":
        """The lot before the transaction."""
        return cls(lot.in_auction, lot.kicked, lot.initial_available, side)

    @property
    def held(self) -> int | None:
        return self.side.held

    @held.setter
    def held(self, value: int | None) -> None:
        self.side.held = value

    @property
    def claimable(self) -> int | None:
        return self.side.claimable

    @claimable.setter
    def claimable(self, value: int | None) -> None:
        self.side.claimable = value

    @property
    def min_amount(self) -> int | None:
        return self.side.min_amount

    @min_amount.setter
    def min_amount(self, value: int | None) -> None:
        self.side.min_amount = value


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
    # Position in the transaction, to replay a strategy's calls across auctions in order.
    order: int = 0


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
    # Every auction context resolved for the same transaction, this one included: calls are
    # replayed across all of them in transaction order (see `walk_auctions`).
    peers: list["YearnAuctionContext"] = field(default_factory=list, repr=False, compare=False)

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
        length = format_duration(self.auction_length) if self.auction_length else "unknown"
        return (
            f"startingPrice = {self.want_amount(normalize(self.starting_price, 18))} for the whole lot, which "
            f"{decay}; auction length {length}; minimumPrice = {self.price(self.minimum_price, lot)} "
            f"(raw {self.minimum_price}, 1e18-scaled)."
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
        """The strategy's idle want, stated without guessing where it came from."""
        idle = self.strategy_idle_want or 0
        line = (
            f"The strategy holds {format_asset_amount(idle, self.want_decimals, self.want_symbol)} idle (not deployed)."
        )
        if self.strategy and self.receiver.lower() == self.strategy.lower():
            line += (
                " This auction pays sale proceeds to the strategy, so a sold lot is one possible source; the balance "
                "alone does not show where it came from."
            )
        if self.strategy_total_assets:
            share = idle / self.strategy_total_assets * 100
            line += (
                " A report() counts it in totalAssets, and whatever arrived after the last report is booked as profit "
                f"(all of it would be {share:.3f}% of totalAssets)."
            )
        return line

    def proposal_lines(self, render: Render) -> list[str]:
        """This auction's calls, replayed with every peer auction's calls in transaction order."""
        return walk_auctions(self.peers or [self], render)[id(self)]

    def initial_settings(self) -> dict[str, object]:
        """Pricing and receiver before the transaction."""
        return {
            "minimumPrice": self.minimum_price,
            "startingPrice": self.starting_price,
            "stepDecayRate": self.step_decay_rate,
            "stepDuration": self.step_duration,
            "receiver": self.receiver,
        }

    def step(
        self, entry: AuctionCall, settings: dict[str, object], runs: dict[str, _LotRun], render: Render
    ) -> list[str]:
        """Describe one call against the running settings and lots, and apply it to them."""
        name = entry.call.function_name
        if entry.kind == "kick":
            return self._kick_lines(entry, settings, runs)
        if entry.kind == "min_amount":
            return [self._min_amount_line(entry, runs)]
        if entry.kind == "set_auction":
            return [
                f"{name}: the strategy's auction {render(self.address)} → {render(str(entry.value))}. Its later "
                "calls in this batch use the new auction and are described with it, if it is a readable Auction."
            ]
        if entry.function in AUCTION_SETTERS:
            via = (
                f"{name} on the strategy forwards to Auction.{entry.function} (verified body)"
                if entry.kind == "forward"
                else entry.function
            )
            invalid = self._invalid_setting(entry.function, entry.value)
            if invalid:
                return [f"{via}: REVERTS — {invalid}."]
            live_before = {token: self._is_live(token, runs[token], settings) for token in runs}
            line = f"{via}: {self._setting_change(entry.function, settings, entry.value, render)}"
            guard, reverts = self._live_guard(live_before)
            if reverts:
                # The call does not land: later calls must see the settings it would have replaced.
                return [line + guard]
            settings[AUCTION_SETTERS[entry.function]] = entry.value
            return [line + guard + self._revival_note(live_before, runs, settings)]
        if entry.function in ("enable", "disable"):
            return [self._enable_line(entry, runs, settings, render)]
        if entry.function == "sweep":
            lot = self.lot_for(entry.value)
            run = runs.get(lot.token.lower()) if lot else None
            held = f" ({lot.amount(run.in_auction)})" if lot and run else ""
            if run is not None:
                run.in_auction = 0
            return [
                f"sweep: sends the auction's entire balance of {lot.symbol if lot else render(str(entry.value))}"
                f"{held} to the caller (governance) — those tokens leave the sale."
            ]
        if entry.function == "setGovernanceOnlyKick":
            return [f"setGovernanceOnlyKick: {self.governance_only_kick} → {entry.value}."]
        return []

    def _enable_line(
        self, entry: AuctionCall, runs: dict[str, _LotRun], settings: dict[str, object], render: Render
    ) -> str:
        """`enable` / `disable` against the lot's running state; `disable` deletes the lot on-chain."""
        name = entry.call.function_name
        lot = self.lot_for(entry.value)
        run = runs.get(lot.token.lower()) if lot else None
        if lot is None or run is None:
            verb = "starts selling" if entry.function == "enable" else "stops selling"
            return f"{name}: the auction {verb} {render(str(entry.value))}."
        if entry.function == "enable":
            if run.enabled:
                return f"{name}: REVERTS — {lot.symbol} is already enabled (`already enabled`)."
            run.enabled = True
            return f"{name}: the auction sells {lot.symbol} again; its next kick starts a new lot."
        if not run.enabled:
            return f"{name}: REVERTS — {lot.symbol} is not enabled (`not enabled`)."
        was_live = self._is_live(lot.token.lower(), run, settings)
        run.enabled, run.kicked, run.initial_available, run.kicked_in_batch = False, 0, None, False
        line = f"{name}: the auction stops selling {lot.symbol} and deletes its lot state"
        if was_live:
            line += " — the live lot ends, so it no longer counts as an active auction for setters"
        if run.in_auction:
            line += f"; the {lot.amount(run.in_auction)} it holds stays in the auction until swept or re-enabled"
        return line + "."

    def _invalid_setting(self, function: str, value: object) -> str:
        """Why the verified Auction 1.0.5 rejects a setter's value, or ""."""
        if self.version != "1.0.5":
            return ""
        if function == "setStartingPrice" and isinstance(value, int) and value <= 10**9:
            return "startingPrice must exceed 1e9 (`starting price`)"
        if function == "setStepDecayRate" and isinstance(value, int) and not 0 < value < 10_000:
            return "stepDecayRate must be between 1 and 9,999 bps (`invalid decay rate`)"
        if function == "setStepDuration" and isinstance(value, int) and not 0 < value < (self.auction_length or 86_400):
            return "stepDuration must be above 0 and below the auction length (`invalid step duration`)"
        if function == "setReceiver" and isinstance(value, str) and value.startswith("0x") and int(value, 16) == 0:
            return "the receiver cannot be the zero address (`ZERO ADDRESS`)"
        return ""

    def _is_live(self, token: str, run: _LotRun, settings: dict[str, object]) -> bool:
        """Whether a lot is live under the running settings, mirroring ``Auction._price``.

        Pricing setters apply to a lot already kicked: lowering the floor can revive a
        lot whose decayed price had fallen below the old one. Evaluated at the time the
        state was read; falls back to the on-chain ``isActive`` when a setting is unknown.
        """
        lot = self.lot_for(token)
        if lot is None or not run.enabled or not run.kicked:
            return False
        start, floor = settings["startingPrice"], settings["minimumPrice"]
        decay, step = settings["stepDecayRate"], settings["stepDuration"]
        if not (
            isinstance(start, int)
            and isinstance(floor, int)
            and isinstance(decay, int)
            and isinstance(step, int)
            and step
            and 0 < decay < 10_000
            and self.auction_length
            and run.initial_available
        ):
            return run.kicked_in_batch or lot.active
        elapsed = 0 if run.kicked_in_batch else self.now - run.kicked
        if elapsed > self.auction_length:
            return False
        per_token = start * WAD / (run.initial_available * 10 ** (18 - lot.decimals))
        price = per_token * (1 - decay / 10_000) ** (elapsed // step)
        return price > 0 and price >= floor

    def _live_guard(self, live: dict[str, bool]) -> tuple[str, bool]:
        """A note when a lot is live while a pricing or receiver setter runs, and whether the call reverts.

        Auction 1.0.5 rejects these setters while any lot is live; other versions are not verified.
        """
        if not any(live.values()):
            return "", False
        symbols = ", ".join(lot.symbol for lot in self.lots if live.get(lot.token.lower()))
        if self.version == "1.0.5":
            return (
                f" A {symbols} lot is live at this point, and Auction 1.0.5 rejects this setter while any lot is live "
                "(`active auction`), so the call reverts and the setting is unchanged.",
                True,
            )
        return f" A {symbols} lot is live at this point; this Auction version may reject setters while one is.", False

    def _revival_note(self, live_before: dict[str, bool], runs: dict[str, _LotRun], settings: dict[str, object]) -> str:
        """Lots the new settings bring back to life: their decayed price is above the floor now in force."""
        notes = []
        for token, run in runs.items():
            lot = self.lot_for(token)
            if lot is None or live_before.get(token) or not self._is_live(token, run, settings):
                continue
            notes.append(
                f" This revives the {lot.symbol} lot kicked {format_utc(run.kicked)}: its decayed price is above the "
                f"settings now in force, so it is live again — a strategy report() later in this batch does not kick a "
                f"new {lot.symbol} lot while it holds {lot.amount(run.in_auction)}."
            )
        return "".join(notes)

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
                    f"{_versus(normalize(value, 18), lot.market_price)}."
                )
            return line
        if setting == "startingPrice":
            before_text = self.want_amount(normalize(old, 18)) if old is not None else "unknown"
            return f"startingPrice {before_text} → {self.want_amount(normalize(value, 18))} for the whole lot."
        if setting == "stepDecayRate":
            return f"stepDecayRate {old} → {value} bps per step."
        return f"stepDuration {format_duration(old) if old else 'unknown'} → {format_duration(value)}."

    def _kick_lines(self, entry: AuctionCall, settings: dict[str, object], runs: dict[str, _LotRun]) -> list[str]:
        """A kick, direct or inside a strategy report, against the lots and settings the earlier calls leave."""
        name = entry.call.function_name
        tokens = [str(entry.value).lower()] if entry.value is not None else [lot.token.lower() for lot in self.lots]
        from_strategy = entry.target.lower() != self.address.lower()
        lines: list[str] = []
        for token in tokens:
            lot, run = self.lot_for(token), runs.get(token)
            if lot is None or run is None:
                continue
            claim_known = run.held is not None and run.claimable is not None
            balance = (run.held or 0) + (run.claimable or 0) if from_strategy else 0
            if self._is_live(token, run, settings) and (run.in_auction or not from_strategy):
                state = "kicked earlier in this batch and still live" if run.kicked_in_batch else "live"
                if not from_strategy:
                    lines.append(f"{name}: REVERTS — the {lot.symbol} lot is {state} (`too soon`).")
                    continue
                line = f"{name}: the {lot.symbol} lot is {state}, so the strategy does not kick a new one"
                if claim_known:
                    line += f"; the ≈ {lot.amount(balance)} it holds after claiming stays in the strategy"
                    run.held, run.claimable = balance, 0
                lines.append(line + ".")
                continue
            if from_strategy and run.min_amount is not None and claim_known and balance <= run.min_amount:
                lines.append(
                    f"{name}: does not kick {lot.symbol} — the strategy's balance after claiming (≈ "
                    f"{lot.amount(balance)}) does not exceed minAmountToSell {lot.amount(run.min_amount)}; the "
                    "claimed rewards stay in the strategy."
                )
                run.held, run.claimable = balance, 0
                continue
            if not run.enabled:
                lines.append(
                    f"{name}: REVERTS — {lot.symbol} is disabled on the auction (`not enabled`)"
                    + ("; the kick inside it fails, so the whole call reverts." if from_strategy else ".")
                )
                continue
            size = run.in_auction + balance
            if size == 0:
                lines.append(
                    f"{name}: REVERTS — nothing to kick: the auction holds no {lot.symbol} (`nothing to kick`)."
                    if not from_strategy
                    else f"{name}: no {lot.symbol} to kick — neither the auction nor the strategy holds any."
                )
                continue
            parts = [f"{lot.amount(run.in_auction)} already in the auction"]
            if from_strategy:
                parts.append(f"{lot.amount(run.held or 0)} the strategy holds")
                if run.claimable is not None:
                    parts.append(f"{lot.amount(run.claimable)} it claims")
            gate = ""
            if from_strategy and run.min_amount is not None and not claim_known:
                gate = (
                    f" The strategy kicks only if its own {lot.symbol} balance after claiming exceeds "
                    f"{lot.amount(run.min_amount)}."
                )
            verb = (
                f"kicks the {lot.symbol} auction"
                if entry.confirmed
                else f"may kick the {lot.symbol} auction (its harvest is not confirmed to kick from verified source); "
                "if it does, it kicks it"
            )
            line = (
                f"{name}: {verb}. The lot is the auction's whole balance — ≈ "
                f"{lot.amount(size)} ({' + '.join(parts)}).{gate}"
            )
            floor = settings["minimumPrice"]
            if isinstance(floor, int) and floor:
                proceeds = normalize(size, lot.decimals) * normalize(floor, 18)
                line += (
                    f" At the minimumPrice then in force ({self.price(floor, lot)}) the full lot clears for at least "
                    f"{self.want_amount(proceeds)}"
                )
                if lot.market_price is not None:
                    worth = normalize(size, lot.decimals) * lot.market_price
                    line += f"; at market it is worth ≈ {self.want_amount(worth)}"
                line += "."
            line += self._time_to_floor(size, lot, settings)
            lines.append(line)
            run.in_auction, run.kicked, run.initial_available, run.kicked_in_batch = size, self.now, size, True
            if from_strategy:
                run.held = 0 if run.held is not None else None
                run.claimable = 0 if run.claimable is not None else None
        return lines

    def _time_to_floor(self, size: int, lot: Lot, settings: dict[str, object]) -> str:
        """How long the decay takes to reach the floor for a lot of ``size`` tokens, under the running settings."""
        start, floor = settings["startingPrice"], settings["minimumPrice"]
        decay, step = settings["stepDecayRate"], settings["stepDuration"]
        if not (
            isinstance(start, int)
            and isinstance(floor, int)
            and isinstance(decay, int)
            and isinstance(step, int)
            and floor
            and start > 0
            and size
            and 0 < decay < 10_000
            and step > 0
        ):
            return ""
        opening = normalize(start, 18) / normalize(size, lot.decimals)
        ratio = float(normalize(floor, 18) / opening)
        if ratio >= 1:
            return (
                f" The lot's starting price ({_sig(opening)} {self.want_symbol} per {lot.symbol}) is already at or "
                "below the floor, so it is never live."
            )
        steps = math.ceil(math.log(ratio) / math.log(1 - decay / 10_000))
        seconds = steps * step
        if self.auction_length and seconds >= self.auction_length:
            return f" It opens at {_sig(opening)} {self.want_symbol} per {lot.symbol} and decays for the full auction."
        return (
            f" It opens at {_sig(opening)} {self.want_symbol} per {lot.symbol} and reaches the floor after "
            f"~{format_duration(seconds)}; if no taker buys by then, the rest stays unsold."
        )

    def _min_amount_line(self, entry: AuctionCall, runs: dict[str, _LotRun]) -> str:
        """``setMinAmountToSell(token, amount)``: old → new, carried into later kicks."""
        params = entry.call.params
        token = str(params[0][1]) if params else ""
        value = params[-1][1] if params else None
        lot, run = self.lot_for(token), runs.get(token.lower())
        if lot is None or run is None or not isinstance(value, int):
            return f"{entry.call.function_name}: minimum kick size for {token} → {value}."
        before = lot.amount(run.min_amount) if run.min_amount is not None else "unknown"
        run.min_amount = value
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
    match = _FORWARD_RE.search(extract_short_function_body(fetched[1], call.function_name, call.signature))
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
    body = extract_short_function_body(fetched[1], "_harvestAndReport", "_harvestAndReport()") if fetched else ""
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

    def reassign(self, strategy: str, auction: object) -> None:
        """After ``setAuction``: the strategy's later calls belong to the new auction, or to none if unreadable."""
        new = _address(auction)
        valid = new is not None and int(new, 16) != 0 and self.is_auction(new)
        self.strategies[strategy] = new if valid else None

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
        # Every Auction call this adapter reads takes its subject first: `disable(address,uint256)`
        # appends an array index, which is not the token.
        value = call.params[0][1] if call.params else None
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
    info = call_view(client, auction, "auctions(address)", "(uint64,uint64,uint128)", (token,))
    if isinstance(info, tuple) and len(info) == 3 and isinstance(info[2], int):
        lot.initial_available = info[2]
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
    for order, (target, call) in enumerate(candidates):
        try:
            classified = _classify(chain_id, probe, to_checksum_address(target), call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Yearn auction classification failed for %s: %s", target, error)
            continue
        if classified is None:
            continue
        auction, entry = classified
        entry.order = order
        grouped.setdefault(auction, []).append(entry)
        if entry.target.lower() != auction.lower():
            strategies.setdefault(auction, entry.target)
        if entry.kind == "set_auction":
            probe.reassign(entry.target, entry.value)

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
    for context in contexts:
        context.peers = contexts
    return contexts


def walk_auctions(contexts: list[YearnAuctionContext], render: Render) -> dict[int, list[str]]:
    """Replay every auction call of the transaction in order; lines keyed by ``id(context)``.

    A strategy's own balance, claimable rewards and kick threshold are one shared state
    per token across all its auctions, and calls run strictly in transaction order. With
    ``setAuction`` back and forth (A → B → A), the rewards B's kick takes are gone when A
    reports again — replaying each auction's calls as a block misattributed them.
    """
    sides: dict[tuple[str, str], _StrategySide] = {}
    states: dict[int, tuple[dict[str, object], dict[str, _LotRun]]] = {}
    for context in contexts:
        owner = (context.strategy or context.address).lower()
        runs: dict[str, _LotRun] = {}
        for lot in context.lots:
            side = sides.setdefault((owner, lot.token.lower()), _StrategySide())
            side.fill(lot)
            runs[lot.token.lower()] = _LotRun.of(lot, side)
        states[id(context)] = (context.initial_settings(), runs)

    # A stable sort keeps each context's own call order when orders tie (contexts built by hand).
    steps = sorted(
        ((entry.order, context, entry) for context in contexts for entry in context.calls), key=lambda s: s[0]
    )
    lines: dict[int, list[str]] = {id(context): [] for context in contexts}
    for _, context, entry in steps:
        settings, runs = states[id(context)]
        lines[id(context)].extend(context.step(entry, settings, runs, render))
    return lines


# How a Yearn Auction sells, from the verified Auction source; stated once per section, not per auction.
AUCTION_MECHANICS = (
    "Yearn Auctions are Dutch auctions. Each kick offers the auction's entire balance of a token — anything left "
    "unsold from an earlier lot plus what is sent in. The whole lot's price starts at startingPrice and decays every "
    "step; the lot ends after the auction length, or as soon as the per-token price falls below minimumPrice, and "
    "unsold tokens stay in the auction for the next kick. The price starts far above market and falls, so a taker "
    "normally buys near market (less their margin); minimumPrice only matters if no taker bids before the price "
    "reaches it. A floor above market therefore leaves lots unsold, and a floor below market lets them clear — the "
    "gap to market bounds what an unbid lot could lose, not what a sale is expected to lose. Lot sizes, proceeds and "
    "timings below "
    "are this adapter's forecast from state read before the transaction: rewards keep accruing until execution, and "
    "each AuctionKicked event's `available` in the simulation is the exact lot."
)


def format_yearn_auction_prompt(contexts: list[YearnAuctionContext]) -> str:
    """Render verified auction settings, lots and changes for the LLM prompt; mechanics are stated once."""
    sections: list[str] = [AUCTION_MECHANICS] if contexts else []
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
    """Render the deterministic auction section for the gist report; mechanics are stated once."""
    sections: list[str] = [f"_{AUCTION_MECHANICS}_"] if contexts else []
    for context in contexts:
        merged = {**context.labels, **labels}

        def render(address: str, merged: dict[str, str] = merged) -> str:
            return address_link(address, chain_id, merged)

        header, *rest = context.header_lines(render)
        lines = [f"**{header}**", *(f"- {line}" for line in rest), "- **Proposed:**"]
        lines.extend(f"  - {line}" for line in context.proposal_lines(render))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
