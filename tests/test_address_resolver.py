"""Tests for utils/address_resolver.py."""

import unittest
from unittest.mock import patch

from utils.address_resolver import register_backend, resolve_address_label


class TestResolveAddressLabel(unittest.TestCase):
    """Backend chain: tries each in order, returns first non-empty result."""

    def test_first_backend_wins(self) -> None:
        # Safe utility shortcut hits before anything else.
        label = resolve_address_label(1, "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D")
        self.assertEqual(label, "Safe MultiSendCallOnly")

    @patch("utils.address_resolver._etherscan_backend", return_value="EtherscanName")
    @patch("utils.address_resolver._swiss_knife_backend", return_value="SwissName")
    def test_swiss_knife_wins_over_etherscan(self, mock_sk: object, mock_es: object) -> None:
        # Random address that doesn't hit the Safe registry.
        label = resolve_address_label(1, "0x" + "ab" * 20)
        self.assertEqual(label, "SwissName")
        mock_es.assert_not_called()  # type: ignore[attr-defined]

    @patch("utils.address_resolver._etherscan_backend", return_value="EtherscanName")
    @patch("utils.address_resolver._swiss_knife_backend", return_value="")
    def test_falls_through_to_etherscan(self, mock_sk: object, mock_es: object) -> None:
        label = resolve_address_label(1, "0x" + "ab" * 20)
        self.assertEqual(label, "EtherscanName")

    @patch("utils.address_resolver._etherscan_backend", return_value="")
    @patch("utils.address_resolver._swiss_knife_backend", return_value="")
    def test_all_miss_returns_empty(self, mock_sk: object, mock_es: object) -> None:
        self.assertEqual(resolve_address_label(1, "0x" + "ab" * 20), "")

    def test_empty_address(self) -> None:
        self.assertEqual(resolve_address_label(1, ""), "")

    @patch("utils.address_resolver._etherscan_backend", return_value="EtherscanName")
    @patch("utils.address_resolver._swiss_knife_backend", side_effect=RuntimeError("API down"))
    def test_failed_backend_skipped(self, mock_sk: object, mock_es: object) -> None:
        # Swiss Knife raising shouldn't kill the chain — Etherscan still tried.
        label = resolve_address_label(1, "0x" + "ab" * 20)
        self.assertEqual(label, "EtherscanName")


class TestRegisterBackend(unittest.TestCase):
    def test_appends_to_end(self) -> None:
        import utils.address_resolver as resolver

        def my_appended_backend(chain_id: int, address: str) -> str:
            return ""

        original_names = list(resolver._BACKEND_NAMES)
        try:
            register_backend(my_appended_backend)
            self.assertEqual(resolver._BACKEND_NAMES[-1], "my_appended_backend")
        finally:
            resolver._BACKEND_NAMES[:] = original_names
            resolver.__dict__.pop("my_appended_backend", None)

    def test_inserts_at_position(self) -> None:
        import utils.address_resolver as resolver

        def my_priority_backend(chain_id: int, address: str) -> str:
            return "WINS"

        original_names = list(resolver._BACKEND_NAMES)
        try:
            register_backend(my_priority_backend, position=0)
            self.assertEqual(resolver._BACKEND_NAMES[0], "my_priority_backend")
            # Now it wins even for known Safe utility addresses.
            self.assertEqual(resolve_address_label(1, "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D"), "WINS")
        finally:
            resolver._BACKEND_NAMES[:] = original_names
            resolver.__dict__.pop("my_priority_backend", None)


if __name__ == "__main__":
    unittest.main()
