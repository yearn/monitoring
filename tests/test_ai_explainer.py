"""Tests for utils/ai_explainer.py."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm.ai_explainer import (
    Explanation,
    _build_prompt,
    _format_decoded_calls,
    _format_simulation_context,
    _parse_explanation,
    explain_transaction,
    format_explanation_line,
)
from utils.llm.base import LLMError
from utils.source_context import SourceContext
from utils.tenderly.simulation import AssetChange, SimulationResult, StateChange


class TestFormatDecodedCalls(unittest.TestCase):
    """Tests for _format_decoded_calls."""

    def test_single_call_no_params(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _format_decoded_calls(calls)
        self.assertIn("Call 1: pause()", result)

    def test_single_call_with_params(self) -> None:
        calls = [
            DecodedCall(
                function_name="grantRole",
                signature="grantRole(bytes32,address)",
                params=[("bytes32", b"\x00" * 32), ("address", "0xABC")],
            )
        ]
        result = _format_decoded_calls(calls)
        self.assertIn("grantRole(bytes32,address)", result)
        self.assertIn("bytes32:", result)
        self.assertIn("address:", result)

    def test_multiple_calls(self) -> None:
        calls = [
            DecodedCall(function_name="pause", signature="pause()"),
            DecodedCall(function_name="unpause", signature="unpause()"),
        ]
        result = _format_decoded_calls(calls)
        self.assertIn("Call 1: pause()", result)
        self.assertIn("Call 2: unpause()", result)


class TestFormatSimulationContext(unittest.TestCase):
    """Tests for _format_simulation_context."""

    def test_successful_simulation(self) -> None:
        sim = SimulationResult(success=True, gas_used=50000)
        result = _format_simulation_context(sim)
        self.assertIn("SUCCESS", result)
        self.assertIn("50,000", result)

    def test_failed_simulation(self) -> None:
        sim = SimulationResult(success=False, gas_used=21000, error_message="execution reverted")
        result = _format_simulation_context(sim)
        self.assertIn("FAILED", result)
        self.assertIn("execution reverted", result)

    def test_with_asset_changes(self) -> None:
        sim = SimulationResult(
            success=True,
            gas_used=100000,
            asset_changes=[
                AssetChange(
                    token_address="0xToken",
                    token_name="USDC",
                    token_symbol="USDC",
                    from_address="0xA",
                    to_address="0xB",
                    amount="1000",
                    raw_amount="1000000000",
                    decimals=6,
                )
            ],
        )
        result = _format_simulation_context(sim)
        self.assertIn("Token transfers:", result)
        self.assertIn("USDC", result)

    def test_with_state_changes(self) -> None:
        sim = SimulationResult(
            success=True,
            gas_used=100000,
            state_changes=[
                StateChange(
                    contract_address="0xContract",
                    key="0x01",
                    original="0x00",
                    dirty="0x01",
                )
            ],
        )
        result = _format_simulation_context(sim)
        self.assertIn("State changes", result)
        self.assertIn("0xContract", result)

    def test_with_logs(self) -> None:
        sim = SimulationResult(
            success=True,
            gas_used=100000,
            logs=[{"name": "Transfer", "inputs": [{"soltype": {"name": "to"}, "value": "0xB"}]}],
        )
        result = _format_simulation_context(sim)
        self.assertIn("Events emitted", result)
        self.assertIn("Transfer", result)


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


class TestSystemPromptBrevity(unittest.TestCase):
    """Verify the system prompt enforces brevity rules."""

    def test_includes_word_cap_and_no_preamble_rules(self) -> None:
        calls = [DecodedCall(function_name="pause", signature="pause()")]
        result = _build_prompt(target="0xT", value=0, decoded_calls=calls, simulation=None)
        self.assertIn("≤25 words", result)
        self.assertIn('"This transaction"', result)
        self.assertIn("risk tag in caps", result)


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


if __name__ == "__main__":
    unittest.main()
