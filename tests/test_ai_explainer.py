"""Tests for utils/ai_explainer.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm.ai_explainer import (
    SYSTEM_INSTRUCTIONS,
    Explanation,
    _build_prompt,
    _collect_safety_checks,
    _explanation_from_json,
    _generate_draft,
    _parse_explanation,
    _refine_explanation,
    explain_transaction,
    format_explanation_line,
)
from utils.llm.base import LLMError
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


class TestSkipSimulation(unittest.TestCase):
    """Tests for skip_simulation flag."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_explain_transaction_skips_tenderly_when_flag_set(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_provider = MagicMock()
        mock_provider.supports_structured_output = False
        mock_provider.complete.return_value = "TLDR: paused"
        mock_provider.model_name = "test-model"
        mock_get_provider.return_value = mock_provider

        explain_transaction(
            target="0xT",
            calldata="0x8456cb59",
            chain_id=1,
            skip_simulation=True,
            context_note="delegated",
        )

        mock_simulate.assert_not_called()
        prompt = mock_provider.complete.call_args[0][0]
        self.assertIn("--- Execution Context ---", prompt)
        self.assertIn("delegated", prompt)
        self.assertNotIn("--- Simulation Results ---", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_explain_batch_transaction_skips_tenderly_when_flag_set(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(
            function_name="swapOwner", signature="swapOwner(address,address,address)"
        )
        mock_provider = MagicMock()
        mock_provider.supports_structured_output = False
        mock_provider.complete.return_value = "TLDR: swap"
        mock_provider.model_name = "test-model"
        mock_get_provider.return_value = mock_provider

        from utils.llm.ai_explainer import explain_batch_transaction

        explain_batch_transaction(
            calls=[
                {"target": "0xSafe", "data": "0xe318b52b" + "00" * 96, "value": "0"},
                {"target": "0xSafe", "data": "0xe318b52b" + "11" * 96, "value": "0"},
            ],
            chain_id=1,
            skip_simulation=True,
            context_note="delegated batch",
        )

        mock_simulate.assert_not_called()
        prompt = mock_provider.complete.call_args[0][0]
        self.assertIn("delegated batch", prompt)
        self.assertNotIn("--- Simulation Results ---", prompt)


class TestExplainTransaction(unittest.TestCase):
    """Tests for explain_transaction."""

    def test_empty_calldata_returns_none(self) -> None:
        result = explain_transaction(target="0xTarget", calldata="0x", chain_id=1)
        self.assertIsNone(result)

    def test_short_calldata_returns_none(self) -> None:
        result = explain_transaction(target="0xTarget", calldata="0x1234", chain_id=1)
        self.assertIsNone(result)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_successful_explanation(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_simulate.return_value = SimulationResult(success=True, gas_used=50000)
        mock_provider = MagicMock()
        mock_provider.supports_structured_output = False
        mock_provider.complete.return_value = "TLDR: This pauses the protocol.\n\nDETAIL:\nPauses all operations."
        mock_provider.model_name = "test-model"
        mock_get_provider.return_value = mock_provider

        result = explain_transaction(
            target="0xTarget",
            calldata="0x8456cb59",  # pause()
            chain_id=1,
            protocol="AAVE",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.summary, "This pauses the protocol.")
        self.assertEqual(result.detail, "Pauses all operations.")
        mock_simulate.assert_called_once()
        mock_provider.complete.assert_called_once()

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_llm_error_returns_none(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_simulate.return_value = None
        mock_provider = MagicMock()
        mock_provider.supports_structured_output = False
        mock_provider.complete.side_effect = LLMError("API error")
        mock_get_provider.return_value = mock_provider

        result = explain_transaction(target="0xTarget", calldata="0x8456cb59", chain_id=1)
        self.assertIsNone(result)

    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_undecoded_calldata_returns_none(self, mock_decode: MagicMock) -> None:
        mock_decode.return_value = None
        result = explain_transaction(target="0xTarget", calldata="0x11223344", chain_id=1)
        self.assertIsNone(result)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_simulation_failure_still_explains(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        """If simulation fails, should still explain using decoded calldata only."""
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_simulate.return_value = None  # Simulation failed
        mock_provider = MagicMock()
        mock_provider.supports_structured_output = False
        mock_provider.complete.return_value = "TLDR: This pauses the protocol."
        mock_provider.model_name = "test-model"
        mock_get_provider.return_value = mock_provider

        result = explain_transaction(target="0xTarget", calldata="0x8456cb59", chain_id=1)
        self.assertIsNotNone(result)
        self.assertEqual(result.summary, "This pauses the protocol.")

    @patch("utils.llm.ai_explainer.get_source_context")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_source_context_passed_to_llm(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        """When source context is available, it should be injected into the prompt."""
        mock_decode.return_value = DecodedCall(function_name="setMaxSlippage", signature="setMaxSlippage(uint256)")
        mock_simulate.return_value = SimulationResult(success=True, gas_used=50000)
        mock_source.return_value = SourceContext(
            contract_name="Farm",
            function_snippet="function setMaxSlippage(uint256) external;",
            state_var_snippets=["/// @dev so actually 1 - slippage\nuint256 public maxSlippage;"],
        )
        mock_provider = MagicMock()
        mock_provider.supports_structured_output = False
        mock_provider.complete.return_value = "TLDR: Tight slippage."
        mock_provider.model_name = "test-model"
        mock_get_provider.return_value = mock_provider

        explain_transaction(target="0xTarget", calldata="0x12345678" + "00" * 32, chain_id=1)

        prompt = mock_provider.complete.call_args[0][0]
        self.assertIn("Contract Source Context", prompt)
        self.assertIn("so actually 1 - slippage", prompt)


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

    def test_generate_draft_uses_structured_when_supported(self) -> None:
        provider = MagicMock()
        provider.supports_structured_output = True
        provider.complete_structured.return_value = {"summary": "Does X", "detail": "d", "risk_tag": "HIGH"}

        result = _generate_draft(provider, "prompt")

        provider.complete_structured.assert_called_once()
        provider.complete.assert_not_called()
        self.assertEqual(result.summary, "Does X HIGH")

    def test_generate_draft_falls_back_to_text_on_error(self) -> None:
        provider = MagicMock()
        provider.supports_structured_output = True
        provider.complete_structured.side_effect = LLMError("unsupported")
        provider.complete.return_value = "TLDR: Does X. LOW."

        result = _generate_draft(provider, "prompt")

        provider.complete.assert_called_once()
        self.assertEqual(result.summary, "Does X. LOW.")

    def test_generate_draft_falls_back_on_empty_summary(self) -> None:
        provider = MagicMock()
        provider.supports_structured_output = True
        provider.complete_structured.return_value = {"summary": "", "detail": "", "risk_tag": "LOW"}
        provider.complete.return_value = "TLDR: text path. LOW."

        result = _generate_draft(provider, "prompt")

        provider.complete.assert_called_once()
        self.assertEqual(result.summary, "text path. LOW.")


class TestParseExplanation(unittest.TestCase):
    """Tests for _parse_explanation."""

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


class TestFormatExplanationLine(unittest.TestCase):
    """Tests for format_explanation_line."""

    @patch("utils.llm.ai_explainer.upload_to_paste", return_value="https://dpaste.com/abc123")
    def test_format_with_detail(self, mock_paste: MagicMock) -> None:
        explanation = Explanation(summary="This pauses the protocol.", detail="Full detail here.")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full detail here.", result)
        self.assertIn("https://dpaste.com/abc123", result)
        self.assertIn("Full details", result)
        mock_paste.assert_called_once_with("Full detail here.", title="AI Transaction Analysis")

    @patch("utils.llm.ai_explainer.upload_to_paste", return_value="")
    def test_format_paste_failure(self, mock_paste: MagicMock) -> None:
        """If paste upload fails, no link is included."""
        explanation = Explanation(summary="This pauses the protocol.", detail="Full detail here.")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full details", result)

    def test_format_no_detail(self) -> None:
        """If there's no detail, no paste upload is attempted."""
        explanation = Explanation(summary="This pauses the protocol.", detail="")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full details", result)


class TestRefineExplanation(unittest.TestCase):
    """Tests for _refine_explanation."""

    def test_pass_keeps_draft_unchanged(self) -> None:
        # Trailing whitespace around "PASS" must also count as PASS.
        draft = Explanation(summary="Lowers fee 30→25 bps. LOW.", detail="bla")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "  PASS  \n"
        self.assertIs(_refine_explanation("orig prompt", draft, provider), draft)
        provider.complete.assert_called_once()

    def test_revision_replaces_draft(self) -> None:
        draft = Explanation(summary="This transaction does X. LOW.", detail="bla")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: Does X. LOW.\n\nDETAIL:\nrefined detail."
        result = _refine_explanation("orig", draft, provider)
        self.assertEqual(result.summary, "Does X. LOW.")
        self.assertEqual(result.detail, "refined detail.")

    def test_llm_error_falls_back_to_draft(self) -> None:
        draft = Explanation(summary="x", detail="y")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.side_effect = LLMError("rate limit")
        self.assertIs(_refine_explanation("p", draft, provider), draft)

    def test_empty_response_falls_back_to_draft(self) -> None:
        draft = Explanation(summary="x", detail="y")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = ""
        self.assertIs(_refine_explanation("p", draft, provider), draft)


class TestRefineFlagInExplainTransaction(unittest.TestCase):
    """Tests that the refine flag triggers a second LLM call."""

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer._collect_state_reads", return_value=[])
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_refine_off_makes_one_call(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_state: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.return_value = "TLDR: Pauses. LOW."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x8456cb59", chain_id=1)
        self.assertEqual(provider.complete.call_count, 1)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer._collect_state_reads", return_value=[])
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_refine_on_makes_two_calls(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_state: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        provider = MagicMock()
        provider.supports_structured_output = False
        provider.complete.side_effect = ["TLDR: Pauses. LOW.", "PASS"]
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x8456cb59", chain_id=1, refine=True)
        self.assertEqual(provider.complete.call_count, 2)
        # Second call should contain the critique task
        second_call_prompt = provider.complete.call_args_list[1][0][0]
        self.assertIn("Critique Task", second_call_prompt)
        self.assertIn("Your Previous Draft", second_call_prompt)


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
        with patch("utils.llm.ai_explainer.decode_calldata", return_value=None):
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
        with patch("utils.llm.ai_explainer.decode_calldata") as mock_decode:
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
        with patch("utils.llm.ai_explainer.decode_calldata") as mock_decode:
            result = _format_decoded_calls([outer])
            mock_decode.assert_not_called()
        self.assertIn(sigs_blob, result)

    def test_recursion_depth_capped(self) -> None:
        from utils.llm.ai_explainer import _MAX_BYTES_RECURSION_DEPTH, _format_decoded_calls

        # Mock _try_decode_inner_bytes so it always returns a self-referential
        # call, bypassing the selector/alignment guard. Without the depth cap
        # this would recurse forever.
        self_referential = DecodedCall(
            function_name="wrap",
            signature="wrap(bytes)",
            params=[("bytes", "0xfeedfacefeedfacefeedfacefeedfacefeedface")],
        )
        with patch("utils.llm.ai_explainer._try_decode_inner_bytes", return_value=self_referential):
            result = _format_decoded_calls([self_referential])
        self.assertEqual(result.count("↳"), _MAX_BYTES_RECURSION_DEPTH)


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


if __name__ == "__main__":
    unittest.main()
