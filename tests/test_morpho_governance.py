"""Behavior tests for Morpho Vault V1 governance monitoring."""

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from protocols.morpho import governance
from protocols.morpho._alerts import VaultDiff
from protocols.morpho.governance import MarketGovernanceState
from utils.alert import AlertSeverity
from utils.chains import Chain

VAULT = "0x" + "11" * 20


def _state(market_id: str, **overrides: int) -> MarketGovernanceState:
    values: dict[str, Any] = {
        "pending_cap": 2_000_000,
        "pending_cap_timestamp": 2_000_000_000,
        "current_cap": 1_000_000,
        "removable_at": 0,
    }
    values.update(overrides)
    return MarketGovernanceState(vault_address=VAULT, market_id=market_id, **values)


class TestMorphoV1GovernanceAlerts(unittest.TestCase):
    def test_new_pending_cap_alert_uses_shared_market_metadata(self) -> None:
        state = _state("0x" + "ab" * 32)
        diff = VaultDiff()

        with (
            patch("protocols.morpho.governance.get_last_executed_morpho_from_file", return_value=0),
            patch("protocols.morpho.governance.fetch_market_info", return_value=("WETH/USDC (86.00%)", 6)),
            patch("protocols.morpho.governance.write_last_executed_morpho_to_file") as write,
        ):
            governance.check_market_governance_state("Example", state, Chain.MAINNET, diff)
            # Writes are deferred until the message is delivered.
            write.assert_not_called()
            diff.commit()

        self.assertEqual(len(diff.alerts), 1)
        body = diff.alerts[0].body
        self.assertIn("WETH/USDC (86.00%)", body)
        self.assertIn("difference: 100.00%", body)
        write.assert_called_once_with(
            state.vault_address,
            state.market_id,
            governance.PENDING_CAP_TYPE,
            state.pending_cap_timestamp,
        )

    def test_previously_alerted_market_removal_is_not_repeated(self) -> None:
        state = _state(
            "0x" + "ab" * 32, pending_cap=0, pending_cap_timestamp=0, current_cap=0, removable_at=2_000_000_000
        )
        diff = VaultDiff()

        with (
            patch(
                "protocols.morpho.governance.get_last_executed_morpho_from_file",
                return_value=state.removable_at,
            ),
            patch("protocols.morpho.governance.write_last_executed_morpho_to_file") as write,
        ):
            governance.check_market_governance_state("Example", state, Chain.MAINNET, diff)
            diff.commit()

        self.assertEqual(diff.alerts, [])
        write.assert_not_called()


METADATA = {"name": "WETH/USDC", "loan_symbol": "USDC", "loan_decimals": 6, "lltv": 860000000000000000}
REMOVAL_STATE = dict(pending_cap=0, pending_cap_timestamp=0, current_cap=0)


class _Cache:
    """In-memory stand-in for the morpho cache file, keyed like the real helpers."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], int] = {}

    def get(self, vault: str, market: str, value_type: str) -> int:
        return self.values.get((vault, market, value_type), 0)

    def write(self, vault: str, market: str, value_type: str, value: int) -> None:
        self.values[(vault, market, value_type)] = value


class TestMorphoV1MarketRemovalLoss(unittest.TestCase):
    def _run(self, state: MarketGovernanceState, cache: _Cache, now: int) -> VaultDiff:
        diff = VaultDiff()
        with (
            patch("protocols.morpho.governance.get_last_executed_morpho_from_file", side_effect=cache.get),
            patch("protocols.morpho.governance.write_last_executed_morpho_to_file", side_effect=cache.write),
            patch("protocols.morpho.governance.fetch_market_metadata", return_value=METADATA),
            patch("protocols.morpho.governance.fetch_market_info", return_value=("WETH/USDC (86.00%)", 6)),
            patch("protocols.morpho.governance.datetime") as fake_datetime,
            patch("protocols.morpho._shared.datetime") as shared_datetime,
            patch("protocols.morpho._alerts.datetime") as alerts_datetime,
        ):
            from datetime import datetime as real_datetime

            for fake in (fake_datetime, shared_datetime, alerts_datetime):
                fake.now.return_value = real_datetime.fromtimestamp(now)
                fake.fromtimestamp.side_effect = real_datetime.fromtimestamp
            governance.check_market_governance_state("Example", state, Chain.MAINNET, diff)
            diff.commit()
        return diff

    def test_removal_with_supply_is_a_standalone_high_loss_alert(self) -> None:
        removable_at = 2_000_000_000
        state = _state(
            "0x" + "ab" * 32,
            **REMOVAL_STATE,
            removable_at=removable_at,
            supply_shares=10**12,
            supply_assets=1_500_000 * 10**6,
            vault_total_assets=10_000_000 * 10**6,
        )
        diff = self._run(state, _Cache(), now=removable_at - 3 * 86400)

        self.assertEqual(len(diff.alerts), 1)
        alert = diff.alerts[0]
        self.assertEqual(alert.severity, AlertSeverity.HIGH)
        self.assertTrue(alert.standalone)
        self.assertIn("funds still supplied", alert.body)
        self.assertIn("1.50M USDC (15.00% of vault)", alert.body)
        self.assertIn("revokePendingMarketRemoval", alert.body)
        self.assertIn("(3 days)", alert.body)

    def test_any_non_zero_supply_is_high_and_dust_shows_raw_units(self) -> None:
        state = _state("0x" + "ab" * 32, **REMOVAL_STATE, removable_at=2_000_000_000, supply_shares=1, supply_assets=1)
        diff = self._run(state, _Cache(), now=1_000_000_000)

        self.assertEqual(diff.alerts[0].severity, AlertSeverity.HIGH)
        self.assertIn("1 raw units USDC", diff.alerts[0].body)

    def test_removal_of_empty_market_stays_medium(self) -> None:
        state = _state("0x" + "ab" * 32, **REMOVAL_STATE, removable_at=2_000_000_000)
        diff = self._run(state, _Cache(), now=1_000_000_000)

        self.assertEqual(len(diff.alerts), 1)
        self.assertEqual(diff.alerts[0].severity, AlertSeverity.MEDIUM)
        self.assertFalse(diff.alerts[0].standalone)
        self.assertIn("no vault supply", diff.alerts[0].body)

    def test_still_supplied_removal_is_reminded_once_inside_the_window(self) -> None:
        removable_at = 2_000_000_000
        state = _state(
            "0x" + "ab" * 32, **REMOVAL_STATE, removable_at=removable_at, supply_shares=5, supply_assets=5 * 10**6
        )
        cache = _Cache()

        first = self._run(state, cache, now=removable_at - 3 * 86400)
        quiet = self._run(state, cache, now=removable_at - 2 * 86400)
        reminder = self._run(state, cache, now=removable_at - 3600)
        after = self._run(state, cache, now=removable_at - 60)

        self.assertEqual(len(first.alerts), 1)
        self.assertEqual(quiet.alerts, [])
        self.assertEqual(len(reminder.alerts), 1)
        self.assertEqual(reminder.alerts[0].severity, AlertSeverity.HIGH)
        self.assertIn("Reminder", reminder.alerts[0].body)
        self.assertEqual(after.alerts, [])

    def test_first_alert_inside_the_window_suppresses_the_reminder(self) -> None:
        removable_at = 2_000_000_000
        state = _state(
            "0x" + "ab" * 32, **REMOVAL_STATE, removable_at=removable_at, supply_shares=5, supply_assets=5 * 10**6
        )
        cache = _Cache()

        first = self._run(state, cache, now=removable_at - 3600)
        second = self._run(state, cache, now=removable_at - 60)

        self.assertEqual(len(first.alerts), 1)
        self.assertEqual(second.alerts, [])

    def test_removal_first_seen_empty_escalates_once_the_vault_supplies_it(self) -> None:
        removable_at = 2_000_000_000
        empty = _state("0x" + "ab" * 32, **REMOVAL_STATE, removable_at=removable_at)
        supplied = _state(
            "0x" + "ab" * 32, **REMOVAL_STATE, removable_at=removable_at, supply_shares=5, supply_assets=5 * 10**6
        )
        cache = _Cache()

        first = self._run(empty, cache, now=removable_at - 3 * 86400)
        escalated = self._run(supplied, cache, now=removable_at - 2 * 86400)
        repeat = self._run(supplied, cache, now=removable_at - 2 * 86400 + 3600)
        emptied = self._run(empty, cache, now=removable_at - 2 * 86400 + 7200)

        self.assertEqual([a.severity for a in first.alerts], [AlertSeverity.MEDIUM])
        self.assertEqual(len(escalated.alerts), 1)
        self.assertEqual(escalated.alerts[0].severity, AlertSeverity.HIGH)
        self.assertTrue(escalated.alerts[0].standalone)
        self.assertNotIn("Reminder", escalated.alerts[0].body)
        self.assertEqual(repeat.alerts, [])
        self.assertEqual(emptied.alerts, [])


class _V1Batch:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def add(self, call: Any) -> None:
        self.calls.append(call)


class TestMorphoV1VaultReads(unittest.TestCase):
    def test_reverting_total_assets_only_drops_the_vault_share(self) -> None:
        """totalAssets() values every withdraw-queue market and can revert on a broken IRM;
        the removal check must still see supply and stay a loss alert."""
        from contextlib import contextmanager

        market_id = bytes.fromhex("ab" * 32)
        vault = MagicMock(address=VAULT)
        vault.functions.MORPHO.return_value.call.return_value = "0x" + "33" * 20
        vault.functions.totalAssets.return_value.call.side_effect = ValueError("execution reverted")
        vault.functions.pendingCap.return_value.call.return_value = (0, 0)
        vault.functions.config.return_value.call.return_value = (0, True, 2_000_000_000)
        blue = MagicMock()
        blue.functions.position.return_value.call.return_value = (10**12, 0, 0)
        blue.functions.market.return_value.call.return_value = (2 * 10**6, 2 * 10**12, 0, 0, 0, 0)
        client = MagicMock()
        client.eth.contract.return_value = blue

        @contextmanager
        def batch_requests() -> Any:
            yield _V1Batch()

        client.batch_requests.side_effect = batch_requests
        client.execute_batch.side_effect = lambda batch: batch.calls

        [state] = governance.load_market_governance_states(vault, [market_id], client, block_number=1)

        self.assertIsNone(state.vault_total_assets)
        self.assertEqual(
            (state.supply_shares, state.supply_assets, state.removable_at), (10**12, 1_000_000, 2_000_000_000)
        )
        self.assertIn("1.00 USDC", governance.format_amount_at_risk(state.supply_assets, 6, "USDC", None))
        self.assertNotIn("of vault", governance.format_amount_at_risk(state.supply_assets, 6, "USDC", None))


class TestMorphoV1Timelock(unittest.TestCase):
    def test_timelock_decrease_message_shows_old_and_new_values(self) -> None:
        message = governance.format_timelock_decrease(3 * 86400, 0, 2_000_000_000)

        self.assertIn("Timelock decrease: 3d → 0s", message)
        self.assertIn("Executable at:", message)

    def test_to_assets_down_matches_shares_math(self) -> None:
        # Virtual offsets: 1e6 shares for 1 asset on an empty market.
        self.assertEqual(governance.to_assets_down(10**6, 0, 0), 1)
        self.assertEqual(governance.to_assets_down(2 * 10**12, 2 * 10**6, 2 * 10**12), 2_000_000)
        # Rounds down: 3 shares of a 10-asset / 1e7-share market are worth less than one unit.
        self.assertEqual(governance.to_assets_down(3, 10, 10**7), 0)


class TestMorphoV1GovernanceGrouping(unittest.TestCase):
    def test_findings_for_one_vault_collapse_into_a_single_message(self) -> None:
        """Two new markets on one vault produce one message, not two.

        The vault name and chain move to the header, so the sections carry only
        what differs between them.
        """
        states = [
            _state("0x" + "ab" * 32, current_cap=0),
            _state("0x" + "cd" * 32, current_cap=0),
        ]
        diff = VaultDiff()
        market_names = iter([("cbETH/USDC (86.00%)", 6), ("cbETH/USDC (77.00%)", 6)])

        sent: list[Any] = []
        with (
            patch("protocols.morpho.governance.get_last_executed_morpho_from_file", return_value=0),
            patch("protocols.morpho.governance.fetch_market_info", side_effect=lambda *_: next(market_names)),
            patch("protocols.morpho.governance.write_last_executed_morpho_to_file"),
            patch("protocols.morpho._alerts.send_alert", side_effect=sent.append),
        ):
            for state in states:
                governance.check_market_governance_state("Yearn OG USDC", state, Chain.BASE, diff)
            governance.send_vault_alerts(
                governance.vault_header("Yearn OG USDC", VAULT, Chain.BASE),
                diff.alerts,
                governance.PROTOCOL,
            )

        self.assertEqual(len(sent), 1, f"expected 1 grouped alert, got {len(sent)}")
        message = sent[0].message
        # Header names the vault and chain exactly once.
        self.assertEqual(message.count("Yearn OG USDC"), 1)
        self.assertEqual(message.count("on BASE"), 1)
        # Both markets are present as separate sections.
        self.assertIn("cbETH/USDC (86.00%)", message)
        self.assertIn("cbETH/USDC (77.00%)", message)
        self.assertEqual(message.count("Adding new market"), 2)


class TestMorphoV1GovernanceMain(unittest.TestCase):
    def test_main_checks_every_configured_chain_and_continues_after_failure(self) -> None:
        checked: list[Chain] = []

        def fake_get_data_for_chain(chain: Chain) -> None:
            checked.append(chain)
            if chain == Chain.MAINNET:
                raise RuntimeError("rpc down")

        with (
            patch("protocols.morpho.governance.get_data_for_chain", side_effect=fake_get_data_for_chain),
            self.assertRaisesRegex(governance.MorphoMonitoringError, "MAINNET: RuntimeError: rpc down"),
        ):
            governance.main()

        self.assertEqual(checked, list(governance.VAULTS_V1_BY_CHAIN))


if __name__ == "__main__":
    unittest.main()
