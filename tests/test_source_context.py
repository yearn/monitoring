"""Tests for utils/source_context.py."""

import json
import unittest
from unittest.mock import patch

from utils.source_context import (
    _concat_sources,
    _extract_function_body,
    _extract_function_snippet,
    _function_signature_from_abi,
    extract_state_var_snippet,
    fetch_function_input_names,
    find_state_var_writes,
    get_contract_label,
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


class TestGetContractLabel(unittest.TestCase):
    """Tests for get_contract_label()."""

    def setUp(self) -> None:
        reset_cache()

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_returns_verified_contract_name(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "contract Farm { }", "ContractName": "MorphoFarm"}],
        }
        label = get_contract_label(1, "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694")
        self.assertEqual(label, "MorphoFarm")

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_unverified_returns_empty(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "", "ContractName": ""}],
        }
        label = get_contract_label(1, "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694")
        self.assertEqual(label, "")

    def test_safe_utility_shortcut(self) -> None:
        # MultiSendCallOnly — no Etherscan call should be needed.
        label = get_contract_label(1, "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D")
        self.assertEqual(label, "Safe MultiSendCallOnly")

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.proxy.get_current_implementation")
    @patch("utils.source_context.fetch_json")
    def test_follows_proxy_when_name_is_generic(self, mock_fetch: object, mock_impl: object) -> None:
        mock_fetch.side_effect = [  # type: ignore[attr-defined]
            {"status": "1", "result": [{"SourceCode": "/* proxy */", "ContractName": "TransparentUpgradeableProxy"}]},
            {"status": "1", "result": [{"SourceCode": "/* impl */", "ContractName": "InfinifiBorrowingFarm"}]},
        ]
        mock_impl.return_value = "0x000000000000000000000000000000000000beef"  # type: ignore[attr-defined]
        label = get_contract_label(1, "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694")
        self.assertEqual(label, "InfinifiBorrowingFarm")

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.proxy.get_current_implementation", return_value=None)
    @patch("utils.source_context.fetch_json")
    def test_keeps_specific_name_without_proxy_follow(self, mock_fetch: object, mock_impl: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "FarmRegistry"}],
        }
        label = get_contract_label(1, "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694")
        self.assertEqual(label, "FarmRegistry")
        mock_impl.assert_not_called()  # type: ignore[attr-defined]

    def test_empty_address_returns_empty(self) -> None:
        self.assertEqual(get_contract_label(1, ""), "")

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.swiss_knife.fetch_json")
    @patch("utils.source_context.fetch_json")
    def test_prefers_swiss_knife_over_etherscan(self, mock_es: object, mock_sk: object) -> None:
        # Swiss Knife knows USDC by its full curated name; Etherscan would just
        # return "FiatTokenV2_2". We want the curated label.
        from utils.swiss_knife import reset_cache as sk_reset

        sk_reset()
        mock_sk.return_value = ["Circle: USDC Token", "circle", "stablecoin"]  # type: ignore[attr-defined]
        mock_es.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "FiatTokenV2_2"}],
        }
        label = get_contract_label(1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.assertEqual(label, "Circle: USDC Token")
        # Etherscan should not have been hit since Swiss Knife was authoritative.
        mock_es.assert_not_called()  # type: ignore[attr-defined]

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.swiss_knife.fetch_json")
    @patch("utils.source_context.fetch_json")
    def test_falls_back_to_etherscan_when_swiss_knife_empty(self, mock_es: object, mock_sk: object) -> None:
        from utils.swiss_knife import reset_cache as sk_reset

        sk_reset()
        mock_sk.return_value = {"error": "Error fetching data"}  # type: ignore[attr-defined]
        mock_es.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "FarmRegistry"}],
        }
        label = get_contract_label(1, "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694")
        self.assertEqual(label, "FarmRegistry")


class TestFetchFunctionInputNames(unittest.TestCase):
    """ABI-driven parameter name extraction."""

    def setUp(self) -> None:
        reset_cache()

    _SAMPLE_ABI = (
        '[{"type":"function","name":"setMaxSlippage","inputs":[{"name":"_maxSlippage","type":"uint256"}],'
        '"outputs":[],"stateMutability":"nonpayable"},'
        '{"type":"function","name":"anonymousArgs","inputs":[{"name":"","type":"uint256"}],"outputs":[]},'
        '{"type":"function","name":"noArgs","inputs":[],"outputs":[]}]'
    )

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_returns_named_inputs(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "Farm", "ABI": self._SAMPLE_ABI}],
        }
        names = fetch_function_input_names(1, "0xabc", "setMaxSlippage")
        self.assertEqual(names, ["_maxSlippage"])

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_empty_inputs_for_noarg_function(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "Farm", "ABI": self._SAMPLE_ABI}],
        }
        self.assertEqual(fetch_function_input_names(1, "0xabc", "noArgs"), [])

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_returns_none_when_inputs_unnamed(self, mock_fetch: object) -> None:
        # Mixing named + anonymous params is worse than nothing — return None
        # so the formatter falls back to bare types for the whole signature.
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "Farm", "ABI": self._SAMPLE_ABI}],
        }
        self.assertIsNone(fetch_function_input_names(1, "0xabc", "anonymousArgs"))

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.source_context.fetch_json")
    def test_returns_none_when_function_not_in_abi(self, mock_fetch: object) -> None:
        mock_fetch.return_value = {  # type: ignore[attr-defined]
            "status": "1",
            "result": [{"SourceCode": "/* x */", "ContractName": "Farm", "ABI": self._SAMPLE_ABI}],
        }
        self.assertIsNone(fetch_function_input_names(1, "0xabc", "doesNotExist"))

    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"})
    @patch("utils.proxy.get_current_implementation")
    @patch("utils.source_context.fetch_json")
    def test_follows_proxy_to_impl_abi(self, mock_fetch: object, mock_impl: object) -> None:
        proxy_abi = '[{"type":"function","name":"fallback","inputs":[]}]'
        mock_fetch.side_effect = [  # type: ignore[attr-defined]
            {"status": "1", "result": [{"SourceCode": "/* x */", "ContractName": "ERC1967Proxy", "ABI": proxy_abi}]},
            {"status": "1", "result": [{"SourceCode": "/* x */", "ContractName": "Farm", "ABI": self._SAMPLE_ABI}]},
        ]
        mock_impl.return_value = "0x" + "11" * 20  # type: ignore[attr-defined]
        names = fetch_function_input_names(1, "0xProxy", "setMaxSlippage")
        self.assertEqual(names, ["_maxSlippage"])


class TestFunctionSignatureFromAbi(unittest.TestCase):
    """Tests for _function_signature_from_abi (selector → canonical signature)."""

    _ABI = json.dumps(
        [
            {"type": "function", "name": "transfer", "inputs": [{"type": "address"}, {"type": "uint256"}]},
            {"type": "function", "name": "pause", "inputs": []},
            {
                "type": "function",
                "name": "configure",
                "inputs": [{"type": "tuple", "components": [{"type": "address"}, {"type": "uint256"}]}],
            },
        ]
    )

    def test_matches_selector(self) -> None:
        # transfer(address,uint256) selector is 0xa9059cbb.
        self.assertEqual(_function_signature_from_abi(self._ABI, "0xa9059cbb"), "transfer(address,uint256)")

    def test_no_arg_function(self) -> None:
        # pause() selector is 0x8456cb59.
        self.assertEqual(_function_signature_from_abi(self._ABI, "0x8456cb59"), "pause()")

    def test_expands_tuple_types(self) -> None:
        # configure((address,uint256)) — tuple expanded by collapse_if_tuple.
        from eth_utils import function_signature_to_4byte_selector

        sel = "0x" + function_signature_to_4byte_selector("configure((address,uint256))").hex()
        self.assertEqual(_function_signature_from_abi(self._ABI, sel), "configure((address,uint256))")

    def test_unknown_selector_returns_none(self) -> None:
        self.assertIsNone(_function_signature_from_abi(self._ABI, "0xdeadbeef"))

    def test_unverified_abi_returns_none(self) -> None:
        self.assertIsNone(_function_signature_from_abi("Contract source code not verified", "0xa9059cbb"))


if __name__ == "__main__":
    unittest.main()
