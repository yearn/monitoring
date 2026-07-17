"""Behavior tests for Morpho Vault V1 governance monitoring."""

import unittest
from unittest.mock import patch

from protocols.morpho import governance
from protocols.morpho.governance import MarketGovernanceState
from utils.chains import Chain


class TestMorphoV1GovernanceAlerts(unittest.TestCase):
    def test_new_pending_cap_alert_uses_shared_market_metadata(self) -> None:
        state = MarketGovernanceState(
            vault_address="0x" + "11" * 20,
            market_id="0x" + "ab" * 32,
            pending_cap=2_000_000,
            pending_cap_timestamp=2_000_000_000,
            current_cap=1_000_000,
            removable_at=0,
        )

        with (
            patch("protocols.morpho.governance.get_last_executed_morpho_from_file", return_value=0),
            patch("protocols.morpho.governance.fetch_market_info", return_value=("WETH/USDC (86.00%)", 6)),
            patch("protocols.morpho.governance.write_last_executed_morpho_to_file") as write,
            patch("protocols.morpho.governance.send_alert") as send,
        ):
            governance.check_market_governance_state("Example", state, Chain.MAINNET)

        alert = send.call_args.args[0]
        self.assertIn("WETH/USDC (86.00%)", alert.message)
        self.assertIn("difference: 100.00%", alert.message)
        write.assert_called_once_with(
            state.vault_address,
            state.market_id,
            governance.PENDING_CAP_TYPE,
            state.pending_cap_timestamp,
        )

    def test_previously_alerted_market_removal_is_not_repeated(self) -> None:
        state = MarketGovernanceState(
            vault_address="0x" + "11" * 20,
            market_id="0x" + "ab" * 32,
            pending_cap=0,
            pending_cap_timestamp=0,
            current_cap=0,
            removable_at=2_000_000_000,
        )

        with (
            patch(
                "protocols.morpho.governance.get_last_executed_morpho_from_file",
                return_value=state.removable_at,
            ),
            patch("protocols.morpho.governance.write_last_executed_morpho_to_file") as write,
            patch("protocols.morpho.governance.send_alert") as send,
        ):
            governance.check_market_governance_state("Example", state, Chain.MAINNET)

        send.assert_not_called()
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
