"""Tests for timelock/calldata_decoder.py."""

import unittest
from unittest.mock import patch

from utils.calldata.decoder import (
    DecodedCall,
    _format_param_value,
    _parse_param_types,
    _selector_cache,
    decode_calldata,
    format_call_lines,
    resolve_selector,
)
from utils.calldata.known_selectors import KNOWN_SELECTORS

# A selector guaranteed NOT to be in the local table, for testing API fallback.
_UNKNOWN_SELECTOR = "0x11223344"


class TestParseParamTypes(unittest.TestCase):
    """Tests for _parse_param_types."""

    def test_no_params(self):
        self.assertEqual(_parse_param_types("pause()"), [])

    def test_single_param(self):
        self.assertEqual(_parse_param_types("withdraw(uint256)"), ["uint256"])

    def test_multiple_params(self):
        self.assertEqual(
            _parse_param_types("grantRole(bytes32,address)"),
            ["bytes32", "address"],
        )

    def test_complex_types(self):
        self.assertEqual(
            _parse_param_types("foo(uint256,address,bool,bytes32)"),
            ["uint256", "address", "bool", "bytes32"],
        )

    def test_no_parentheses(self):
        self.assertEqual(_parse_param_types("malformed"), [])


class TestFormatParamValue(unittest.TestCase):
    """Tests for _format_param_value."""

    def test_address(self):
        # eth_abi returns addresses as lowercase hex strings
        result = _format_param_value("address", "0x5d8a7dc9405f08f14541ba918c1bf7eb2dace556")
        self.assertEqual(result, "0x5d8A7DC9405F08F14541BA918c1Bf7eb2dACE556")

    def test_uint256(self):
        self.assertEqual(_format_param_value("uint256", 1000), "1000")

    def test_int256(self):
        self.assertEqual(_format_param_value("int256", -42), "-42")

    def test_bytes32(self):
        val = b"\x00" * 31 + b"\x01"
        result = _format_param_value("bytes32", val)
        self.assertEqual(result, "0x" + "00" * 31 + "01")

    def test_bytes_short(self):
        val = b"\xde\xad"
        result = _format_param_value("bytes", val)
        self.assertEqual(result, "0xdead")

    def test_bytes_long_truncated(self):
        val = b"\xff" * 64
        result = _format_param_value("bytes", val)
        self.assertTrue(result.endswith("..."))
        # 0x + 64 chars = 66, then ...
        self.assertEqual(len(result), 66 + 3)

    def test_bool(self):
        self.assertEqual(_format_param_value("bool", True), "True")
        self.assertEqual(_format_param_value("bool", False), "False")

    def test_string(self):
        self.assertEqual(_format_param_value("string", "hello"), '"hello"')

    def test_fallback(self):
        self.assertEqual(_format_param_value("tuple", (1, 2)), "(1, 2)")


class TestResolveSelector(unittest.TestCase):
    """Tests for resolve_selector."""

    def setUp(self):
        _selector_cache.clear()

    def test_known_selector_no_api_call(self):
        """Selectors in the local table should resolve without any API call."""
        with patch("utils.calldata.decoder.fetch_json") as mock_fetch:
            result = resolve_selector("0xa9059cbb")
            self.assertEqual(result, "transfer(address,uint256)")
            mock_fetch.assert_not_called()

    def test_known_selector_case_insensitive(self):
        """Local table lookup should be case-insensitive."""
        result = resolve_selector("0xA9059CBB")
        self.assertEqual(result, "transfer(address,uint256)")

    @patch("utils.calldata.decoder.fetch_json")
    def test_api_fallback_for_unknown_selector(self, mock_fetch):
        """Selectors not in the local table should fall through to the API."""
        mock_fetch.return_value = {
            "result": {
                "function": {
                    _UNKNOWN_SELECTOR: [{"name": "someRareFunction(uint256)"}],
                }
            }
        }
        result = resolve_selector(_UNKNOWN_SELECTOR)
        self.assertEqual(result, "someRareFunction(uint256)")
        mock_fetch.assert_called_once()

    @patch("utils.calldata.decoder.fetch_json")
    def test_runtime_cache_hit(self, mock_fetch):
        """Previously resolved selectors should be served from runtime cache."""
        _selector_cache[_UNKNOWN_SELECTOR] = "cachedFunc(address)"
        result = resolve_selector(_UNKNOWN_SELECTOR)
        self.assertEqual(result, "cachedFunc(address)")
        mock_fetch.assert_not_called()

    @patch("utils.calldata.decoder.fetch_json")
    def test_runtime_cache_hit_none(self, mock_fetch):
        """Cached None (previously failed lookup) should return None without API call."""
        _selector_cache[_UNKNOWN_SELECTOR] = None
        result = resolve_selector(_UNKNOWN_SELECTOR)
        self.assertIsNone(result)
        mock_fetch.assert_not_called()

    @patch("utils.calldata.decoder.fetch_json")
    def test_api_returns_none(self, mock_fetch):
        mock_fetch.return_value = None
        result = resolve_selector(_UNKNOWN_SELECTOR)
        self.assertIsNone(result)
        # Should be cached as None
        self.assertIsNone(_selector_cache[_UNKNOWN_SELECTOR])

    @patch("utils.calldata.decoder.fetch_json")
    def test_no_match(self, mock_fetch):
        mock_fetch.return_value = {"result": {"function": {_UNKNOWN_SELECTOR: []}}}
        result = resolve_selector(_UNKNOWN_SELECTOR)
        self.assertIsNone(result)


class TestKnownSelectors(unittest.TestCase):
    """Sanity checks for the known_selectors table."""

    def test_table_not_empty(self):
        self.assertGreater(len(KNOWN_SELECTORS), 0)

    def test_all_selectors_are_valid_hex(self):
        for selector in KNOWN_SELECTORS:
            self.assertTrue(selector.startswith("0x"), f"Missing 0x prefix: {selector}")
            self.assertEqual(len(selector), 10, f"Wrong length: {selector}")

    def test_all_signatures_have_parens(self):
        for selector, sig in KNOWN_SELECTORS.items():
            self.assertIn("(", sig, f"No opening paren in {selector}: {sig}")
            self.assertIn(")", sig, f"No closing paren in {selector}: {sig}")


# transfer(address,uint256) calldata:
# to=0x0000...0001, amount=1000
TRANSFER_CALLDATA = (
    "0xa9059cbb"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "00000000000000000000000000000000000000000000000000000000000003e8"
)


class TestDecodeCalldata(unittest.TestCase):
    """Tests for decode_calldata."""

    def setUp(self):
        _selector_cache.clear()

    @patch("utils.calldata.decoder.resolve_selector")
    def test_successful_decode(self, mock_resolve):
        mock_resolve.return_value = "transfer(address,uint256)"
        result = decode_calldata(TRANSFER_CALLDATA)

        self.assertIsNotNone(result)
        self.assertIsInstance(result, DecodedCall)
        self.assertEqual(result.function_name, "transfer")
        self.assertEqual(result.signature, "transfer(address,uint256)")
        self.assertEqual(len(result.params), 2)
        self.assertEqual(result.params[0][0], "address")
        self.assertEqual(result.params[1][0], "uint256")
        self.assertEqual(result.params[1][1], 1000)

    @patch("utils.calldata.decoder.resolve_selector")
    def test_no_params_function(self, mock_resolve):
        mock_resolve.return_value = "pause()"
        # selector only, no param data
        result = decode_calldata("0xabcd1234")

        self.assertIsNotNone(result)
        self.assertEqual(result.function_name, "pause")
        self.assertEqual(result.signature, "pause()")
        self.assertEqual(result.params, [])

    @patch("utils.calldata.decoder.resolve_selector")
    def test_selector_not_resolved(self, mock_resolve):
        mock_resolve.return_value = None
        result = decode_calldata("0xdeadbeef00112233")
        self.assertIsNone(result)

    def test_data_too_short(self):
        self.assertIsNone(decode_calldata("0x1234"))
        self.assertIsNone(decode_calldata(""))
        self.assertIsNone(decode_calldata(None))

    @patch("utils.calldata.decoder.resolve_selector")
    def test_malformed_param_data_still_returns(self, mock_resolve):
        """If param decoding fails, should still return DecodedCall with empty params."""
        mock_resolve.return_value = "transfer(address,uint256)"
        # Valid selector but truncated param data
        result = decode_calldata("0xa9059cbb0000")

        self.assertIsNotNone(result)
        self.assertEqual(result.function_name, "transfer")
        self.assertEqual(result.params, [])


class TestFormatCallLines(unittest.TestCase):
    """Tests for format_call_lines."""

    def setUp(self):
        _selector_cache.clear()

    @patch("utils.calldata.decoder.resolve_selector")
    def test_successful_format(self, mock_resolve):
        mock_resolve.return_value = "transfer(address,uint256)"
        lines = format_call_lines(TRANSFER_CALLDATA)

        self.assertTrue(len(lines) >= 1)
        self.assertIn("transfer(address,uint256)", lines[0])
        self.assertTrue(lines[0].startswith("📝 Function:"))
        # Should have param lines
        self.assertEqual(len(lines), 3)
        self.assertIn("address", lines[1])
        self.assertIn("uint256", lines[2])
        self.assertIn("1000", lines[2])

    @patch("utils.calldata.decoder.resolve_selector")
    def test_fallback_raw_selector(self, mock_resolve):
        mock_resolve.return_value = None
        lines = format_call_lines("0xdeadbeef00112233")

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "📝 Function: `0xdeadbeef`")

    def test_short_data_returns_empty(self):
        self.assertEqual(format_call_lines("0x12"), [])
        self.assertEqual(format_call_lines(""), [])
        self.assertEqual(format_call_lines(None), [])

    @patch("utils.calldata.decoder.resolve_selector")
    def test_no_params_format(self, mock_resolve):
        mock_resolve.return_value = "pause()"
        lines = format_call_lines("0xabcd1234")

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "📝 Function: `pause()`")


if __name__ == "__main__":
    unittest.main()
