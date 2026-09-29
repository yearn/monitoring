"""Tests for utils/on_chain_state.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.on_chain_state import (
    MAX_ARRAY_KEY_READS,
    StateRead,
    _is_externally_readable,
    _is_simple_type,
    _match_key_value_from_params,
    _parse_var_declaration,
    format_state_reads,
    read_before_state,
)


class TestIsExternallyReadable(unittest.TestCase):
    def test_public_var(self) -> None:
        self.assertTrue(_is_externally_readable("uint256 public maxSlippage;"))

    def test_public_mapping(self) -> None:
        self.assertTrue(_is_externally_readable("mapping(address => uint256) public coverageCap;"))

    def test_internal_var(self) -> None:
        self.assertFalse(_is_externally_readable("mapping(address => Configuration) internal configuratorParams;"))

    def test_private_var(self) -> None:
        self.assertFalse(_is_externally_readable("uint256 private _secret;"))

    def test_ignores_natspec_mentioning_public(self) -> None:
        snippet = "/// @notice exposed via a public helper\nuint256 internal _x;"
        self.assertFalse(_is_externally_readable(snippet))


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

    def test_named_mapping_parameters(self) -> None:
        # ConnectorCCTP_Chainlink: the bool value was misreported as the setter's uint32.
        snippet = "/// @notice Sentinel\nmapping(uint256 chainId => bool) public cctpDomainConfigured;"
        self.assertEqual(_parse_var_declaration(snippet, "cctpDomainConfigured"), ("bool", ["uint256"]))
        snippet = "mapping(uint256 chainId => uint32 domain) public cctpDomains;"
        self.assertEqual(_parse_var_declaration(snippet, "cctpDomains"), ("uint32", ["uint256"]))

    def test_named_nested_mapping_skipped(self) -> None:
        snippet = "mapping(address owner => mapping(address spender => uint256)) public allowances;"
        self.assertIsNone(_parse_var_declaration(snippet, "allowances"))

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

    @patch("utils.on_chain_state.ChainManager")
    @patch("utils.on_chain_state.fetch_source")
    def test_internal_var_skipped_without_eth_call(self, mock_fetch: MagicMock, mock_chain: MagicMock) -> None:
        # Compound III Configurator: configuratorParams is `internal`, so there is
        # no auto-generated getter — the read must be skipped, not attempted.
        source = """
        mapping(address => Configuration) internal configuratorParams;
        function setSupplyKink(address cometProxy, uint64 newSupplyKink) external {
            configuratorParams[cometProxy].supplyKink = newSupplyKink;
        }
        """
        mock_fetch.return_value = ("Configurator", source)
        mock_client = MagicMock()
        mock_chain.get_client.return_value = mock_client

        call = DecodedCall(
            function_name="setSupplyKink",
            signature="setSupplyKink(address,uint64)",
            params=[("address", "0xc3d688B66703497DAA19211EEdff47f25384cdc3"), ("uint64", 850000000000000000)],
        )

        reads = read_before_state(1, "0x316f9708bb98af7da9c68c1c3b5e79039cd336e3", call)

        self.assertEqual(reads, [])
        mock_client.eth.call.assert_not_called()

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

    def test_unavailable(self) -> None:
        reads = [
            StateRead(var_name="cap", type_str="", value=None, key_args=("0xA",), available=False),
        ]
        result = format_state_reads(reads)
        self.assertIn("cap(0xA) = unavailable", result)
        self.assertIn("could not be read", result)


# Trimmed from the yETH recovery claim contract (Vyper 0.4).
VYPER_CLAIM_SOURCE = """
management: public(address)
pending_management: public(address)
unclaimed: public(uint256)
secret: uint256
claimable: public(HashMap[address, uint256])
PRECISION: constant(uint256) = 10**18

@external
def set_claimable(_accounts: DynArray[address, 64], _amounts: DynArray[uint256, 64]):
    \"\"\"
    @notice Set the claimable amount for a set of addresses
    \"\"\"
    assert msg.sender == self.management
    unclaimed: uint256 = self.unclaimed
    for i: uint256 in range(len(_accounts), bound=64):
        account: address = _accounts[i]
        amount: uint256 = _amounts[i]
        unclaimed = unclaimed - self.claimable[account] + amount
        self.claimable[account] = amount
    self.unclaimed = unclaimed

@external
def set_management(_management: address):
    assert msg.sender == self.management
    self.pending_management = _management
"""


class TestVyperDeclarations(unittest.TestCase):
    """Vyper storage is readable only through ``public(...)``."""

    def test_public_hashmap(self) -> None:
        decl = "claimable: public(HashMap[address, uint256])"
        self.assertEqual(_parse_var_declaration(decl, "claimable"), ("uint256", ["address"]))
        self.assertTrue(_is_externally_readable(decl))

    def test_public_scalar_and_bounded_string(self) -> None:
        self.assertEqual(_parse_var_declaration("management: public(address)", "management"), ("address", []))
        self.assertEqual(_parse_var_declaration("name: public(String[64])", "name"), ("string", []))

    def test_private_storage_is_not_readable(self) -> None:
        self.assertFalse(_is_externally_readable("secret: uint256"))
        self.assertIsNone(_parse_var_declaration("secret: uint256", "secret"))

    def test_nested_hashmap_skipped(self) -> None:
        decl = "allowance: public(HashMap[address, HashMap[address, uint256]])"
        self.assertIsNone(_parse_var_declaration(decl, "allowance"))


def _client_returning(values_by_account: dict[str, int], scalar: int = 0) -> MagicMock:
    """Mock client answering claimable(account) per account, and any no-arg getter with ``scalar``."""
    from eth_abi import decode as abi_decode
    from eth_abi import encode as abi_encode

    def call(tx: dict) -> bytes:
        data = bytes.fromhex(tx["data"][2:])
        if len(data) == 4 + 32:
            (account,) = abi_decode(["address"], data[4:])
            return abi_encode(["uint256"], [values_by_account[account.lower()]])
        return abi_encode(["uint256"], [scalar])

    client = MagicMock()
    client.eth.call.side_effect = call
    return client


class TestVyperArrayKeyedSetter(unittest.TestCase):
    """``set_claimable(address[], uint256[])`` reads ``claimable(account)`` for each element."""

    ACCOUNTS = ["0xFC5453ACF7807455d6bC6406a06d6A6Bb26DFf55", "0x1BF44e3b0DfC84046a416c2Cd2040860EC593e53"]

    def _call(self, accounts: list[str]) -> DecodedCall:
        return DecodedCall(
            function_name="set_claimable",
            signature="set_claimable(address[],uint256[])",
            params=[("address[]", accounts), ("uint256[]", [0] * len(accounts))],
        )

    @patch("utils.on_chain_state.ChainManager")
    @patch("utils.on_chain_state.fetch_source", return_value=("yETH recovery claim", VYPER_CLAIM_SOURCE))
    def test_reads_each_array_key_and_scalar(self, _mock_fetch: MagicMock, mock_chain: MagicMock) -> None:
        values = {self.ACCOUNTS[0].lower(): 29 * 10**18, self.ACCOUNTS[1].lower(): 5 * 10**18}
        mock_chain.get_client.return_value = _client_returning(values, scalar=318 * 10**18)

        reads = read_before_state(1, "0x9564850c7090B13794e6d1164B0826C0aEFf3143", self._call(self.ACCOUNTS))

        claimable = [r for r in reads if r.var_name == "claimable"]
        self.assertEqual([r.key_args for r in claimable], [(a,) for a in self.ACCOUNTS])
        self.assertEqual([r.value for r in claimable], [29 * 10**18, 5 * 10**18])
        self.assertEqual(claimable[0].type_str, "mapping(address => uint256)")
        # The local `unclaimed` is not mistaken for storage; `self.unclaimed` is.
        self.assertEqual([r.value for r in reads if r.var_name == "unclaimed"], [318 * 10**18])

    @patch("utils.on_chain_state.ChainManager")
    @patch("utils.on_chain_state.fetch_source", return_value=("yETH recovery claim", VYPER_CLAIM_SOURCE))
    def test_array_keys_beyond_cap_are_unavailable(self, _mock_fetch: MagicMock, mock_chain: MagicMock) -> None:
        accounts = [f"0x{i + 1:040x}" for i in range(MAX_ARRAY_KEY_READS + 2)]
        mock_chain.get_client.return_value = _client_returning({a.lower(): 1 for a in accounts})

        reads = [
            r
            for r in read_before_state(1, "0x9564850c7090B13794e6d1164B0826C0aEFf3143", self._call(accounts))
            if r.var_name == "claimable"
        ]

        self.assertEqual(len(reads), MAX_ARRAY_KEY_READS + 2)
        self.assertTrue(all(r.available for r in reads[:MAX_ARRAY_KEY_READS]))
        self.assertFalse(any(r.available for r in reads[MAX_ARRAY_KEY_READS:]))

    @patch("utils.on_chain_state.ChainManager")
    @patch("utils.on_chain_state.fetch_source", return_value=("yETH recovery claim", VYPER_CLAIM_SOURCE))
    def test_vyper_pending_management(self, _mock_fetch: MagicMock, mock_chain: MagicMock) -> None:
        from eth_abi import encode as abi_encode

        client = MagicMock()
        client.eth.call.return_value = abi_encode(["address"], ["0x" + "00" * 20])
        mock_chain.get_client.return_value = client
        call = DecodedCall("set_management", "set_management(address)", [("address", "0x" + "ab" * 20)])

        reads = read_before_state(1, "0x9564850c7090B13794e6d1164B0826C0aEFf3143", call)

        self.assertEqual([(r.var_name, r.type_str) for r in reads], [("pending_management", "address")])


if __name__ == "__main__":
    unittest.main()
