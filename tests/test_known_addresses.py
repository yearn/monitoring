"""Tests for utils/known_addresses and its address-resolver integration."""

import unittest
from unittest.mock import patch

from utils import known_addresses
from utils.address_resolver import resolve_address_label


class TestKnownAddressesLookup(unittest.TestCase):
    def test_chain_agnostic_burn_address(self) -> None:
        label = known_addresses.lookup(1, "0x000000000000000000000000000000000000dEaD")
        self.assertIn("Burn", label)

    def test_case_insensitive(self) -> None:
        self.assertEqual(
            known_addresses.lookup(1, "0x000000000000000000000000000000000000DEAD"),
            known_addresses.lookup(8453, "0x000000000000000000000000000000000000dead"),
        )

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(known_addresses.lookup(1, "0x" + "ab" * 20), "")

    def test_empty_address(self) -> None:
        self.assertEqual(known_addresses.lookup(1, ""), "")

    def test_chain_specific_takes_precedence(self) -> None:
        addr = "0x" + "11" * 20
        with patch.dict(known_addresses._BY_CHAIN, {(1, addr): "Yearn yChad"}, clear=False):
            self.assertEqual(known_addresses.lookup(1, addr), "Yearn yChad")
            # Different chain → no chain-specific entry, falls through to "".
            self.assertEqual(known_addresses.lookup(8453, addr), "")

    def test_watched_yearn_safe_label_from_safe_config(self) -> None:
        self.assertEqual(
            known_addresses.lookup(1, "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"),
            "yChad (Yearn multisig/daddy)",
        )

    def test_yearn_proposer_bot_label_is_chain_agnostic(self) -> None:
        self.assertEqual(
            known_addresses.lookup(8453, "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5"),
            "Yearn yChad proposer bot",
        )

    def test_safe_utility_label_is_chain_agnostic(self) -> None:
        self.assertEqual(
            known_addresses.lookup(42161, "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D"),
            "Safe MultiSendCallOnly",
        )

    def test_operator_supplied_timelock_label(self) -> None:
        self.assertEqual(
            known_addresses.lookup(1, "0x88Ba032be87d5EF1fbE87336B7090767F367BF73"),
            "Yearn TimelockController",
        )


class TestResolverIntegration(unittest.TestCase):
    def test_known_address_backend_wins(self) -> None:
        addr = "0x" + "22" * 20
        # Registry hit should short-circuit before any network backend runs.
        with patch.dict(known_addresses._BY_CHAIN, {(1, addr): "Yearn dev multisig"}, clear=False):
            with patch("utils.address_resolver._etherscan_backend", return_value="ShouldNotWin") as etherscan:
                self.assertEqual(resolve_address_label(1, addr), "Yearn dev multisig")
                etherscan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
