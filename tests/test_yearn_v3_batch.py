"""Tests for utils/llm/yearn_v3_batch.py — the V3 vault _update_debt model."""

import unittest

from utils.llm.yearn_v3_batch import VaultBatchState


def _state(idle: int = 0, minimum: int = 0, shutdown: bool = False) -> VaultBatchState:
    return VaultBatchState(idle=idle, minimum_total_idle=minimum, shutdown=shutdown)


class TestUpdateDebt(unittest.TestCase):
    def test_deposit_capped_by_available_idle_above_minimum(self) -> None:
        state = _state(idle=100, minimum=30)
        state.add_strategy("0xA", max_deposit=None, max_withdraw=None)
        state.set_max_debt("0xA", 1_000)
        move = state.update_debt("0xA", 1_000)
        self.assertEqual((move.moved, move.idle_after), (70, 30))
        self.assertIn("available idle", move.limited_by)

    def test_idle_at_or_below_minimum_moves_nothing(self) -> None:
        state = _state(idle=30, minimum=30)
        state.add_strategy("0xA", None, None)
        state.set_max_debt("0xA", 1_000)
        self.assertEqual(state.update_debt("0xA", 500).moved, 0)

    def test_deposit_capped_by_max_deposit_and_spends_it(self) -> None:
        state = _state(idle=1_000)
        state.add_strategy("0xA", max_deposit=300, max_withdraw=None)
        state.set_max_debt("0xA", 1_000)
        first = state.update_debt("0xA", 1_000)
        self.assertEqual(first.moved, 300)
        self.assertIn("maxDeposit", first.limited_by)
        self.assertEqual(state.update_debt("0xA", 2_000).moved, 0)

    def test_zero_max_deposit(self) -> None:
        state = _state(idle=1_000)
        state.add_strategy("0xA", max_deposit=0, max_withdraw=None)
        state.set_max_debt("0xA", 1_000)
        self.assertEqual(state.update_debt("0xA", 500).limited_by, "strategy maxDeposit is 0")

    def test_withdrawal_keeps_minimum_idle_and_feeds_later_deposits(self) -> None:
        state = _state(idle=0, minimum=0)
        state.add_strategy("0xA", None, None)
        state.strategies["0xa"].current_debt = 500
        state.add_strategy("0xB", None, None)
        state.set_max_debt("0xB", 10_000)
        self.assertEqual(state.update_debt("0xA", 0).moved, -500)
        self.assertEqual(state.update_debt("0xB", 10_000).moved, 500)
        self.assertEqual(state.idle, 0)

    def test_withdrawal_raised_to_restore_minimum_idle(self) -> None:
        # Mirrors the vault: a small reduction is raised so idle reaches minimum_total_idle.
        state = _state(idle=0, minimum=100)
        state.add_strategy("0xA", None, None)
        state.strategies["0xa"].current_debt = 500
        self.assertEqual(state.update_debt("0xA", 450).moved, -100)

    def test_shutdown_forces_a_full_withdrawal(self) -> None:
        state = _state(shutdown=True)
        state.add_strategy("0xA", None, None)
        state.strategies["0xa"].current_debt = 500
        move = state.update_debt("0xA", 800)
        self.assertEqual(move.moved, -500)
        self.assertIn("shut down", move.limited_by)

    def test_revoked_strategy_reverts(self) -> None:
        state = _state(idle=100)
        state.add_strategy("0xA", None, None)
        state.revoke("0xA")
        self.assertEqual(state.update_debt("0xA", 10).reverts, "inactive strategy")

    def test_addresses_are_case_insensitive(self) -> None:
        state = _state(idle=100)
        state.add_strategy("0xAbC", None, None)
        state.set_max_debt("0xabc", 50)
        self.assertEqual(state.update_debt("0xABC", 50).moved, 50)


class TestParentVaultFlows(unittest.TestCase):
    """The inner vault's side of a parent vault's deposit or redemption."""

    def test_auto_allocated_deposit_stays_redeemable(self) -> None:
        state = _state(idle=0)
        state.add_strategy("0xA", max_deposit=500, max_withdraw=100)
        state.set_max_debt("0xA", 1_000)
        state.strategies["0xa"].current_debt = 100
        move = state.receive(50, "0xA")
        assert move is not None
        self.assertEqual((move.debt_before, move.debt_after, state.idle), (100, 150, 0))
        strategy = state.strategies["0xa"]
        self.assertEqual((strategy.max_withdraw, strategy.max_deposit), (150, 450))
        # A later redemption in the same batch can take the deposit back out.
        self.assertEqual(state.redeem(150, ["0xa"]), (0, [("0xa", 150, 0)], 0))

    def test_auto_allocation_respects_the_vault_deposit_limits(self) -> None:
        # Debt 90 under a 100 ceiling, deposit room 5: the vault allocates 5 and keeps 45 idle.
        state = _state(idle=0)
        state.add_strategy("0xA", max_deposit=5, max_withdraw=None)
        state.set_max_debt("0xA", 100)
        state.strategies["0xa"].current_debt = 90
        move = state.receive(50, "0xA")
        assert move is not None
        self.assertEqual((move.debt_after, state.idle), (95, 45))
        self.assertIn("maxDeposit", move.limited_by)

    def test_auto_allocation_sweeps_idle_above_the_minimum(self) -> None:
        state = _state(idle=30, minimum=10)
        state.add_strategy("0xA", None, None)
        state.set_max_debt("0xA", 1_000)
        move = state.receive(50, "0xA")
        assert move is not None
        self.assertEqual((move.moved, state.idle), (70, 10))

    def test_deposit_without_auto_allocate_lands_in_idle(self) -> None:
        state = _state(idle=10)
        state.add_strategy("0xA", None, None)
        self.assertIsNone(state.receive(50))
        self.assertEqual(state.idle, 60)


if __name__ == "__main__":
    unittest.main()
