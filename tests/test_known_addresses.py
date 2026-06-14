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
