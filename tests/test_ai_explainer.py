"""Tests for utils/ai_explainer.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm.ai_explainer import (
    Explanation,
    _build_prompt,
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
        self.assertIn("DeFi risk analyst", result)

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
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None)
        self.assertIn("Do NOT assume the semantic meaning", result)
        self.assertIn("source context", result.lower())

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
        mock_provider.complete.return_value = "TLDR: Tight slippage."
        mock_provider.model_name = "test-model"
        mock_get_provider.return_value = mock_provider

        explain_transaction(target="0xTarget", calldata="0x12345678" + "00" * 32, chain_id=1)

        prompt = mock_provider.complete.call_args[0][0]
        self.assertIn("Contract Source Context", prompt)
        self.assertIn("so actually 1 - slippage", prompt)


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
        provider.complete.return_value = "  PASS  \n"
        self.assertIs(_refine_explanation("orig prompt", draft, provider), draft)
        provider.complete.assert_called_once()

    def test_revision_replaces_draft(self) -> None:
        draft = Explanation(summary="This transaction does X. LOW.", detail="bla")
        provider = MagicMock()
        provider.complete.return_value = "TLDR: Does X. LOW.\n\nDETAIL:\nrefined detail."
        result = _refine_explanation("orig", draft, provider)
        self.assertEqual(result.summary, "Does X. LOW.")
        self.assertEqual(result.detail, "refined detail.")

    def test_llm_error_falls_back_to_draft(self) -> None:
        draft = Explanation(summary="x", detail="y")
        provider = MagicMock()
        provider.complete.side_effect = LLMError("rate limit")
        self.assertIs(_refine_explanation("p", draft, provider), draft)

    def test_empty_response_falls_back_to_draft(self) -> None:
        draft = Explanation(summary="x", detail="y")
        provider = MagicMock()
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
    def test_address_array_arg_is_labeled(
        self,
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
    def test_scalar_address_arg_is_labeled(
        self,
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
    def test_target_address_is_not_relabeled(
        self,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        # The target appears as its own argument — should be skipped so we don't
        # double up with the Contract Source Context block.
        mock_decode.return_value = DecodedCall(
            function_name="selfWire",
            signature="selfWire(address)",
            params=[("address", self.REGISTRY.lower())],
        )
        provider = MagicMock()
        provider.complete.return_value = "TLDR: wires self. LOW."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xdeadbeef" + "00" * 32, chain_id=1)

        mock_label.assert_not_called()

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    @patch("utils.llm.ai_explainer.get_contract_label")
    def test_unverified_address_left_unannotated(
        self,
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
    def test_zero_address_not_queried(
        self,
        mock_label: MagicMock,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_source: MagicMock,
    ) -> None:
        zero = "0x" + "00" * 20
        mock_decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", zero)],
        )
        provider = MagicMock()
        provider.complete.return_value = "TLDR: unsets oracle. LOW."
        provider.model_name = "test-model"
        mock_get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)
        mock_label.assert_not_called()


if __name__ == "__main__":
    unittest.main()
