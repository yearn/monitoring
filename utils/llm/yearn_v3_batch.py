"""Model a Yearn V3 vault's debt accounting across one transaction's calls.

``update_debt(strategy, target_debt)`` is a *target*, not an amount: the vault
moves toward it only as far as its own limits allow. Rendering ``target −
current_debt`` as "deposits X" reported a 50M deposit into a vault holding
27M — and an LLM then flagged a funding gap that cannot exist. Calls in one
batch also compound: a withdrawal fills idle that a later deposit spends.

This module mirrors the V3 vault's ``_update_debt`` (3.0.x and 3.1.x) over a
running copy of the vault state, so each call is described against the state
the earlier calls leave:

- decrease: withdraw ``current − target``, raised to keep ``minimum_total_idle``,
  capped by what the strategy can redeem (``convertToAssets(maxRedeem(vault))``);
- increase: the target is capped by ``max_debt``, the deposit by the strategy's
  ``maxDeposit(vault)`` and by idle above ``minimum_total_idle``;
- a shut-down vault can only pull (target forced to 0); ``target == current``
  reverts ("new debt equals current debt"); inactive strategies revert.

Amounts are expectations from pre-transaction state: realized withdrawals can
differ by strategy losses or rounding, which the vault accounts for on-chain.

A strategy can itself be a V3 vault that the same batch also manages. Moving
debt into or out of it changes that vault's own state: a deposit lands in its
idle — and with ``auto_allocate`` the vault then runs ``_update_debt(first queue
strategy, max)``, which moves all idle above ``minimum_total_idle`` within the
strategy's ``max_debt`` and ``maxDeposit``, not just the new deposit — and a redemption
is served from its idle, then from its default queue in order. ``receive`` and
``redeem`` apply that side, so a later call on the inner vault sees it.
"""

from dataclasses import dataclass, field

MAX_UINT256 = 2**256 - 1


@dataclass
class StrategyDebt:
    """A strategy's registration on the vault, as the batch changes it."""

    current_debt: int
    max_debt: int
    active: bool
    max_deposit: int | None = None  # strategy.maxDeposit(vault); None when unread
    max_withdraw: int | None = None  # strategy.convertToAssets(strategy.maxRedeem(vault)); None when unread

    def credit(self, amount: int) -> None:
        """Book a deposit: debt rises, deposit room shrinks, and the deposit is redeemable again.

        Shared by update_debt deposits and a parent vault's auto-allocated deposit,
        so exit liquidity and later redemptions in the batch see the same limits.
        """
        self.current_debt += amount
        if self.max_deposit is not None:
            self.max_deposit -= amount
        if self.max_withdraw is not None:
            self.max_withdraw += amount


@dataclass(frozen=True)
class DebtMove:
    """What one ``update_debt`` call is expected to do."""

    target: int
    debt_before: int
    debt_after: int
    idle_before: int
    idle_after: int
    limited_by: str = ""  # why the move stopped short of the target; "" when it reaches it
    reverts: str = ""  # revert reason when the call cannot succeed

    @property
    def moved(self) -> int:
        """Assets moved: positive for a deposit into the strategy, negative for a withdrawal."""
        return self.debt_after - self.debt_before


@dataclass
class VaultBatchState:
    """Running vault state: idle, the idle floor, shutdown, and per-strategy debt."""

    idle: int
    minimum_total_idle: int
    shutdown: bool
    strategies: dict[str, StrategyDebt] = field(default_factory=dict)
    # The move the latest update_debt made, so a caller can pass it on to an inner vault.
    last_move: DebtMove | None = None

    def get(self, address: str) -> StrategyDebt | None:
        return self.strategies.get(address.lower())

    def add_strategy(self, address: str, max_deposit: int | None, max_withdraw: int | None) -> None:
        """Registration starts with no debt and a zero ceiling."""
        self.strategies[address.lower()] = StrategyDebt(0, 0, True, max_deposit, max_withdraw)

    def set_max_debt(self, address: str, max_debt: int) -> None:
        strategy = self.get(address)
        if strategy is not None:
            strategy.max_debt = max_debt

    def revoke(self, address: str) -> None:
        strategy = self.get(address)
        if strategy is not None:
            strategy.active = False

    def update_debt(self, address: str, target: int) -> DebtMove:
        """Apply one ``update_debt`` to the running state, mirroring the vault."""
        self.last_move = self._update_debt(address, target)
        return self.last_move

    def receive(self, amount: int, auto_allocate_to: str | None = None) -> DebtMove | None:
        """Take a deposit from a parent vault, mirroring the vault's ``_deposit``.

        The assets land in idle; with ``auto_allocate`` the vault then calls
        ``_update_debt(first_queue_strategy, max_uint256)``, which this applies
        with every deposit limit. Returns that move, or None without auto_allocate.
        """
        self.idle += amount
        strategy = self.get(auto_allocate_to) if auto_allocate_to else None
        if strategy is None or not strategy.active or self.shutdown or auto_allocate_to is None:
            return None
        return self._update_debt(auto_allocate_to, MAX_UINT256)

    def redeem(self, amount: int, queue: list[str]) -> tuple[int, list[tuple[str, int, int]], int]:
        """Serve a parent vault's redemption: idle first, then the default queue in order.

        Returns:
            (taken from idle, [(strategy, debt before, debt after)], amount no strategy could supply).
        """
        from_idle = min(self.idle, amount)
        self.idle -= from_idle
        remaining = amount - from_idle
        pulls: list[tuple[str, int, int]] = []
        for address in queue:
            if remaining == 0:
                break
            strategy = self.get(address)
            if strategy is None or not strategy.active or strategy.current_debt == 0:
                continue
            available = strategy.current_debt
            if strategy.max_withdraw is not None:
                available = min(available, strategy.max_withdraw)
            taken = min(available, remaining)
            if taken == 0:
                continue
            before = strategy.current_debt
            strategy.current_debt -= taken
            if strategy.max_withdraw is not None:
                strategy.max_withdraw -= taken
            remaining -= taken
            pulls.append((address, before, strategy.current_debt))
        return from_idle, pulls, remaining

    def _update_debt(self, address: str, target: int) -> DebtMove:
        strategy = self.get(address)
        if strategy is None or not strategy.active:
            return DebtMove(target, 0, 0, self.idle, self.idle, reverts="inactive strategy")

        current = strategy.current_debt
        new_debt = 0 if self.shutdown else target
        if new_debt == current:
            return DebtMove(target, current, current, self.idle, self.idle, reverts="new debt equals current debt")
        if current > new_debt:
            return self._withdraw(strategy, target, current - new_debt)
        return self._deposit(strategy, target, new_debt)

    def _withdraw(self, strategy: StrategyDebt, target: int, wanted: int) -> DebtMove:
        current, idle_before = strategy.current_debt, self.idle
        amount, limited_by = wanted, ""
        if self.idle + amount < self.minimum_total_idle:
            amount = min(self.minimum_total_idle - self.idle, current)
        if strategy.max_withdraw is not None and strategy.max_withdraw < amount:
            amount, limited_by = strategy.max_withdraw, "what the strategy can redeem now"
        if self.shutdown and target != 0:
            limited_by = limited_by or "vault is shut down, so the target is forced to 0"
        if amount == 0:
            return DebtMove(target, current, current, idle_before, idle_before, limited_by=limited_by)

        strategy.current_debt -= amount
        if strategy.max_withdraw is not None:
            strategy.max_withdraw -= amount
        self.idle += amount
        return DebtMove(target, current, strategy.current_debt, idle_before, self.idle, limited_by=limited_by)

    def _deposit(self, strategy: StrategyDebt, target: int, new_debt: int) -> DebtMove:
        current, idle_before = strategy.current_debt, self.idle
        limited_by = ""
        if new_debt > strategy.max_debt:
            new_debt, limited_by = strategy.max_debt, "the strategy's max_debt"
            if new_debt <= current:
                return DebtMove(target, current, current, idle_before, idle_before, limited_by=limited_by)
        if strategy.max_deposit == 0:
            return DebtMove(target, current, current, idle_before, idle_before, limited_by="strategy maxDeposit is 0")

        amount = new_debt - current
        if strategy.max_deposit is not None and amount > strategy.max_deposit:
            amount, limited_by = strategy.max_deposit, "the strategy's maxDeposit"
        available = self.idle - self.minimum_total_idle if self.idle > self.minimum_total_idle else 0
        if amount > available:
            amount, limited_by = available, "the vault's available idle (idle above minimum_total_idle)"
        if amount == 0:
            return DebtMove(target, current, current, idle_before, idle_before, limited_by=limited_by)

        strategy.credit(amount)
        self.idle -= amount
        return DebtMove(target, current, strategy.current_debt, idle_before, self.idle, limited_by=limited_by)
