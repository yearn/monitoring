"""Tests for utils/on_chain_state.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.on_chain_state import (
    StateRead,
    _is_simple_type,
    _match_key_value_from_params,
    _parse_var_declaration,
    format_state_reads,
    read_before_state,
)


class TestIsSimpleType(unittest.TestCase):
    def test_uint(self) -> None:
        self.assertTrue(_is_simple_type("uint256"))
        self.assertTrue(_is_simple_type("uint128"))
        self.assertTrue(_is_simple_type("uint"))

    def test_int(self) -> None:
        self.assertTrue(_is_simple_type("int256"))

    def test_address_bool_bytes(self) -> None:
        self.assertTrue(_is_simple_type("address"))
        self.assertTrue(_is_simple_type("bool"))
        self.assertTrue(_is_simple_type("bytes32"))
        self.assertTrue(_is_simple_type("bytes16"))

    def test_compound_types_are_not_simple(self) -> None:
        self.assertFalse(_is_simple_type("uint256[]"))
        self.assertFalse(_is_simple_type("CreditLineData"))
        self.assertFalse(_is_simple_type("mapping"))


class TestParseVarDeclaration(unittest.TestCase):
    def test_simple_uint(self) -> None:
        snippet = "/// @notice slip\nuint256 public maxSlippage;"
        result = _parse_var_declaration(snippet, "maxSlippage")
        self.assertEqual(result, ("uint256", []))

    def test_simple_address(self) -> None:
        snippet = "address public owner;"
        result = _parse_var_declaration(snippet, "owner")
        self.assertEqual(result, ("address", []))

    def test_single_key_mapping(self) -> None:
        snippet = "mapping(address => uint256) public coverageCap;"
        result = _parse_var_declaration(snippet, "coverageCap")
        self.assertEqual(result, ("uint256", ["address"]))

    def test_mapping_with_bytes32_key(self) -> None:
        snippet = "mapping(bytes32 => uint256) public values;"
        result = _parse_var_declaration(snippet, "values")
        self.assertEqual(result, ("uint256", ["bytes32"]))

    def test_nested_mapping_skipped(self) -> None:
        snippet = "mapping(bytes32 => mapping(address => uint256)) public nested;"
        result = _parse_var_declaration(snippet, "nested")
        self.assertIsNone(result)

    def test_struct_value_mapping_skipped(self) -> None:
        snippet = "mapping(address => CreditLineData) public creditLines;"
        result = _parse_var_declaration(snippet, "creditLines")
        self.assertIsNone(result)

    def test_array_value_mapping_skipped(self) -> None:
        snippet = "mapping(address => uint256[]) public history;"
        result = _parse_var_declaration(snippet, "history")
        self.assertIsNone(result)

    def test_strips_natspec_lines(self) -> None:
        snippet = """/// @notice Max slip
        /// @dev some stuff
        uint256 public maxSlippage;"""
        result = _parse_var_declaration(snippet, "maxSlippage")
        self.assertEqual(result, ("uint256", []))


class TestMatchKeyValueFromParams(unittest.TestCase):
    def test_matches_address_key(self) -> None:
        call = DecodedCall(
            function_name="setCoverageCap",
            signature="setCoverageCap(address,uint256)",
            params=[("address", "0xAgent"), ("uint256", 1000)],
        )
        self.assertEqual(_match_key_value_from_params(call, "address"), "0xAgent")

    def test_matches_bytes32_key(self) -> None:
        call = DecodedCall(
            function_name="setConfig",
            signature="setConfig(bytes32,uint256)",
            params=[("bytes32", b"\x01" * 32), ("uint256", 42)],
        )
        self.assertEqual(_match_key_value_from_params(call, "bytes32"), b"\x01" * 32)

    def test_uint256_key_matches_any_uint_size(self) -> None:
        call = DecodedCall(
            function_name="byId",
            signature="byId(uint64,address)",
            params=[("uint64", 5), ("address", "0xA")],
        )
        self.assertEqual(_match_key_value_from_params(call, "uint256"), 5)

    def test_no_match_returns_none(self) -> None:
        call = DecodedCall(
            function_name="pause",
            signature="pause(bool)",
            params=[("bool", True)],
        )
        self.assertIsNone(_match_key_value_from_params(call, "address"))

    def test_skips_array_params(self) -> None:
        call = DecodedCall(
            function_name="setMany",
            signature="setMany(address[],uint256[])",
            params=[("address[]", ["0xA", "0xB"]), ("uint256[]", [1, 2])],
        )
        self.assertIsNone(_match_key_value_from_params(call, "address"))


class TestReadBeforeState(unittest.TestCase):
    @patch("utils.on_chain_state.ChainManager")
    @patch("utils.on_chain_state.fetch_source")
    def test_reads_simple_uint(self, mock_fetch: MagicMock, mock_chain: MagicMock) -> None:
        source = """
        uint256 public maxSlippage;
        function setMaxSlippage(uint256 _x) external { maxSlippage = _x; }
        """
        mock_fetch.return_value = ("Farm", source)

        # Mock eth_call to return abi-encoded uint256(999999000000000000)
        from eth_abi import encode as abi_encode

        mock_client = MagicMock()
        mock_client.eth.call.return_value = abi_encode(["uint256"], [999999000000000000])
        mock_chain.get_client.return_value = mock_client

        call = DecodedCall(
            function_name="setMaxSlippage",
            signature="setMaxSlippage(uint256)",
            params=[("uint256", 990000000000000000)],
        )

        reads = read_before_state(1, "0x35f9ebdc02f936e199826778bc06a13272a06b87", call)

        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].var_name, "maxSlippage")
        self.assertEqual(reads[0].value, 999999000000000000)
        self.assertEqual(reads[0].key_args, ())

    @patch("utils.on_chain_state.ChainManager")
    @patch("utils.on_chain_state.fetch_source")
    def test_reads_address_keyed_mapping(self, mock_fetch: MagicMock, mock_chain: MagicMock) -> None:
        source = """
        mapping(address => uint256) public coverageCap;
        function setCoverageCap(address _a, uint256 _c) external { coverageCap[_a] = _c; }
        """
        mock_fetch.return_value = ("Delegation", source)

        from eth_abi import encode as abi_encode

        mock_client = MagicMock()
        mock_client.eth.call.return_value = abi_encode(["uint256"], [5000000000000000])
        mock_chain.get_client.return_value = mock_client

        agent = "0xbAfa91d22C093E42E28D7Be417e38244E4153f78"
        call = DecodedCall(
            function_name="setCoverageCap",
            signature="setCoverageCap(address,uint256)",
            params=[("address", agent), ("uint256", 8000000000000000)],
        )

        reads = read_before_state(1, "0xf3E3Eae671000612cE3fd15e1019154c1a4D693f", call)

        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].var_name, "coverageCap")
        self.assertEqual(reads[0].value, 5000000000000000)
        self.assertEqual(reads[0].key_args, (agent,))

    @patch("utils.on_chain_state.fetch_source", return_value=None)
    def test_no_source_returns_empty(self, mock_fetch: MagicMock) -> None:
        call = DecodedCall(function_name="setX", signature="setX(uint256)", params=[("uint256", 1)])
        self.assertEqual(read_before_state(1, "0xT", call), [])

    @patch("utils.on_chain_state.fetch_source")
    def test_struct_mapping_returns_empty(self, mock_fetch: MagicMock) -> None:
        source = """
        mapping(address => CreditLine) public creditLines;
        function setCreditLine(address _a, CreditLine memory _c) external { creditLines[_a] = _c; }
        """
        mock_fetch.return_value = ("Bank", source)
        call = DecodedCall(
            function_name="setCreditLine",
            signature="setCreditLine(address)",
            params=[("address", "0xA")],
        )
        self.assertEqual(read_before_state(1, "0xT", call), [])


class TestFormatStateReads(unittest.TestCase):
    def test_simple(self) -> None:
        reads = [StateRead(var_name="maxSlippage", type_str="uint256", value=999, key_args=())]
        result = format_state_reads(reads)
        self.assertIn("maxSlippage = 999", result)
        self.assertIn("uint256", result)

    def test_mapping(self) -> None:
        reads = [StateRead(var_name="cap", type_str="mapping(address => uint256)", value=42, key_args=("0xA",))]
        result = format_state_reads(reads)
        self.assertIn("cap(", result)
        self.assertIn("0xA", result)
        self.assertIn("= 42", result)

    def test_empty(self) -> None:
        self.assertEqual(format_state_reads([]), "")


if __name__ == "__main__":
    unittest.main()
