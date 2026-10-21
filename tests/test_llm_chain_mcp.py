"""Security boundaries for the agent's narrow chain lookup tools."""

import os
import unittest
from unittest.mock import patch

import requests

from utils.llm import chain_mcp

ADDRESS = "0x" + "1" * 40
TX_HASH = "0x" + "2" * 64


class TestChainMCP(unittest.TestCase):
    def setUp(self) -> None:
        chain_mcp._calls = 0
        self.addCleanup(setattr, chain_mcp, "_calls", 0)

    @patch("utils.llm.chain_mcp._get_json")
    def test_rpc_read_uses_configured_endpoint_and_restricts_method(self, get_json) -> None:
        get_json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x2a"}
        with patch.dict(os.environ, {"PROVIDER_URL_MAINNET": "https://rpc.example.test/key"}, clear=True):
            result = chain_mcp.read_rpc(1, "eth_getBalance", [ADDRESS, "latest"])
            self.assertEqual(result, {"chain_id": 1, "method": "eth_getBalance", "result": "0x2a"})
            self.assertEqual(get_json.call_args.args[0], "https://rpc.example.test/key")
            with self.assertRaisesRegex(ValueError, "not allowed"):
                chain_mcp.read_rpc(1, "eth_sendRawTransaction", [TX_HASH])
        self.assertEqual(get_json.call_count, 1)

    @patch("utils.llm.chain_mcp._get_json")
    def test_eth_call_rejects_value_sender_overrides_and_bad_calldata(self, get_json) -> None:
        for call in (
            {"to": ADDRESS, "data": "0x1234", "value": "0x1"},
            {"to": ADDRESS, "data": "0x1234", "from": ADDRESS},
            {"to": ADDRESS, "data": "not hex"},
        ):
            with self.subTest(call=call), self.assertRaises(ValueError):
                chain_mcp.read_rpc(1, "eth_call", [call, "latest"])
        get_json.assert_not_called()

    @patch("utils.llm.chain_mcp._get_json")
    def test_chain_and_request_budget_are_enforced(self, get_json) -> None:
        get_json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x2a"}
        with patch.dict(os.environ, {"PROVIDER_URL_MAINNET": "https://rpc.example.test"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Unsupported chain"):
                chain_mcp.read_rpc(31337, "eth_getBalance", [ADDRESS, "latest"])
            for _ in range(chain_mcp.MAX_CALLS):
                chain_mcp.read_rpc(1, "eth_getBalance", [ADDRESS, "latest"])
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                chain_mcp.read_rpc(1, "eth_getBalance", [ADDRESS, "latest"])
        self.assertEqual(get_json.call_count, chain_mcp.MAX_CALLS)

    @patch("utils.llm.chain_mcp._get_json")
    def test_etherscan_has_fixed_action_and_keeps_key_out_of_result(self, get_json) -> None:
        get_json.return_value = {
            "status": "1",
            "result": [{"ContractName": "Vault", "SourceCode": "contract Vault {}"}],
        }
        with patch.dict(os.environ, {"ETHERSCAN_TOKEN": "secret-key"}, clear=True):
            result = chain_mcp.read_etherscan(1, ADDRESS, "source")
            self.assertEqual(result["contract_name"], "Vault")
            self.assertNotIn("secret-key", str(result))
            self.assertEqual(get_json.call_args.args[0], chain_mcp.ETHERSCAN_URL)
            self.assertEqual(get_json.call_args.kwargs["params"]["action"], "getsourcecode")
            with self.assertRaisesRegex(ValueError, "abi or source"):
                chain_mcp.read_etherscan(1, ADDRESS, "sendtx")
        self.assertEqual(get_json.call_count, 1)

    @patch("utils.llm.chain_mcp._get_json")
    def test_etherscan_source_is_truncated(self, get_json) -> None:
        get_json.return_value = {"status": "1", "result": [{"SourceCode": "x" * (chain_mcp.MAX_SOURCE_CHARS + 1)}]}
        with patch.dict(os.environ, {"ETHERSCAN_TOKEN": "secret-key"}, clear=True):
            result = chain_mcp.read_etherscan(1, ADDRESS, "source")
        self.assertEqual(len(result["source"]), chain_mcp.MAX_SOURCE_CHARS)
        self.assertTrue(result["truncated"])

    @patch("utils.llm.chain_mcp.requests.Session")
    def test_http_transport_blocks_redirects_and_large_responses(self, session_cls) -> None:
        session = session_cls.return_value.__enter__.return_value
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [b"x" * 11]
        with self.assertRaisesRegex(ValueError, "too large"):
            chain_mcp._get_json("https://api.etherscan.io/v2/api", limit=10)
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    @patch("utils.llm.chain_mcp.requests.Session")
    def test_http_error_does_not_echo_credential_url(self, session_cls) -> None:
        session = session_cls.return_value.__enter__.return_value
        session.get.side_effect = requests.RequestException("https://api.etherscan.io/?apikey=secret-key")
        with self.assertRaises(ValueError) as ctx:
            chain_mcp._get_json("https://api.etherscan.io/v2/api", limit=100)
        self.assertNotIn("secret-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
