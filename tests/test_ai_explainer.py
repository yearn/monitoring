"""Tests for utils/ai_explainer.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.erc20_metadata import ERC20Metadata
from utils.formatting import format_decimal_amount, normalize_token_amount
from utils.llm.ai_explainer import (
    MAX_PROMPT_CALLDATA_CHARS,
    MAX_PROMPT_SIMULATIONS,
    MAX_STATE_READ_KEYS_PER_SIGNATURE,
    SYSTEM_INSTRUCTIONS,
    Explanation,
    _build_prompt,
    _collect_safety_checks,
    _collect_state_reads,
    _collect_token_flows,
    _explanation_from_json,
    _generate_explanation,
    _parse_explanation,
    _sole_token_by_target,
    collect_unique_addresses,
    explain_batch_transaction,
    explain_transaction,
    format_explanation_line,
)
from utils.llm.base import LLMError
from utils.related_tokens import RelatedToken
from utils.source_context import SourceContext
from utils.tenderly.simulation import SimulationResult


class TestBuildPrompt(unittest.TestCase):
    """Tests for _build_prompt."""

    def test_basic_prompt(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(target="0xTarget", value=0, decoded_calls=calls, simulation=None)
        self.assertIn("Target: 0xTarget", result)
        self.assertIn("pause()", result)
        # Static instructions now live in the system prompt, not the user prompt.
        self.assertIn("DeFi risk analyst", SYSTEM_INSTRUCTIONS)

    def test_with_protocol_and_label(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(
            target="0xTarget",
            value=0,
            decoded_calls=calls,
            simulation=None,
            protocol="AAVE",
            label="Aave Governance V3",
        )
        self.assertIn("Protocol: AAVE", result)
        self.assertIn("Contract: Aave Governance V3", result)

    def test_with_eth_value(self) -> None:
        calls = [DecodedCall(function_name="transfer", signature="transfer(address,uint256)")]
        result = _build_prompt(target="0xTarget", value=int(1e18), decoded_calls=calls, simulation=None)
        self.assertIn("ETH Value:", result)

    def test_with_simulation(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        sim = SimulationResult(success=True, gas_used=50000)
        result = _build_prompt(target="0xTarget", value=0, decoded_calls=calls, simulation=sim)
        self.assertIn("Simulation Results", result)
        self.assertIn("SUCCESS", result)


class TestBuildPromptWithSourceContext(unittest.TestCase):
    """Tests for source context injection and hardened system prompt."""

    def test_source_context_appears_in_prompt(self) -> None:
        calls = [DecodedCall(function_name="setMaxSlippage", signature="setMaxSlippage(uint256)")]
        ctx = SourceContext(
            contract_name="Farm",
            function_snippet="/// @notice tight\nfunction setMaxSlippage(uint256) external;",
            state_var_snippets=["/// @dev so actually 1 - slippage\nuint256 public maxSlippage;"],
        )
        result = _build_prompt(
            target="0xT",
            value=0,
            decoded_calls=calls,
            simulation=None,
            source_contexts=[ctx],
        )
        self.assertIn("Contract Source Context", result)
        self.assertIn("so actually 1 - slippage", result)

    def test_hardened_prompt_includes_unit_guidance(self) -> None:
        # Unit-interpretation guidance is part of the static system prompt.
        self.assertIn("Do NOT assume the semantic meaning", SYSTEM_INSTRUCTIONS)
        self.assertIn("source context", SYSTEM_INSTRUCTIONS.lower())

    def test_context_note_appears_in_prompt(self) -> None:
        calls = [DecodedCall(function_name="swapOwner", signature="swapOwner(address,address,address)")]
        result = _build_prompt(
            target="0xT",
            value=0,
            decoded_calls=calls,
            simulation=None,
            context_note="Outer call is DELEGATECALL from the Safe.",
        )
        self.assertIn("--- Execution Context ---", result)
        self.assertIn("DELEGATECALL from the Safe", result)

    def test_safety_notes_appear_in_prompt(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(
            target="0xT",
            value=0,
            decoded_calls=calls,
            simulation=None,
            safety_notes=["0xT is UNVERIFIED on Etherscan — source is not published."],
        )
        self.assertIn("--- Safety Checks ---", result)
        self.assertIn("UNVERIFIED", result)

    def test_description_appears_in_prompt(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(
            target="0xT",
            value=0,
            decoded_calls=calls,
            simulation=None,
            description="Pause the vault during the migration window.",
        )
        self.assertIn("--- Stated Intent (proposal description) ---", result)
        self.assertIn("migration window", result)


class TestCollectSafetyChecks(unittest.TestCase):
    """Tests for _collect_safety_checks (seatbelt-style deterministic checks)."""

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=None)
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=False)
    def test_unverified_target_flagged(self, _mut: MagicMock, _ver: MagicMock) -> None:
        call = DecodedCall(function_name="pause", signature="pause()")
        notes = _collect_safety_checks([("0xT", call, 0)], chain_id=1)
        self.assertEqual(len(notes), 1)
        self.assertIn("UNVERIFIED", notes[0])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=None)
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    def test_verified_target_no_note(self, _mut: MagicMock, _ver: MagicMock) -> None:
        call = DecodedCall(function_name="pause", signature="pause()")
        self.assertEqual(_collect_safety_checks([("0xT", call, 0)], chain_id=1), [])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=None)
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=None)
    def test_unknown_verification_no_note(self, _mut: MagicMock, _ver: MagicMock) -> None:
        # None (no API key / fetch error) must not cry wolf.
        call = DecodedCall(function_name="pause", signature="pause()")
        self.assertEqual(_collect_safety_checks([("0xT", call, 0)], chain_id=1), [])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value="nonpayable")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    def test_value_to_nonpayable_flagged(self, _ver: MagicMock, _mut: MagicMock) -> None:
        call = DecodedCall(function_name="setConfig", signature="setConfig(uint256)")
        notes = _collect_safety_checks([("0xT", call, 10**18)], chain_id=1)
        self.assertEqual(len(notes), 1)
        self.assertIn("nonpayable", notes[0])
        self.assertIn("revert", notes[0])

    def test_value_to_view_or_pure_flagged(self) -> None:
        # view and pure functions are also non-payable; value to them reverts.
        for mut in ("view", "pure"):
            with self.subTest(mut=mut):
                with patch("utils.llm.ai_explainer.get_verification_status", return_value=True):
                    with patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=mut):
                        call = DecodedCall(function_name="getConfig", signature="getConfig()")
                        notes = _collect_safety_checks([("0xT", call, 10**18)], chain_id=1)
                self.assertEqual(len(notes), 1)
                self.assertIn(mut, notes[0])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value="payable")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    def test_value_to_payable_not_flagged(self, _ver: MagicMock, _mut: MagicMock) -> None:
        call = DecodedCall(function_name="deposit", signature="deposit()")
        self.assertEqual(_collect_safety_checks([("0xT", call, 10**18)], chain_id=1), [])

    @patch("utils.llm.ai_explainer.get_function_state_mutability")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=False)
    def test_unknown_call_still_flags_unverified_without_dummy_decoded(
        self, _ver: MagicMock, mock_mut: MagicMock
    ) -> None:
        notes = _collect_safety_checks([("0xT", None, 10**18)], chain_id=1)
        self.assertEqual(len(notes), 1)
        self.assertIn("UNVERIFIED", notes[0])
        mock_mut.assert_not_called()

    @patch("utils.llm.ai_explainer.get_function_state_mutability")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=False)
    def test_empty_calldata_does_not_flag_unverified(self, mock_ver: MagicMock, mock_mut: MagicMock) -> None:
        notes = _collect_safety_checks(
            [("0xT", None, 10**18)],
            chain_id=1,
            decode_statuses=["empty_calldata"],
        )
        self.assertEqual(notes, [])
        mock_ver.assert_not_called()
        mock_mut.assert_not_called()


class TestBatchParamConstants(unittest.TestCase):
    """Tests for the 'Shared Across Batch' section."""

    def test_surfaces_duplicate_arg(self) -> None:
        market = b"\x01" * 32
        calls = [
            DecodedCall(
                function_name="setCreditLines",
                signature="setCreditLines(bytes32,address,uint256)",
                params=[("bytes32", market), ("address", "0xA"), ("uint256", 100)],
            ),
            DecodedCall(
                function_name="setCreditLines",
                signature="setCreditLines(bytes32,address,uint256)",
                params=[("bytes32", market), ("address", "0xB"), ("uint256", 200)],
            ),
        ]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None)
        self.assertIn("--- Shared Across Batch ---", result)
        self.assertIn("arg[0]", result)
        self.assertNotIn("arg[1]", result)  # different across calls
        self.assertNotIn("arg[2]", result)

    def test_scope_names_decoded_subset_when_batch_has_unknowns(self) -> None:
        """The note must not claim a constant holds across calls it never saw."""
        market = b"\x01" * 32
        calls = [
            DecodedCall(
                function_name="setCap",
                signature="setCap(bytes32,uint256)",
                params=[("bytes32", market), ("uint256", cap)],
            )
            for cap in (100, 200, 300, 400)
        ]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None, total_calls=6)
        self.assertIn("identical across all 4 decoded calls (of 6 in the batch)", result)
        self.assertNotIn("identical across all 4 calls", result)

    def test_scope_omits_qualifier_when_every_call_decoded(self) -> None:
        market = b"\x01" * 32
        calls = [
            DecodedCall(
                function_name="setCap",
                signature="setCap(bytes32,uint256)",
                params=[("bytes32", market), ("uint256", cap)],
            )
            for cap in (100, 200)
        ]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None, total_calls=2)
        self.assertIn("identical across all 2 calls", result)
        self.assertNotIn("in the batch)", result)

    def test_single_call_no_section(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()", params=[])]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None)
        self.assertNotIn("--- Shared Across Batch ---", result)

    def test_mixed_signatures_no_section(self) -> None:
        calls = [
            DecodedCall(function_name="pause", signature="pause()"),
            DecodedCall(function_name="unpause", signature="unpause()"),
        ]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None)
        self.assertNotIn("--- Shared Across Batch ---", result)


class TestExplainTransaction(unittest.TestCase):
    """Tests for explain_transaction."""

    TARGET = "0x" + "aa" * 20

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_empty_calldata_is_deterministic(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_meta: MagicMock,
    ) -> None:
        result = explain_transaction(target=self.TARGET, calldata="0x", chain_id=1, value=10**18)
        assert result is not None
        mock_decode.assert_not_called()
        mock_simulate.assert_not_called()
        mock_get_provider.assert_called()
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertEqual(result.detail, "")
        self.assertNotIn("LOW", result.summary)
        self.assertNotIn("MEDIUM", result.summary)
        self.assertNotIn("**Risk:**", result.report)
        self.assertIn("empty calldata", result.report)
        self.assertIn("no function selector", result.report)
        self.assertNotIn("Native ETH transfer", result.report)
        self.assertNotIn("delivered", result.report.lower())
        self.assertIn("1. **Empty calldata**", result.report)
        self.assertIn("**ETH value:** `1.000000` ETH", result.report)

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_short_calldata_is_unknown_selector_not_empty_transfer(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_meta: MagicMock,
    ) -> None:
        result = explain_transaction(target=self.TARGET, calldata="0x1234", chain_id=1)
        assert result is not None
        mock_decode.assert_not_called()
        mock_simulate.assert_not_called()
        mock_get_provider.assert_called()
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertEqual(result.detail, "")
        self.assertIn("unknown_selector", result.report)
        self.assertIn("`0x1234`", result.report)
        self.assertNotIn("empty_calldata", result.report)
        self.assertNotIn("Empty calldata", result.report)
        self.assertNotIn("Native ETH transfer", result.report)

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=None)
    def test_undecoded_selector_is_deterministic(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_meta: MagicMock,
    ) -> None:
        result = explain_transaction(target=self.TARGET, calldata="0x11223344", chain_id=1)
        assert result is not None
        mock_decode.assert_called_once()
        mock_simulate.assert_not_called()
        mock_get_provider.assert_called()
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertEqual(result.detail, "")
        self.assertIn("unknown_selector", result.report)
        self.assertIn("1. **Undecoded calldata**", result.report)
        self.assertNotIn("**Risk:**", result.report)

    @patch("utils.llm.ai_explainer.get_llm_provider", side_effect=LLMError("LLM_API_KEY is not set"))
    def test_unconfigured_llm_skips_deterministic_path(self, _mock_get_provider: MagicMock) -> None:
        self.assertIsNone(explain_transaction(target=self.TARGET, calldata="0x", chain_id=1, value=10**18))


class TestStructuredOutput(unittest.TestCase):
    """Tests for the structured-output draft path and JSON→Explanation mapping."""

    def test_appends_risk_tag_when_missing(self) -> None:
        exp = _explanation_from_json({"summary": "Pauses the vault", "detail": "d", "risk_tag": "MEDIUM"})
        self.assertEqual(exp.summary, "Pauses the vault MEDIUM")
        self.assertEqual(exp.detail, "d")

    def test_normalizes_matching_trailing_tag(self) -> None:
        exp = _explanation_from_json({"summary": "Pauses the vault. LOW.", "detail": "d", "risk_tag": "LOW"})
        self.assertEqual(exp.summary, "Pauses the vault. LOW")

    def test_schema_tag_overrides_inlined_tag(self) -> None:
        # Model put LOW in the prose but the validated risk_tag is HIGH — schema wins.
        exp = _explanation_from_json({"summary": "Grants admin role. LOW.", "detail": "d", "risk_tag": "HIGH"})
        self.assertEqual(exp.summary, "Grants admin role. HIGH")


class TestParseExplanation(unittest.TestCase):
    """Tests for _parse_explanation."""

    def test_heading_containing_keyword_is_not_a_marker(self) -> None:
        """'## Detailed Analysis' must not match the DETAIL marker and get sliced."""
        raw = "## Detailed Analysis\n\nThe call registers a farm."
        result = _parse_explanation(raw)
        self.assertEqual(result.detail, "")
        self.assertIn("Detailed Analysis", result.summary)
        self.assertIn("registers a farm", result.summary)

    def test_both_sections(self) -> None:
        raw = "TLDR: Short summary here.\n\nDETAIL:\nDetailed analysis here."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Short summary here.")
        self.assertEqual(result.detail, "Detailed analysis here.")

    def test_tldr_only(self) -> None:
        raw = "TLDR: Just a summary, no detail."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Just a summary, no detail.")
        self.assertEqual(result.detail, "")

    def test_no_markers_fallback(self) -> None:
        raw = "This is a plain response without markers."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "This is a plain response without markers.")
        self.assertEqual(result.detail, "")

    def test_case_insensitive(self) -> None:
        raw = "tldr: Lower case markers.\n\ndetail:\nLower case detail."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Lower case markers.")
        self.assertEqual(result.detail, "Lower case detail.")

    def test_multiline_detail(self) -> None:
        raw = "TLDR: Summary.\n\nDETAIL:\nLine 1.\nLine 2.\n- Risk: HIGH"
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Summary.")
        self.assertIn("Line 1.", result.detail)
        self.assertIn("Risk: HIGH", result.detail)


class TestAddressLinksSection(unittest.TestCase):
    """The prompt hands the LLM ready-made explorer links to copy."""

    def test_links_block_included(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        links = "- [`0xAbc`](https://etherscan.io/address/0xAbc)"
        result = _build_prompt(target="0xTarget", value=0, decoded_calls=calls, simulation=None, address_links=links)
        self.assertIn("--- Address Links", result)
        self.assertIn(links, result)

    def test_section_omitted_when_no_links(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(target="0xTarget", value=0, decoded_calls=calls, simulation=None)
        self.assertNotIn("--- Address Links", result)

    def test_hyperlink_rule_in_system_prompt(self) -> None:
        self.assertIn("markdown link to the block explorer", SYSTEM_INSTRUCTIONS)


class TestRelatedTokensSection(unittest.TestCase):
    """The prompt tells the LLM which token a target's amounts are denominated in."""

    def test_section_included(self) -> None:
        calls = [DecodedCall(function_name="setEpochEmissions", signature="setEpochEmissions(uint256,uint256)")]
        block = "0xT (RewardsDistributor):\n  jane() -> 0xJ (JANE, 18 decimals)"
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None, related_tokens=block)
        self.assertIn("--- Related Tokens", result)
        self.assertIn("jane() -> 0xJ (JANE, 18 decimals)", result)

    def test_section_omitted_when_nothing_resolved(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None)
        self.assertNotIn("--- Related Tokens", result)

    def test_single_token_rule_in_system_prompt(self) -> None:
        self.assertIn("EXACTLY ONE token", SYSTEM_INSTRUCTIONS)

    def test_sole_token_map_skips_ambiguous_targets(self) -> None:
        one = RelatedToken(getter="jane", address="0xJ", symbol="JANE", decimals=18)
        two = RelatedToken(getter="usdc", address="0xU", symbol="USDC", decimals=6)
        mapping = _sole_token_by_target([("0xAaA", [one]), ("0xBbB", [one, two]), ("0xCcC", [])])
        self.assertEqual(mapping, {"0xaaa": one})


class TestProtocolContextSection(unittest.TestCase):
    """Protocol adapters can add verified facts to the prompt."""

    def test_section_included(self) -> None:
        calls = [DecodedCall(function_name="setRate", signature="setRate(address,uint256)")]
        result = _build_prompt(
            target="0xT",
            value=0,
            decoded_calls=calls,
            simulation=None,
            protocol_context="Farm: New Silver 2 Senior\nAccounting asset: USDC",
        )
        self.assertIn("--- Protocol Context", result)
        self.assertIn("Farm: New Silver 2 Senior", result)
        self.assertIn("Accounting asset: USDC", result)

    def test_system_prompt_distinguishes_whitelist_from_accounting_asset(self) -> None:
        self.assertIn("Distinguish", SYSTEM_INSTRUCTIONS)
        self.assertIn("non-accounting ERC20 targets", SYSTEM_INSTRUCTIONS)


class TestCollectUniqueAddresses(unittest.TestCase):
    """Targets and address args are gathered once, deduped, checksummed."""

    def test_target_and_args_deduped(self) -> None:
        farm = "0x79e1b8e45932a7c802ea3dab3844e5dea68d971f"
        registry = "0xF5f2718708f471e43968271956CC01aaA8c46119"
        call = DecodedCall(
            function_name="addFarms",
            signature="addFarms(uint256,address[])",
            params=[("uint256", 2), ("address[]", (farm, farm))],
        )
        result = collect_unique_addresses([(registry, call)])
        self.assertEqual(result, [registry, "0x79e1B8e45932A7C802eA3dAb3844e5DEa68d971f"])

    def test_addresses_inside_tuple_args_collected(self) -> None:
        """Struct args (e.g. MarketParams) must contribute their addresses."""
        farm = "0x79e1b8e45932a7c802ea3dab3844e5dea68d971f"
        registry = "0xF5f2718708f471e43968271956CC01aaA8c46119"
        call = DecodedCall(
            function_name="createMarket",
            signature="createMarket((address,uint256))",
            params=[("(address,uint256)", (farm, 5))],
        )
        self.assertEqual(
            collect_unique_addresses([(registry, call)]),
            [registry, "0x79e1B8e45932A7C802eA3dAb3844e5DEa68d971f"],
        )

    def test_zero_and_malformed_addresses_dropped(self) -> None:
        call = DecodedCall(
            function_name="transfer",
            signature="transfer(address,uint256)",
            params=[("address", "0x" + "00" * 20), ("uint256", 1)],
        )
        self.assertEqual(collect_unique_addresses([("0xnothex", call)]), [])

    def test_unknown_call_still_contributes_target(self) -> None:
        target = "0xF5f2718708f471e43968271956CC01aaA8c46119"
        self.assertEqual(collect_unique_addresses([(target, None)]), [target])


class TestFormatExplanationLine(unittest.TestCase):
    """Tests for format_explanation_line."""

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_report_published_when_present(self, mock_gist: MagicMock) -> None:
        """The full report (metadata + call flow + analysis) is what gets uploaded."""
        explanation = Explanation(
            summary="Pauses the vault. HIGH",
            detail="Full detail here.",
            report="## Call Flow\n\n1. pause()",
            title="Yearn Timelock - 11/08/2026 10:00 - HIGH",
        )
        result = format_explanation_line(explanation)
        mock_gist.assert_called_once_with(explanation.report, title=explanation.title)
        self.assertIn("https://gist.wavey.info/abc123", result)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_format_with_detail(self, mock_gist: MagicMock) -> None:
        explanation = Explanation(summary="This pauses the protocol.", detail="Full detail here.")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full detail here.", result)
        self.assertIn("https://gist.wavey.info/abc123", result)
        self.assertIn("Full details", result)
        mock_gist.assert_called_once_with("Full detail here.", title="AI Transaction Analysis")

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    def test_format_gist_failure(self, mock_gist: MagicMock) -> None:
        """If gist upload fails, surface a notice instead of a link."""
        explanation = Explanation(summary="This pauses the protocol.", detail="Full detail here.")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full details", result)
        self.assertIn("Couldn't post full report", result)

    def test_format_no_detail(self) -> None:
        """If there's no detail and no report, no gist upload is attempted."""
        explanation = Explanation(summary="This pauses the protocol.", detail="")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full details", result)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_report_only_uploads_without_detail(self, mock_gist: MagicMock) -> None:
        explanation = Explanation(
            summary="Could not decode 3 calls in this batch.",
            detail="",
            report="## Call Flow\n\n1. **Undecoded calldata**",
            title="Infinifi Shorttimelock - 11/08/2026 10:00",
        )
        result = format_explanation_line(explanation)
        mock_gist.assert_called_once_with(explanation.report, title=explanation.title)
        self.assertIn("Full details", result)
        self.assertIn("https://gist.wavey.info/abc123", result)

    @patch("utils.llm.ai_explainer._spill_unpublished_report", return_value="/tmp/report.md")
    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    def test_report_only_upload_failure_spills(self, mock_gist: MagicMock, mock_spill: MagicMock) -> None:
        explanation = Explanation(
            summary="Could not decode 3 calls in this batch.",
            detail="",
            report="## Call Flow\n\n1. **Undecoded calldata**",
            title="Infinifi Shorttimelock - 11/08/2026 10:00",
        )
        result = format_explanation_line(explanation)
        mock_gist.assert_called_once()
        mock_spill.assert_called_once()
        self.assertIn("Couldn't post full report", result)


class TestFailedSimulationDropped(unittest.TestCase):
    """Failed Tenderly simulations must not leak into the LLM prompt."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_failed_sim_omitted_from_single_prompt(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_label: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_simulate.return_value = SimulationResult(
            success=False, gas_used=0, error_message="execution reverted: not authorized"
        )
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: pauses. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x8456cb59", chain_id=1)
        prompt = provider.complete.call_args[0][0]

        self.assertNotIn("--- Simulation Results ---", prompt)
        self.assertNotIn("FAILED", prompt)
        self.assertNotIn("execution reverted", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_failed_sim_omitted_from_batch_prompt(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_label: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        from utils.llm.ai_explainer import explain_batch_transaction

        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_simulate.return_value = SimulationResult(success=False, gas_used=0, error_message="reverted")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: pauses both. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": "0x8456cb59", "value": "0"},
                {"target": "0xT2", "data": "0x8456cb59", "value": "0"},
            ],
            chain_id=1,
        )
        prompt = provider.complete.call_args[0][0]
        self.assertNotIn("--- Simulation Results ---", prompt)
        self.assertNotIn("FAILED", prompt)


class TestAbiParamNames(unittest.TestCase):
    """When the ABI is available, parameters render as `type name: value`."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_function_input_names")
    def test_named_params_appear_in_prompt(
        self,
        mock_names: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="setMaxSlippage",
            signature="setMaxSlippage(uint256)",
            params=[("uint256", 950000000000000000)],
        )
        mock_names.return_value = ["_maxSlippage"]
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: tightens slippage. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x736defe0" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        self.assertIn("uint256 _maxSlippage: 950000000000000000", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_function_input_names", return_value=None)
    def test_falls_back_to_bare_types_when_abi_missing(
        self,
        mock_names: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="setMaxSlippage",
            signature="setMaxSlippage(uint256)",
            params=[("uint256", 1)],
        )
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: tightens slippage. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x736defe0" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        # Without ABI names, params render as plain `type: value`.
        decoded_section = prompt.split("--- Decoded Calldata ---")[1]
        self.assertIn("uint256: 1", decoded_section)


class TestErc20MetadataInPrompt(unittest.TestCase):
    """ERC20 symbol/decimals are appended to address labels so the LLM can size amounts."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    def test_erc20_target_gets_decimals_suffix(
        self,
        mock_label: MagicMock,
        mock_meta: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        from utils.erc20_metadata import ERC20Metadata

        usdc = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        mock_decode.return_value = DecodedCall(
            function_name="transfer",
            signature="transfer(address,uint256)",
            params=[("address", "0x" + "11" * 20), ("uint256", 1000000)],
        )

        # Label resolver returns the USDC name; metadata fills in decimals.
        def label_for(_chain: int, addr: str) -> str:
            return "Circle: USDC Token" if addr.lower() == usdc.lower() else ""

        mock_label.side_effect = label_for
        mock_meta.side_effect = lambda _c, addr: ERC20Metadata("USDC", 6) if addr.lower() == usdc.lower() else None

        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: tiny transfer. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target=usdc, calldata="0xa9059cbb" + "00" * 64, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        # Target line should show the token symbol + decimals.
        self.assertIn("Circle: USDC Token (USDC, 6 dec)", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="FarmRegistry")
    def test_non_erc20_keeps_label_unchanged(
        self,
        mock_label: MagicMock,
        mock_meta: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="addFarms",
            signature="addFarms(uint256,address[])",
            params=[("uint256", 1), ("address[]", ("0x" + "ac" * 20,))],
        )
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: registers farm. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0x" + "ff" * 20, calldata="0xabcdef10" + "00" * 64, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        # FarmRegistry label without ERC20 decoration.
        self.assertIn("FarmRegistry", prompt)
        self.assertNotIn("dec)", prompt)


class TestRiskAnchorsSection(unittest.TestCase):
    """Risk Anchors block is added for calls with anchored selectors."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    def test_anchor_section_added_for_known_selector(
        self,
        mock_label: MagicMock,
        mock_meta: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="transferOwnership",
            signature="transferOwnership(address)",
            params=[("address", "0x" + "11" * 20)],
        )
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: hands ownership. HIGH."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0x" + "ff" * 20, calldata="0xf2fde38b" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        self.assertIn("--- Risk Anchors ---", prompt)
        self.assertIn("transferOwnership(address) → typically HIGH", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    def test_safe_enable_module_anchor_is_critical(
        self,
        mock_label: MagicMock,
        mock_meta: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="enableModule",
            signature="enableModule(address)",
            params=[("address", "0x" + "11" * 20)],
        )
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: enables a Safe module. CRITICAL."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0x" + "ff" * 20, calldata="0x610b5925" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        self.assertIn("--- Risk Anchors ---", prompt)
        self.assertIn("enableModule(address) → typically CRITICAL", prompt)
        self.assertIn("NO owner signatures", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    def test_no_anchor_section_for_unknown_selector(
        self,
        mock_label: MagicMock,
        mock_meta: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        # setMaxSlippage isn't anchored — parameter-dependent.
        mock_decode.return_value = DecodedCall(
            function_name="setMaxSlippage",
            signature="setMaxSlippage(uint256)",
            params=[("uint256", 1)],
        )
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: tightens slippage. LOW."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        explain_transaction(target="0x" + "ff" * 20, calldata="0x736defe0" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        self.assertNotIn("--- Risk Anchors ---", prompt)


class TestNestedBytesDecoding(unittest.TestCase):
    """`bytes` arguments that hold inner calldata are recursively decoded."""

    def test_inner_call_rendered_as_nested(self) -> None:
        from utils.llm.ai_explainer import _format_decoded_calls

        # `0x8456cb59` is pause() — a known selector, always decodes.
        inner_payload = "0x8456cb59"
        outer = DecodedCall(
            function_name="upgradeToAndCall",
            signature="upgradeToAndCall(address,bytes)",
            params=[
                ("address", "0x" + "ab" * 20),
                ("bytes", inner_payload),
            ],
        )
        result = _format_decoded_calls([outer])
        self.assertIn("bytes: ↳", result)
        self.assertIn("pause()", result)

    def test_undecodable_bytes_falls_back_to_raw(self) -> None:
        from utils.llm.ai_explainer import _format_decoded_calls

        garbage = "0xdeadbeefcafebabe"  # not a known selector, Sourcify miss in test env
        outer = DecodedCall(
            function_name="initialize",
            signature="initialize(bytes)",
            params=[("bytes", garbage)],
        )
        with patch("utils.calldata.decoder.decode_calldata", return_value=None):
            result = _format_decoded_calls([outer])
        self.assertIn(f"bytes: {garbage}", result)

    def test_unknown_selector_skipped_no_network(self) -> None:
        """A bytes blob whose selector isn't in KNOWN_SELECTORS must not trigger a network lookup."""
        from utils.calldata.decoder import _selector_cache
        from utils.llm.ai_explainer import _format_decoded_calls

        # Pollution from the persistent cache (prior runs may have resolved
        # this selector to something) would defeat the offline-only guard.
        _selector_cache.pop("0xdeadbeef", None)
        unknown = "0xdeadbeef" + "00" * 32  # well-formed length, selector unknown
        outer = DecodedCall(
            function_name="exec",
            signature="exec(bytes)",
            params=[("bytes", unknown)],
        )
        with patch("utils.calldata.decoder.decode_calldata") as mock_decode:
            result = _format_decoded_calls([outer])
            mock_decode.assert_not_called()
        self.assertIn(f"bytes: {unknown}", result)

    def test_unaligned_bytes_skipped(self) -> None:
        """Safe `signatures` (e.g. 195 bytes packed) and other non-calldata blobs are skipped."""
        from utils.llm.ai_explainer import _format_decoded_calls

        sigs_blob = "0x" + "11" * 195  # 3 packed Safe signatures, not calldata
        outer = DecodedCall(
            function_name="execTx",
            signature="execTx(bytes)",
            params=[("bytes", sigs_blob)],
        )
        with patch("utils.calldata.decoder.decode_calldata") as mock_decode:
            result = _format_decoded_calls([outer])
            mock_decode.assert_not_called()
        self.assertIn(sigs_blob, result)

    def test_recursion_depth_capped(self) -> None:
        from utils.calldata.decoder import MAX_BYTES_RECURSION_DEPTH
        from utils.llm.ai_explainer import _format_decoded_calls

        # Mock try_decode_inner_calldata so it always returns a self-referential
        # call, bypassing the selector/alignment guard. Without the depth cap
        # this would recurse forever.
        self_referential = DecodedCall(
            function_name="wrap",
            signature="wrap(bytes)",
            params=[("bytes", "0xfeedfacefeedfacefeedfacefeedfacefeedface")],
        )
        with patch("utils.llm.ai_explainer.try_decode_inner_calldata", return_value=self_referential):
            result = _format_decoded_calls([self_referential])
        self.assertEqual(result.count("↳"), MAX_BYTES_RECURSION_DEPTH)


class TestAddressLabels(unittest.TestCase):
    """Tests for address-argument annotation in the LLM prompt."""

    REGISTRY = "0xF5f2718708F471e43968271956cC01Aaa8C46119"
    FARM = "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694"
    FARM_CKS = "0xAc21B22B5aEb11bc32De4ecF59E4538fCa48b694"

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    def test_address_array_arg_is_labeled(
        self,
        mock_meta: MagicMock,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="addFarms",
            signature="addFarms(uint256,address[])",
            params=[("uint256", 1), ("address[]", (self.FARM,))],
        )
        mock_label.return_value = "MorphoFarm"
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: adds farm. LOW."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xabcdef10" + "00" * 64, chain_id=1)

        prompt = provider.complete.call_args[0][0]
        self.assertIn("MorphoFarm", prompt)
        self.assertIn(self.FARM_CKS, prompt)
        # Address goes on its own line, bulleted, under the type label.
        self.assertIn("address[]:", prompt)
        self.assertIn(f"- {self.FARM_CKS} (MorphoFarm)", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    def test_scalar_address_arg_is_labeled(
        self,
        mock_meta: MagicMock,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", self.FARM)],
        )
        mock_label.return_value = "ChainlinkOracle"
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: rewires oracle. MEDIUM."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)

        prompt = provider.complete.call_args[0][0]
        self.assertIn(f"address: {self.FARM_CKS} (ChainlinkOracle)", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    def test_target_appearing_as_arg_is_deduped(
        self,
        mock_meta: MagicMock,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        # The target now also gets labeled (so the Target: line can show
        # ERC20 decimals/symbol). When the same address appears as an
        # argument, we still want exactly one resolver call, not two.
        mock_decode.return_value = DecodedCall(
            function_name="selfWire",
            signature="selfWire(address)",
            params=[("address", self.REGISTRY.lower())],
        )
        mock_label.return_value = ""
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: wires self. LOW."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xdeadbeef" + "00" * 32, chain_id=1)

        self.assertEqual(mock_label.call_count, 1)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    def test_unverified_address_left_unannotated(
        self,
        mock_meta: MagicMock,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", self.FARM)],
        )
        mock_label.return_value = ""  # unverified / EOA / no API key
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: rewires. MEDIUM."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)

        prompt = provider.complete.call_args[0][0]
        # Address shows up, but with no `(Label)` suffix.
        self.assertIn(self.FARM_CKS, prompt)
        self.assertNotIn(f"{self.FARM_CKS} (", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    def test_address_inside_nested_bytes_is_labeled(
        self,
        mock_meta: MagicMock,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        """upgradeToAndCall(impl, initData) → the address inside initData must get a label."""
        from utils.calldata.decoder import decode_calldata as real_decode

        # initialize(address) calldata, address arg = 0x1111...1111
        inner_init_payload = "0xc4d66de8" + "00" * 12 + "11" * 20
        outer = DecodedCall(
            function_name="upgradeToAndCall",
            signature="upgradeToAndCall(address,bytes)",
            params=[("address", self.REGISTRY.lower()), ("bytes", inner_init_payload)],
        )

        # The outer decode is mocked (no fake selector to resolve); the inner
        # bytes recursion uses the real decoder so `initialize(address)`
        # resolves via KNOWN_SELECTORS and yields the inner address.
        def routed_decode(data: str, chain_id: int | None = None, target: str | None = None) -> DecodedCall | None:
            return outer if data == "0xUPGRADE_CALLDATA" else real_decode(data)

        mock_decode.side_effect = routed_decode
        mock_label.return_value = "ImplContract"

        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: upgrades. MEDIUM."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xUPGRADE_CALLDATA", chain_id=1)

        addresses_looked_up = {call.args[1].lower() for call in mock_label.call_args_list}
        self.assertIn("0x" + "11" * 20, addresses_looked_up)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    def test_zero_address_not_queried(
        self,
        mock_meta: MagicMock,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        # The target is still resolved (it's a real address) but the zero
        # address arg must be filtered before reaching the label resolver.
        zero = "0x" + "00" * 20
        mock_decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", zero)],
        )
        mock_label.return_value = ""
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: unsets oracle. LOW."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)
        # Resolver called exactly once — for the target, not for the zero arg.
        addresses_queried = {call.args[1].lower() for call in mock_label.call_args_list}
        self.assertNotIn(zero, addresses_queried)


class TestTokenFlows(unittest.TestCase):
    """Tests for deterministic token-flow normalization."""

    USDC = "0xa0b86991c6218b3e0d4f0d4e0f1b8f7a8e8c0d4e"
    R1 = "0x1111111111111111111111111111111111111111"
    R2 = "0x2222222222222222222222222222222222222222"

    def test_format_decimal_no_float_error(self) -> None:
        from decimal import Decimal

        # 50_780000 raw / 1e6 == exactly 50.78, not 50.78000001 or 50.8k.
        self.assertEqual(format_decimal_amount(Decimal(50_780000) / Decimal(10**6)), "50.78")
        self.assertEqual(format_decimal_amount(Decimal(1_000_000_000000) / Decimal(10**6)), "1,000,000")
        self.assertEqual(format_decimal_amount(Decimal(0)), "0")

    def test_normalize_is_immune_to_global_decimal_precision(self) -> None:
        """utils/defillama.py sets getcontext().prec = 18 process-wide on import.

        Division would silently truncate a 25-digit raw amount under that
        context; exponent construction must not.
        """
        from decimal import getcontext, localcontext

        with localcontext() as ctx:
            ctx.prec = 18
            amount = normalize_token_amount(5369214230155537376952673, 18)
        self.assertEqual(format_decimal_amount(amount), "5,369,214.230155537376952673")
        self.assertEqual(format_decimal_amount(normalize_token_amount(-50_780000, 6)), "-50.78")
        self.assertGreater(getcontext().prec, 0)  # context left untouched

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata")
    def test_transfer_amounts_normalized_with_total(self, mock_meta: MagicMock) -> None:
        mock_meta.return_value = ERC20Metadata(symbol="yvUSDC-1", decimals=6)
        calls = [
            (
                self.USDC,
                DecodedCall("transfer", "transfer(address,uint256)", [("address", self.R1), ("uint256", 50_000000)]),
            ),
            (
                self.USDC,
                DecodedCall("transfer", "transfer(address,uint256)", [("address", self.R2), ("uint256", 780000)]),
            ),
        ]
        out = _collect_token_flows(calls, chain_id=1)
        self.assertIn("transfer 50 yvUSDC-1", out)
        self.assertIn("transfer 0.78 yvUSDC-1", out)
        # Total of the two flows, computed deterministically — never the raw-unit sum.
        self.assertIn("Total moved: 50.78 yvUSDC-1", out)
        self.assertNotIn("50780000", out)

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata")
    def test_non_erc20_target_skipped(self, mock_meta: MagicMock) -> None:
        mock_meta.return_value = None  # decimals not discoverable
        calls = [
            (
                self.USDC,
                DecodedCall("transfer", "transfer(address,uint256)", [("address", self.R1), ("uint256", 50_000000)]),
            ),
        ]
        self.assertEqual(_collect_token_flows(calls, chain_id=1), "")

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata")
    def test_approve_listed_but_not_summed(self, mock_meta: MagicMock) -> None:
        mock_meta.return_value = ERC20Metadata(symbol="USDC", decimals=6)
        calls = [
            (
                self.USDC,
                DecodedCall("approve", "approve(address,uint256)", [("address", self.R1), ("uint256", 5_000000)]),
            ),
        ]
        out = _collect_token_flows(calls, chain_id=1)
        self.assertIn("approve 5 USDC", out)
        self.assertNotIn("Total moved", out)  # allowance isn't a balance move

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata")
    def test_token_flows_section_in_prompt(self, mock_meta: MagicMock) -> None:
        mock_meta.return_value = ERC20Metadata(symbol="yvUSDC-1", decimals=6)
        flows = _collect_token_flows(
            [
                (
                    self.USDC,
                    DecodedCall(
                        "transfer", "transfer(address,uint256)", [("address", self.R1), ("uint256", 50_780000)]
                    ),
                )
            ],
            chain_id=1,
        )
        prompt = _build_prompt(target=self.USDC, value=0, decoded_calls=[], simulation=None, token_flows=flows)
        self.assertIn("Token Flows (computed", prompt)
        self.assertIn("50.78 yvUSDC-1", prompt)


if __name__ == "__main__":
    unittest.main()


class TestCollectRoleNames(unittest.TestCase):
    """Tests for _collect_role_names (bytes32 role → name resolution).

    The call is built with the real `decode_calldata` rather than hand-written
    params on purpose: `eth_abi` hands back a bytes32 as 32 raw bytes, and an
    earlier version of this collector stringified that into a Python repr, so
    every role silently failed to resolve while string-based tests passed.
    """

    MINTER_HASH = "0x615a688d53344290b742a2e72e4f187e5b88227c01f9d77ce2406d32f8bd0eda"

    # Real call 0 of tx 0xcfa148be… — grantRole(RECEIPT_TOKEN_MINTER, OutlandVault).
    GRANT = decode_calldata(
        "0x2f2ff15d"
        "615a688d53344290b742a2e72e4f187e5b88227c01f9d77ce2406d32f8bd0eda"
        "000000000000000000000000a69e4155f62c097ce92daadef7a925dd40907c0c"
    )

    def test_decoded_role_param_is_raw_bytes(self) -> None:
        """Guards the assumption the collector depends on."""
        type_str, value = self.GRANT.params[0]
        self.assertEqual(type_str, "bytes32")
        self.assertIsInstance(value, bytes)

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_passes_normalized_hash_from_decoded_bytes(self, mock_resolve: MagicMock) -> None:
        """The hash handed to the resolver must be hex, not a bytes repr."""
        from utils.llm.ai_explainer import _collect_role_names

        mock_resolve.return_value = {}
        _collect_role_names([("0xCore", self.GRANT)], chain_id=1)

        passed = mock_resolve.call_args[0][0]
        self.assertEqual(passed, [self.MINTER_HASH])

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_resolves_role_arguments(self, mock_resolve: MagicMock) -> None:
        from utils.llm.ai_explainer import _collect_role_names, _format_role_name_notes

        mock_resolve.return_value = {self.MINTER_HASH: "RECEIPT_TOKEN_MINTER"}
        resolved = _collect_role_names([("0xCore", self.GRANT)], chain_id=1)

        self.assertEqual(resolved["0xcore"][self.MINTER_HASH], "RECEIPT_TOKEN_MINTER")
        self.assertIn("is the role RECEIPT_TOKEN_MINTER", "\n".join(_format_role_name_notes(resolved)))

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_ignores_bytes32_on_non_role_functions(self, mock_resolve: MagicMock) -> None:
        """A timelock's all-zero predecessor/salt must not become DEFAULT_ADMIN_ROLE."""
        from utils.llm.ai_explainer import _collect_role_names

        schedule = DecodedCall(
            function_name="scheduleBatch",
            signature="scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            params=[("bytes32", b"\x00" * 32), ("bytes32", b"\x00" * 32), ("uint256", 604800)],
        )
        self.assertEqual(_collect_role_names([("0xTimelock", schedule)], chain_id=1), {})
        mock_resolve.assert_not_called()

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_unresolved_roles_are_omitted(self, mock_resolve: MagicMock) -> None:
        from utils.llm.ai_explainer import _collect_role_names

        mock_resolve.return_value = {}
        self.assertEqual(_collect_role_names([("0xCore", self.GRANT)], chain_id=1), {})

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_resolution_failure_never_raises(self, mock_resolve: MagicMock) -> None:
        from utils.llm.ai_explainer import _collect_role_names

        mock_resolve.side_effect = RuntimeError("etherscan down")
        self.assertEqual(_collect_role_names([("0xCore", self.GRANT)], chain_id=1), {})


class TestUnpublishedReportSpill(unittest.TestCase):
    """A report that can't reach the gist is written to CACHE_DIR.

    It exists only in memory at that point, so without this the LLM output is
    lost and a recovery means regenerating it against a chain state that has
    since moved.
    """

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    def test_failed_upload_spills_report_to_disk(self, _mock_gist: MagicMock) -> None:
        import os

        from utils.cache import cache_path
        from utils.llm.ai_explainer import UNPUBLISHED_REPORTS_DIRNAME, Explanation, format_explanation_line

        explanation = Explanation(
            summary="Grants mint rights.",
            detail="Full detail here.",
            report="# Call flow\n\nEverything worth keeping.",
            title="InfiniFi LongTimelock - MEDIUM",
        )
        result = format_explanation_line(explanation)
        self.assertIn("Couldn't post full report", result)

        directory = cache_path(UNPUBLISHED_REPORTS_DIRNAME)
        spilled = os.listdir(directory)
        self.assertEqual(len(spilled), 1)
        contents = open(os.path.join(directory, spilled[0])).read()
        self.assertIn("Everything worth keeping.", contents)
        self.assertIn("InfiniFi LongTimelock - MEDIUM", contents)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_successful_upload_spills_nothing(self, _mock_gist: MagicMock) -> None:
        import os

        from utils.cache import cache_path
        from utils.llm.ai_explainer import UNPUBLISHED_REPORTS_DIRNAME, Explanation, format_explanation_line

        format_explanation_line(Explanation(summary="ok", detail="d", report="r"))
        self.assertFalse(os.path.exists(cache_path(UNPUBLISHED_REPORTS_DIRNAME)))

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    @patch("utils.llm.ai_explainer.os.makedirs", side_effect=OSError("read-only filesystem"))
    def test_spill_failure_still_returns_alert_line(self, _mock_mkdir: MagicMock, _mock_gist: MagicMock) -> None:
        """A failed spill must never take down the alert, which still has the summary."""
        from utils.llm.ai_explainer import Explanation, format_explanation_line

        result = format_explanation_line(Explanation(summary="Grants mint rights.", detail="d", report="r"))
        self.assertIn("Grants mint rights.", result)
        self.assertIn("Couldn't post full report", result)


PAUSE = DecodedCall(function_name="pause", signature="pause()")
UNKNOWN_DATA = "0xdeadbeef"
PAUSE_DATA = "0x8456cb59"


def _addr(i: int) -> str:
    return "0x" + f"{i:040x}"


def _set_cap(key: str, cap: int = 1) -> DecodedCall:
    return DecodedCall(
        function_name="setCap",
        signature="setCap(address,uint256)",
        params=[("address", key), ("uint256", cap)],
    )


class TestBatchUndecodedCalls(unittest.TestCase):
    """Unknown batch calls stay visible; all-unknown batches skip the LLM."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_unknown_middle_call_kept_in_prompt_and_report(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        def decode(data: str, chain_id: int | None = None, target: str | None = None) -> DecodedCall | None:
            return None if data == UNKNOWN_DATA else PAUSE

        mock_decode.side_effect = decode
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: mixed batch. LOW.\n\nDETAIL:\nanalysis."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": PAUSE_DATA, "value": "0"},
                {"target": "0xT2", "data": UNKNOWN_DATA, "value": "1000000000000000000"},
                {"target": "0xT3", "data": PAUSE_DATA, "value": "0"},
            ],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertIn("Call 1:", prompt)
        self.assertIn("Call 2: UNDECODED", prompt)
        self.assertIn("Call 3:", prompt)
        self.assertIn(UNKNOWN_DATA, prompt)
        self.assertIn("ETH value: 1.000000 ETH", prompt)
        self.assertIn("semantics: unresolved", prompt)
        self.assertIn("2. **Undecoded calldata**", result.report)
        self.assertIn("unknown_selector", result.report)
        self.assertEqual(provider.complete.call_count, 1)

    @patch("utils.llm.ai_explainer.fetch_function_input_names")
    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_param_names_stay_aligned_across_unknown_middle(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
        mock_names: MagicMock,
    ) -> None:
        """decoded / unknown / decoded must not shift ABI names onto the wrong call."""
        set_owner = DecodedCall(
            function_name="setOwner",
            signature="setOwner(address)",
            params=[("address", _addr(1))],
        )
        set_cap = DecodedCall(
            function_name="setCap",
            signature="setCap(address,uint256)",
            params=[("address", _addr(2)), ("uint256", 99)],
        )
        owner_data = "0xf2fde38b" + "00" * 32
        cap_data = "0xabcdef01" + "00" * 64

        def decode(data: str, chain_id: int | None = None, target: str | None = None) -> DecodedCall | None:
            if data == owner_data:
                return set_owner
            if data == cap_data:
                return set_cap
            return None

        mock_decode.side_effect = decode
        mock_names.side_effect = lambda _chain, _target, fname: {
            "setOwner": ["newOwner"],
            "setCap": ["asset", "cap"],
        }[fname]
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: mixed names. LOW.\n\nDETAIL:\nanalysis."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": owner_data, "value": "0"},
                {"target": "0xT2", "data": UNKNOWN_DATA, "value": "0"},
                {"target": "0xT3", "data": cap_data, "value": "0"},
            ],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        call3 = prompt[prompt.index("Call 3:") :]
        call2 = prompt[prompt.index("Call 2:") : prompt.index("Call 3:")]
        self.assertIn("address newOwner", prompt)
        self.assertIn("Call 2: UNDECODED", prompt)
        self.assertNotIn("newOwner", call2)
        self.assertNotIn("newOwner", call3)
        self.assertIn("address asset", call3)
        self.assertIn("uint256 cap", call3)
        self.assertIn("`address newOwner`", result.report)
        self.assertIn("`address asset`", result.report)
        self.assertIn("`uint256 cap`", result.report)
        # Names belong to call 1 and 3; call 2 must not inherit either set.
        flow_call2 = result.report[
            result.report.index("2. **Undecoded calldata**") : result.report.index("3. **`setCap")
        ]
        self.assertNotIn("newOwner", flow_call2)
        self.assertNotIn("asset", flow_call2)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=None)
    def test_all_unknown_is_deterministic_and_skips_llm(
        self,
        _mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": UNKNOWN_DATA, "value": "0"},
                {"target": "0xT2", "data": "0x", "value": "1"},
            ],
            chain_id=1,
            label="Test Timelock",
        )
        assert result is not None
        mock_get_provider.assert_called()
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertEqual(result.detail, "")
        self.assertIn("empty-calldata", result.summary)
        self.assertIn("undecoded", result.summary)
        self.assertNotIn("Could not decode 2 calls", result.summary)
        self.assertNotIn("LOW", result.summary)
        self.assertNotIn("MEDIUM", result.summary)
        self.assertNotIn("**Risk:**", result.report)
        self.assertNotIn(" LOW", result.title)
        self.assertIn("1. **Undecoded calldata**", result.report)
        self.assertIn("2. **Empty calldata**", result.report)
        self.assertNotIn("Native ETH transfer", result.report)
        self.assertEqual(mock_simulate.call_count, 1)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=None)
    def test_all_empty_calldata_does_not_claim_decode_failure(
        self,
        _mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": "0x", "value": "1"},
                {"target": "0xT2", "data": "0x", "value": "1"},
                {"target": "0xT3", "data": "0x", "value": "1"},
            ],
            chain_id=1,
            context_note="Executed via DELEGATECALL from the Safe.",
            description="payouts",
        )
        assert result is not None
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertIn("3 empty-calldata calls", result.summary)
        self.assertNotIn("Could not decode", result.summary)
        self.assertIn("## Execution Context", result.report)
        self.assertIn("DELEGATECALL", result.report)
        self.assertIn("## Stated Intent", result.report)
        self.assertIn("payouts", result.report)


class TestBatchSimulationsAttributed(unittest.TestCase):
    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_each_successful_sim_is_labeled_independent(
        self,
        _mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        mock_simulate.side_effect = [
            SimulationResult(success=True, gas_used=111),
            SimulationResult(success=False, gas_used=0, error_message="execution reverted: not authorized"),
            SimulationResult(success=True, gas_used=333),
        ]
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: three calls. LOW.\n\nDETAIL:\nanalysis."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": PAUSE_DATA, "value": "0"},
                {"target": "0xT2", "data": PAUSE_DATA, "value": "0"},
                {"target": "0xT3", "data": PAUSE_DATA, "value": "0"},
            ],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertIn("Call 1 (independent simulation", prompt)
        self.assertIn("Call 3 (independent simulation", prompt)
        self.assertNotIn("Call 2 (independent simulation", prompt)
        self.assertNotIn("FAILED", prompt)
        self.assertNotIn("execution reverted", prompt)
        self.assertEqual(mock_simulate.call_count, 3)
        self.assertIn("1. **`pause()`**", result.report)
        self.assertIn("not a predicted governance failure", result.report)


class TestMappingKeyStateReads(unittest.TestCase):
    @patch("utils.llm.ai_explainer.read_before_state")
    def test_distinct_keys_are_read_and_duplicates_deduped(self, mock_read: MagicMock) -> None:
        mock_read.side_effect = lambda _chain, _target, decoded: [
            __import__("utils.on_chain_state", fromlist=["StateRead"]).StateRead(
                var_name="cap",
                type_str="mapping(address => uint256)",
                value=1,
                key_args=_setter_key(decoded),
            )
        ]
        a, b = _addr(1), _addr(2)
        result = _collect_state_reads(
            [
                ("0xT", _set_cap(a, 10)),
                ("0xT", _set_cap(b, 20)),
                ("0xT", _set_cap(a, 99)),
            ],
            chain_id=1,
        )
        self.assertEqual(mock_read.call_count, 2)
        rendered = result.prompt_text()
        self.assertIn(a, rendered)
        self.assertIn(b, rendered)

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_single_arg_setters_share_one_scalar_read(self, mock_read: MagicMock) -> None:
        from utils.on_chain_state import StateRead

        mock_read.return_value = [StateRead(var_name="maxSlippage", type_str="uint256", value=10**18, key_args=())]

        def _set_slippage(value: int) -> DecodedCall:
            return DecodedCall(
                function_name="setMaxSlippage",
                signature="setMaxSlippage(uint256)",
                params=[("uint256", value)],
            )

        result = _collect_state_reads(
            [("0xT", _set_slippage(99 * 10**16)), ("0xT", _set_slippage(98 * 10**16))],
            chain_id=1,
        )
        self.assertEqual(mock_read.call_count, 1)
        rendered = result.prompt_text()
        self.assertIn("maxSlippage = 1000000000000000000", rendered)
        self.assertNotIn("maxSlippage(", rendered)

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_forty_keys_cap_at_twelve_and_report_omitted(self, mock_read: MagicMock) -> None:
        from utils.on_chain_state import StateRead

        mock_read.side_effect = lambda _chain, _target, decoded: [
            StateRead(
                var_name="cap",
                type_str="mapping(address => uint256)",
                value=1,
                key_args=(decoded.params[0][1],),
            )
        ]
        calls = [("0xT", _set_cap(_addr(i))) for i in range(1, 41)]
        result = _collect_state_reads(calls, chain_id=1)
        self.assertEqual(mock_read.call_count, MAX_STATE_READ_KEYS_PER_SIGNATURE)
        self.assertIn("28 additional mapping keys skipped", result.prompt_text())
        self.assertIn(f"(limit: {MAX_STATE_READ_KEYS_PER_SIGNATURE})", result.report_text())
        unavailable = [r for _, reads in result.by_target for r in reads if not r.available]
        self.assertEqual(len(unavailable), 28)
        self.assertTrue(all(r.var_name == "cap" for r in unavailable))
        md = result.report_text(chain_id=1)
        self.assertIn("- On", md)
        self.assertIn("_unavailable_", md)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.read_before_state")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_batch_retains_all_calls_when_keys_are_capped(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_read: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        from utils.on_chain_state import StateRead

        mock_decode.side_effect = [_set_cap(_addr(i)) for i in range(1, 41)]
        mock_read.side_effect = lambda _chain, _target, decoded: [
            StateRead(
                var_name="cap",
                type_str="mapping(address => uint256)",
                value=1,
                key_args=(decoded.params[0][1],),
            )
        ]
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: sets many caps. LOW.\n\nDETAIL:\nanalysis."
        provider.model_name = "test"
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            [{"target": "0xT", "data": f"0x{i:08x}", "value": "0"} for i in range(1, 41)],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        self.assertEqual(mock_read.call_count, MAX_STATE_READ_KEYS_PER_SIGNATURE)
        self.assertIn("1. **`setCap(address,uint256)`**", result.report)
        self.assertIn("40. **`setCap(address,uint256)`**", result.report)
        self.assertIn("28 additional mapping keys skipped", result.report)
        self.assertIn("28 additional mapping keys skipped", provider.complete.call_args[0][0])

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_overloads_have_separate_caps(self, mock_read: MagicMock) -> None:
        from utils.on_chain_state import StateRead

        mock_read.return_value = [StateRead(var_name="cap", type_str="uint256", value=1)]
        addr_calls = [("0xT", _set_cap(_addr(i))) for i in range(1, 14)]
        bytes_calls = [
            (
                "0xT",
                DecodedCall(
                    function_name="setCap",
                    signature="setCap(bytes32,uint256)",
                    params=[("bytes32", bytes([i]) * 32), ("uint256", 1)],
                ),
            )
            for i in range(1, 5)
        ]
        result = _collect_state_reads(addr_calls + bytes_calls, chain_id=1)
        self.assertEqual(mock_read.call_count, 12 + 4)
        self.assertIn("1 additional mapping key skipped for `setCap(address,uint256)`", result.prompt_text())

    @patch("utils.llm.ai_explainer.read_before_state", side_effect=RuntimeError("rpc down"))
    def test_worker_failure_marked_unavailable(self, mock_read: MagicMock) -> None:
        result = _collect_state_reads([("0xT", _set_cap(_addr(1)))], chain_id=1)
        reads = result.by_target[0][1]
        self.assertTrue(any(not r.available for r in reads))
        self.assertEqual(mock_read.call_count, 1)


def _setter_key(decoded: DecodedCall) -> tuple:
    return (decoded.params[0][1],)


class TestTextRefineDetailBudget(unittest.TestCase):
    def test_immediate_pass_keeps_detail_without_extra_call(self) -> None:
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.side_effect = [
            "TLDR: original. LOW.\n\nDETAIL:\nOriginal analysis.",
            "PASS",
        ]
        result = _generate_explanation(provider, "prompt", refine=True)
        self.assertEqual(provider.complete.call_count, 2)
        self.assertEqual(result.detail, "Original analysis.")
        self.assertIn("original. LOW", result.summary)

    def test_multiple_revisions_regenerate_detail_once(self) -> None:
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.side_effect = [
            "TLDR: original. LOW.\n\nDETAIL:\nOriginal analysis.",
            "TLDR: revised once. LOW.",
            "TLDR: revised twice. LOW.",
            "PASS",
            "Fresh detail from the final summary.",
        ]
        result = _generate_explanation(provider, "prompt", refine=True)
        self.assertEqual(provider.complete.call_count, 5)
        self.assertIn("revised twice. LOW", result.summary)
        self.assertEqual(result.detail, "Fresh detail from the final summary.")
        self.assertNotIn("Original analysis", result.detail)

    def test_expansion_failure_keeps_revised_summary_discards_stale_detail(self) -> None:
        from utils.llm.base import LLMError

        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.side_effect = [
            "TLDR: original. LOW.\n\nDETAIL:\nOriginal analysis.",
            "TLDR: revised. LOW.",
            "PASS",
            LLMError("boom"),
        ]
        result = _generate_explanation(provider, "prompt", refine=True)
        self.assertIn("revised. LOW", result.summary)
        self.assertEqual(result.detail, "")


class TestPromptSizeGuards(unittest.TestCase):
    """Batch prompts stay bounded, and quantities keep one unit throughout."""

    @staticmethod
    def _provider() -> MagicMock:
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: big batch. LOW.\n\nDETAIL:\nanalysis."
        provider.model_name = "test"
        return provider

    @staticmethod
    def _calldata_section(prompt: str) -> str:
        return prompt.split("--- Decoded Calldata ---", 1)[1].split("\n---", 1)[0]

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_successful_simulations_are_capped(
        self,
        _mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Every sim used to be rendered; a 30-call batch could overflow the context."""
        total = MAX_PROMPT_SIMULATIONS + 2
        mock_simulate.side_effect = [SimulationResult(success=True, gas_used=100 + i) for i in range(total)]
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[{"target": _addr(i), "data": PAUSE_DATA, "value": "0"} for i in range(total)],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertEqual(prompt.count("(independent simulation;"), MAX_PROMPT_SIMULATIONS)
        self.assertIn("2 further successful independent simulations omitted", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_long_undecoded_payload_is_truncated(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Undecoded payloads are dumped verbatim, so they need their own cap."""
        long_data = "0x" + "ab" * 400
        mock_decode.side_effect = lambda data, chain_id=None, target=None: None if data == long_data else PAUSE
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": PAUSE_DATA, "value": "0"},
                {"target": "0xT2", "data": long_data, "value": "0"},
            ],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertNotIn(long_data, prompt)
        self.assertIn(long_data[:MAX_PROMPT_CALLDATA_CHARS], prompt)
        self.assertIn(f"({len(long_data)} chars, truncated)", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_native_values_use_eth_for_decoded_and_undecoded_alike(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Handing the model wei and ETH in one section invites decimal confusion."""
        mock_decode.side_effect = lambda data, chain_id=None, target=None: PAUSE if data == PAUSE_DATA else None
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": PAUSE_DATA, "value": "1000000000000000000"},
                {"target": "0xT2", "data": UNKNOWN_DATA, "value": "2000000000000000000"},
                {"target": "0xT3", "data": "0x", "value": "3000000000000000000"},
            ],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        section = self._calldata_section(provider.complete.call_args[0][0])
        self.assertEqual(section.count("ETH value:"), 3)
        for eth in ("1.000000 ETH", "2.000000 ETH", "3.000000 ETH"):
            self.assertIn(eth, section)
        self.assertNotIn(" wei", section)
        self.assertNotIn("1000000000000000000", section)
