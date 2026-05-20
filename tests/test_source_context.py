"""Tests for utils/source_context.py."""

import unittest
from unittest.mock import patch

from utils.source_context import (
    _concat_sources,
    _extract_function_body,
    _extract_function_snippet,
    extract_state_var_snippet,
    find_state_var_writes,
    get_source_context,
    reset_cache,
)

# Inlined copy of the InfiniFi Farm.sol natspec + setMaxSlippage + maxSlippage declaration.
# This is the real-world example that motivated the fix.
INFINIFI_FARM_SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

abstract contract Farm {
    /// @notice Max slippage for depositing and witdhrawing assets from the farm.
    /// @dev Stored as a percentage with 18 decimals of precision, of the minimum
    /// position size compared to the previous position size (so actually 1 - slippage).
    /// @dev Set to 0 to disable slippage checks.
    uint256 public maxSlippage;

    event MaxSlippageUpdated(uint256 newMaxSlippage);

    /// @notice set the max tolerated slippage for depositing and witdhrawing assets from the farm
    function setMaxSlippage(uint256 _maxSlippage) external onlyCoreRole(CoreRoles.PROTOCOL_PARAMETERS) {
        maxSlippage = _maxSlippage;
        emit MaxSlippageUpdated(_maxSlippage);
    }
}
"""


class TestExtractFunctionSnippet(unittest.TestCase):
    def test_extracts_natspec_and_signature(self) -> None:
        snippet = _extract_function_snippet(INFINIFI_FARM_SOURCE, "setMaxSlippage")
        self.assertIn("set the max tolerated slippage", snippet)
        self.assertIn("function setMaxSlippage(uint256 _maxSlippage) external", snippet)

    def test_missing_function_returns_empty(self) -> None:
        snippet = _extract_function_snippet(INFINIFI_FARM_SOURCE, "doesNotExist")
        self.assertEqual(snippet, "")

    def test_function_without_natspec(self) -> None:
        source = "function bareFunction() external { x = 1; }"
        snippet = _extract_function_snippet(source, "bareFunction")
        self.assertIn("function bareFunction()", snippet)


class TestExtractFunctionBody(unittest.TestCase):
    def test_extracts_body(self) -> None:
        body = _extract_function_body(INFINIFI_FARM_SOURCE, "setMaxSlippage")
        self.assertIn("maxSlippage = _maxSlippage", body)
        self.assertIn("emit MaxSlippageUpdated", body)

    def test_handles_nested_braces(self) -> None:
        source = """
        function complex() external {
            if (x) { y = 1; }
            for (uint i; i < 10; i++) { z[i] = i; }
        }
        """
        body = _extract_function_body(source, "complex")
        self.assertIn("y = 1", body)
        self.assertIn("z[i] = i", body)
        # Make sure the closing brace of the outer function is excluded
        self.assertEqual(body.count("{"), body.count("}"))


class TestFindStateVarWrites(unittest.TestCase):
    def test_finds_assignment(self) -> None:
        writes = find_state_var_writes(INFINIFI_FARM_SOURCE, "setMaxSlippage")
        self.assertIn("maxSlippage", writes)

    def test_ignores_local_and_keyword_assignments(self) -> None:
        source = """
        function f() external {
            uint256 local = 1;
            if (local == 1) { storedVar = local; }
            _underscoreVar = 5;
        }
        """
        writes = find_state_var_writes(source, "f")
        self.assertIn("storedVar", writes)
        self.assertNotIn("local", writes)  # locals can't be distinguished, but this test documents the heuristic limit
        self.assertNotIn("_underscoreVar", writes)
        self.assertNotIn("uint256", writes)


class TestExtractStateVarSnippet(unittest.TestCase):
    def test_extracts_natspec_and_declaration(self) -> None:
        snippet = extract_state_var_snippet(INFINIFI_FARM_SOURCE, "maxSlippage")
        self.assertIn("so actually 1 - slippage", snippet)
        self.assertIn("uint256 public maxSlippage", snippet)

    def test_missing_var_returns_empty(self) -> None:
        snippet = extract_state_var_snippet(INFINIFI_FARM_SOURCE, "doesNotExist")
        self.assertEqual(snippet, "")

    def test_skips_local_vars_without_visibility(self) -> None:
        source = """
        function f() external {
            uint256 plainLocal = 5;
        }
        """
        # Should not match — no public/private/internal/external modifier
        snippet = extract_state_var_snippet(source, "plainLocal")
        self.assertEqual(snippet, "")


class TestConcatSources(unittest.TestCase):
    def test_plain_solidity(self) -> None:
        source = "contract Foo { function bar() public {} }"
        self.assertEqual(_concat_sources(source), source)

    def test_double_brace_json_format(self) -> None:
        raw = '{{"sources":{"Foo.sol":{"content":"contract Foo {}"},"Bar.sol":{"content":"contract Bar {}"}}}}'
        result = _concat_sources(raw)
        self.assertIn("contract Foo {}", result)
        self.assertIn("contract Bar {}", result)

    def test_single_brace_json_format(self) -> None:
        raw = '{"sources":{"Foo.sol":{"content":"contract Foo {}"}}}'
        result = _concat_sources(raw)
        self.assertIn("contract Foo {}", result)

    def test_invalid_json_falls_back(self) -> None:
        raw = "{not valid json}"
        # Should fall back to raw string when JSON parsing fails
        self.assertEqual(_concat_sources(raw), raw)


class TestGetSourceContext(unittest.TestCase):
    def setUp(self) -> None:
        reset_cache()

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_returns_context_for_verified_contract(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": INFINIFI_FARM_SOURCE, "ContractName": "Farm"}],
        }
        ctx = get_source_context(1, "0xabc", "setMaxSlippage")
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx.contract_name, "Farm")
        self.assertIn("set the max tolerated slippage", ctx.function_snippet)
        self.assertEqual(len(ctx.state_var_snippets), 1)
        self.assertIn("so actually 1 - slippage", ctx.state_var_snippets[0])

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": ""}, clear=False)
    @patch("utils.source_context.fetch_json")
    def test_missing_api_key_returns_none(self, mock_fetch: object) -> None:
        import os

        os.environ.pop("ETHERSCAN_TOKEN", None)
        ctx = get_source_context(1, "0xabc", "setMaxSlippage")
        self.assertIsNone(ctx)
        mock_fetch.assert_not_called()  # type: ignore[attr-defined]

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_unverified_contract_returns_none(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "", "ContractName": ""}],
        }
        ctx = get_source_context(1, "0xabc", "setMaxSlippage")
        self.assertIsNone(ctx)

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_caches_per_address(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": INFINIFI_FARM_SOURCE, "ContractName": "Farm"}],
        }
        get_source_context(1, "0xabc", "setMaxSlippage")
        get_source_context(1, "0xabc", "setMaxSlippage")
        # Two calls — Etherscan should be hit only once
        self.assertEqual(mock_fetch.call_count, 1)  # type: ignore[attr-defined]

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.proxy.get_current_implementation")
    @patch("utils.source_context.fetch_json")
    def test_follows_proxy_when_function_not_in_target(self, mock_fetch: object, mock_impl: object) -> None:
        # Proxy source has no `setMaxSlippage`; implementation has it.
        proxy_source = "contract ERC1967Proxy { fallback() external payable {} }"
        mock_fetch.side_effect = [  # type: ignore[attr-defined]
            {"status": "1", "result": [{"SourceCode": proxy_source, "ContractName": "ERC1967Proxy"}]},
            {"status": "1", "result": [{"SourceCode": INFINIFI_FARM_SOURCE, "ContractName": "Farm"}]},
        ]
        mock_impl.return_value = "0xImplementation"  # type: ignore[attr-defined]

        ctx = get_source_context(1, "0xProxy", "setMaxSlippage")

        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx.contract_name, "Farm")
        self.assertIn("so actually 1 - slippage", ctx.state_var_snippets[0])
        mock_impl.assert_called_once_with("0xProxy", 1)  # type: ignore[attr-defined]

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.proxy.get_current_implementation", return_value=None)
    @patch("utils.source_context.fetch_json")
    def test_no_proxy_follow_when_no_impl(self, mock_fetch: object, mock_impl: object) -> None:
        # Function missing from source, no proxy impl → return None.
        proxy_source = "contract ERC1967Proxy { fallback() external payable {} }"
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": proxy_source, "ContractName": "ERC1967Proxy"}],
        }
        ctx = get_source_context(1, "0xPlain", "setMaxSlippage")
        self.assertIsNone(ctx)

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.proxy.get_current_implementation")
    @patch("utils.source_context.fetch_json")
    def test_proxy_follow_skipped_when_impl_equals_target(self, mock_fetch: object, mock_impl: object) -> None:
        # get_current_implementation returns same address (heuristic guard) → don't loop.
        proxy_source = "contract Plain { function bar() external {} }"
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": proxy_source, "ContractName": "Plain"}],
        }
        mock_impl.return_value = "0xABC"  # type: ignore[attr-defined]
        ctx = get_source_context(1, "0xABC", "setMaxSlippage")
        self.assertIsNone(ctx)
        # Should only fetch once (the target), not retry for impl
        self.assertEqual(mock_fetch.call_count, 1)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
