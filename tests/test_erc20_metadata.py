"""Tests for utils/erc20_metadata.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.erc20_metadata import ERC20Metadata, fetch_erc20_metadata, reset_cache


class TestFetchErc20Metadata(unittest.TestCase):
    def setUp(self) -> None:
        reset_cache()

    @patch("utils.erc20_metadata.ChainManager")
    def test_returns_symbol_and_decimals(self, mock_cm: MagicMock) -> None:
        client = MagicMock()
        client.execute_batch.return_value = ("USDC", 6)
        client.batch_requests.return_value.__enter__.return_value = MagicMock()
        client.batch_requests.return_value.__exit__.return_value = False
        mock_cm.get_client.return_value = client

        meta = fetch_erc20_metadata(1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.assertEqual(meta, ERC20Metadata(symbol="USDC", decimals=6))

    @patch("utils.erc20_metadata.ChainManager")
    def test_returns_none_on_eth_call_failure(self, mock_cm: MagicMock) -> None:
        client = MagicMock()
        client.batch_requests.side_effect = RuntimeError("execution reverted")
        mock_cm.get_client.return_value = client

        meta = fetch_erc20_metadata(1, "0x" + "ab" * 20)
        self.assertIsNone(meta)

    def test_invalid_address_skips_network(self) -> None:
        with patch("utils.erc20_metadata.ChainManager") as mock_cm:
            self.assertIsNone(fetch_erc20_metadata(1, ""))
            self.assertIsNone(fetch_erc20_metadata(1, "0xshort"))
            self.assertIsNone(fetch_erc20_metadata(1, "not-hex"))
            mock_cm.get_client.assert_not_called()

    @patch("utils.erc20_metadata.ChainManager")
    def test_caches_repeat_lookups(self, mock_cm: MagicMock) -> None:
        client = MagicMock()
        client.execute_batch.return_value = ("USDC", 6)
        client.batch_requests.return_value.__enter__.return_value = MagicMock()
        client.batch_requests.return_value.__exit__.return_value = False
        mock_cm.get_client.return_value = client

        addr = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        fetch_erc20_metadata(1, addr)
        fetch_erc20_metadata(1, addr)
        self.assertEqual(client.batch_requests.call_count, 1)

    @patch("utils.erc20_metadata.ChainManager")
    def test_caches_misses(self, mock_cm: MagicMock) -> None:
        client = MagicMock()
        client.batch_requests.side_effect = RuntimeError("not a token")
        mock_cm.get_client.return_value = client

        addr = "0x" + "cd" * 20
        self.assertIsNone(fetch_erc20_metadata(1, addr))
        self.assertIsNone(fetch_erc20_metadata(1, addr))
        # Cached miss → only one attempt.
        self.assertEqual(client.batch_requests.call_count, 1)


if __name__ == "__main__":
    unittest.main()
