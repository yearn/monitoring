"""Tests for utils/erc20_metadata.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.erc20_metadata import ERC20Metadata, fetch_erc20_metadata, reset_cache

# Bytecode fragments used by the gate. A "token" must contain both the symbol()
# (95d89b41) and decimals() (313ce567) selectors; anything else is non-token.
_TOKEN_CODE = bytes.fromhex("608060405295d89b41313ce567")
_NON_TOKEN_CODE = bytes.fromhex("6080604052")

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _client_with_code(code: bytes) -> MagicMock:
    """Build a mock Web3 client whose get_code returns ``code`` and whose batch
    yields ("USDC", 6)."""
    client = MagicMock()
    client.eth.get_code.return_value = code
    client.execute_batch.return_value = ("USDC", 6)
    client.batch_requests.return_value.__enter__.return_value = MagicMock()
    client.batch_requests.return_value.__exit__.return_value = False
    return client


class TestFetchErc20Metadata(unittest.TestCase):
    def setUp(self) -> None:
        reset_cache()

    @patch("utils.erc20_metadata.ChainManager")
    def test_returns_symbol_and_decimals(self, mock_cm: MagicMock) -> None:
        mock_cm.get_client.return_value = _client_with_code(_TOKEN_CODE)
        meta = fetch_erc20_metadata(1, USDC)
        self.assertEqual(meta, ERC20Metadata(symbol="USDC", decimals=6))

    @patch("utils.erc20_metadata.ChainManager")
    def test_skips_eoa_without_calling(self, mock_cm: MagicMock) -> None:
        """No deployed code => EOA => return None without a symbol() call."""
        client = _client_with_code(b"")
        mock_cm.get_client.return_value = client
        self.assertIsNone(fetch_erc20_metadata(1, "0x" + "ab" * 20))
        client.batch_requests.assert_not_called()

    @patch("utils.erc20_metadata.get_current_implementation", return_value=None)
    @patch("utils.erc20_metadata.ChainManager")
    def test_skips_non_token_contract_without_calling(self, mock_cm: MagicMock, _impl: MagicMock) -> None:
        """Contract without the selectors (and not a proxy) => skip the call."""
        client = _client_with_code(_NON_TOKEN_CODE)
        mock_cm.get_client.return_value = client
        self.assertIsNone(fetch_erc20_metadata(1, "0x" + "cd" * 20))
        client.batch_requests.assert_not_called()

    @patch("utils.erc20_metadata.ChainManager")
    def test_resolves_proxy_implementation(self, mock_cm: MagicMock) -> None:
        """A proxy stub carries no selectors; we resolve the impl and scan it."""
        proxy = "0x" + "11" * 20
        impl = "0x" + "22" * 20

        def code_for(addr: str) -> bytes:
            return _TOKEN_CODE if addr.lower() == impl.lower() else _NON_TOKEN_CODE

        client = _client_with_code(_NON_TOKEN_CODE)
        client.eth.get_code.side_effect = lambda addr: code_for(addr)
        mock_cm.get_client.return_value = client

        with patch("utils.erc20_metadata.get_current_implementation", return_value=impl):
            meta = fetch_erc20_metadata(1, proxy)
        self.assertEqual(meta, ERC20Metadata(symbol="USDC", decimals=6))

    def test_invalid_address_skips_network(self) -> None:
        with patch("utils.erc20_metadata.ChainManager") as mock_cm:
            self.assertIsNone(fetch_erc20_metadata(1, ""))
            self.assertIsNone(fetch_erc20_metadata(1, "0xshort"))
            self.assertIsNone(fetch_erc20_metadata(1, "not-hex"))
            mock_cm.get_client.assert_not_called()

    @patch("utils.erc20_metadata.ChainManager")
    def test_returns_none_on_eth_call_failure(self, mock_cm: MagicMock) -> None:
        """Gate passes but the metadata call reverts => None (backstop catch)."""
        client = _client_with_code(_TOKEN_CODE)
        client.batch_requests.side_effect = RuntimeError("execution reverted")
        mock_cm.get_client.return_value = client
        self.assertIsNone(fetch_erc20_metadata(1, USDC))

    @patch("utils.erc20_metadata.ChainManager")
    def test_caches_repeat_lookups(self, mock_cm: MagicMock) -> None:
        client = _client_with_code(_TOKEN_CODE)
        mock_cm.get_client.return_value = client
        fetch_erc20_metadata(1, USDC)
        fetch_erc20_metadata(1, USDC)
        self.assertEqual(client.batch_requests.call_count, 1)

    @patch("utils.erc20_metadata.ChainManager")
    def test_caches_misses(self, mock_cm: MagicMock) -> None:
        """A cached miss must not re-hit the network (get_code called once)."""
        client = _client_with_code(b"")
        mock_cm.get_client.return_value = client
        addr = "0x" + "cd" * 20
        self.assertIsNone(fetch_erc20_metadata(1, addr))
        self.assertIsNone(fetch_erc20_metadata(1, addr))
        self.assertEqual(client.eth.get_code.call_count, 1)


# Runtime bytecode of a real Yearn V3 vault clone (yETH Recovery Vault).
_VAULT_IMPL = "0xd8063123BBA3B480569244AE66BFE72B6c84b00d"
_CLONE_CODE = bytes.fromhex("363d3d373d3d3d363d73" + _VAULT_IMPL[2:].lower() + "5af43d82803e903d91602b57fd5bf3")
# Token bytecode that also dispatches name() (06fdde03).
_NAMED_TOKEN_CODE = bytes.fromhex("608060405295d89b41313ce56706fdde03")


class TestTokenName(unittest.TestCase):
    """name() is read alongside symbol/decimals, and its failure is not fatal."""

    def setUp(self) -> None:
        reset_cache()

    @patch("utils.erc20_metadata.ChainManager")
    def test_reads_name_when_dispatched(self, mock_cm: MagicMock) -> None:
        client = _client_with_code(_NAMED_TOKEN_CODE)
        client.execute_batch.return_value = ("yvWETH-2", 18, "WETH-2 yVault")
        mock_cm.get_client.return_value = client
        meta = fetch_erc20_metadata(1, USDC)
        self.assertEqual(meta, ERC20Metadata(symbol="yvWETH-2", decimals=18, name="WETH-2 yVault"))

    @patch("utils.erc20_metadata.ChainManager")
    def test_name_failure_falls_back_to_symbol_and_decimals(self, mock_cm: MagicMock) -> None:
        """A bytes32 name (MKR) fails to decode; the token must keep its symbol/decimals."""
        client = _client_with_code(_NAMED_TOKEN_CODE)
        client.execute_batch.side_effect = [ValueError("could not decode name"), ("MKR", 18)]
        mock_cm.get_client.return_value = client
        self.assertEqual(fetch_erc20_metadata(1, USDC), ERC20Metadata(symbol="MKR", decimals=18))

    @patch("utils.erc20_metadata.ChainManager")
    def test_skips_name_when_not_dispatched(self, mock_cm: MagicMock) -> None:
        client = _client_with_code(_TOKEN_CODE)
        mock_cm.get_client.return_value = client
        self.assertEqual(fetch_erc20_metadata(1, USDC), ERC20Metadata(symbol="USDC", decimals=6))
        self.assertEqual(client.execute_batch.call_count, 1)


class TestMinimalProxyClone(unittest.TestCase):
    """EIP-1167 clones (every Yearn V3 vault) resolve through their embedded implementation."""

    def setUp(self) -> None:
        reset_cache()

    @patch("utils.erc20_metadata.get_current_implementation")
    @patch("utils.erc20_metadata.ChainManager")
    def test_clone_resolves_to_embedded_implementation(self, mock_cm: MagicMock, mock_impl: MagicMock) -> None:
        clone = "0xd7a540ba3626c0aa66e7DB4088971d0CD64695B6"

        def code_for(addr: str) -> bytes:
            return _TOKEN_CODE if addr.lower() == _VAULT_IMPL.lower() else _CLONE_CODE

        client = _client_with_code(_CLONE_CODE)
        client.eth.get_code.side_effect = code_for
        mock_cm.get_client.return_value = client

        self.assertEqual(fetch_erc20_metadata(1, clone), ERC20Metadata(symbol="USDC", decimals=6))
        # The clone's bytecode names the implementation; no slot or getter probing.
        mock_impl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
