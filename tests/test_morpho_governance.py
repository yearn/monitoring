"""Behavior tests for Morpho Vault V1 governance monitoring."""

import unittest
from typing import Any
from unittest.mock import patch

from protocols.morpho import governance
from protocols.morpho._alerts import VaultDiff
from protocols.morpho.governance import MarketGovernanceState
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
                governance._vault_header("Yearn OG USDC", VAULT, Chain.BASE),
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


if __name__ == "__main__":
    unittest.main()
