"""Tests for explainer batch."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm.ai_explainer import (
    MAX_INLINE_UNDECODED_CALLS,
    explain_batch_transaction,
    explain_transaction,
)
from utils.tenderly.simulation import SimulationResult

from .helpers import PAUSE, PAUSE_DATA, UNKNOWN_DATA, make_address, make_provider


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
        provider = make_provider("TLDR: mixed batch. LOW.\n\nDETAIL:\nanalysis.")
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
            params=[("address", make_address(1))],
        )
        set_cap = DecodedCall(
            function_name="setCap",
            signature="setCap(address,uint256)",
            params=[("address", make_address(2)), ("uint256", 99)],
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
        mock_names.side_effect = lambda _chain, _target, fname, _signature=None: {
            "setOwner": ["newOwner"],
            "setCap": ["asset", "cap"],
        }[fname]
        provider = make_provider("TLDR: mixed names. LOW.\n\nDETAIL:\nanalysis.")
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
    @patch("utils.llm.ai_explainer.simulate_bundle", return_value=None)
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
        # Two calls fit inline, so the alert is self-contained: no gist, and both
        # entries keep their original index, target and value.
        self.assertEqual(result.report, "")
        self.assertEqual(result.title, "")
        self.assertIn("1. 0xT1 (selector 0xdeadbeef)", result.summary)
        self.assertIn("2. 0xT2 (empty calldata, 0.000000 ETH)", result.summary)
        self.assertNotIn("Native ETH transfer", result.summary)
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

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=None)
    def test_batch_over_inline_cap_still_publishes_a_report(
        self,
        _mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Too many entries to inline: the facts must survive in a linked report."""
        total = MAX_INLINE_UNDECODED_CALLS + 2
        result = explain_batch_transaction(
            calls=[{"target": make_address(i), "data": "0x", "value": "0"} for i in range(total)],
            chain_id=1,
            label="Test Timelock",
        )
        assert result is not None
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertIn("See the linked report", result.summary)
        self.assertNotEqual(result.report, "")
        self.assertIn(f"{total}. **Empty calldata**", result.report)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=None)
    def test_proposer_description_keeps_the_report_for_a_small_batch(
        self,
        _mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Stated intent cannot fit in the summary, so it must not be dropped."""
        result = explain_batch_transaction(
            calls=[{"target": "0xT1", "data": "0x", "value": "0"}],
            chain_id=1,
            description="top up the payer",
        )
        assert result is not None
        mock_get_provider.return_value.complete.assert_not_called()
        self.assertIn("## Stated Intent", result.report)
        self.assertIn("top up the payer", result.report)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_bytes_params_reach_the_prompt_as_hex_not_python_repr(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """eth_abi returns bytes for bytes32; the model should not parse Python escapes."""
        root = bytes.fromhex("d6e32aa8b4cae01447b55ec0f497f92b3b27bc6f36185c06e3229e7743db2062")
        mock_decode.return_value = DecodedCall(
            function_name="updateRoot", signature="updateRoot(bytes32)", params=[("bytes32", root)]
        )
        provider = make_provider("TLDR: root rotated. LOW.\n\nDETAIL:\nanalysis.")
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x21ff9970" + root.hex(), chain_id=1)
        prompt = provider.complete.call_args[0][0]
        self.assertIn(f"0x{root.hex()}", prompt)
        self.assertNotIn("\\x", prompt)
        self.assertNotIn("b'", prompt)


class TestBatchSequentialSimulation(unittest.TestCase):
    """Batch calls are simulated in order on shared state, as the timelock executes them."""

    CALLS = [
        {"target": "0xT1", "data": PAUSE_DATA, "value": "0"},
        {"target": "0xT2", "data": PAUSE_DATA, "value": "0"},
        {"target": "0xT3", "data": PAUSE_DATA, "value": "0"},
    ]

    def _provider(self) -> MagicMock:
        provider = make_provider("TLDR: three calls. LOW.\n\nDETAIL:\nanalysis.")
        return provider

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.simulate_bundle")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_bundle_results_are_labeled_batch_order(
        self,
        _mock_decode: MagicMock,
        mock_bundle: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        mock_bundle.return_value = [
            SimulationResult(success=True, gas_used=111),
            SimulationResult(success=True, gas_used=222),
            SimulationResult(success=True, gas_used=333),
        ]
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(calls=self.CALLS, chain_id=1, from_address="0xTimelock", refine=False)

        assert result is not None
        bundle_calls = mock_bundle.call_args.args[0]
        self.assertEqual([call.target for call in bundle_calls], ["0xT1", "0xT2", "0xT3"])
        self.assertEqual(mock_bundle.call_args.kwargs["from_address"], "0xTimelock")
        mock_simulate.assert_not_called()
        prompt = provider.complete.call_args[0][0]
        self.assertIn("Call 1 (simulated in batch order, first call):", prompt)
        self.assertIn("Call 3 (simulated in batch order, after calls 1-2):", prompt)
        self.assertNotIn("independent simulation", prompt)
        self.assertIn("**Batch simulation:** SUCCESS, gas 333", result.report)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.simulate_bundle")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_calls_after_a_bundle_revert_are_not_reached(
        self,
        _mock_decode: MagicMock,
        mock_bundle: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """A revert stops the batch; later calls must not be re-simulated out of order."""
        mock_bundle.return_value = [
            SimulationResult(success=True, gas_used=111),
            SimulationResult(success=False, error_message="execution reverted: not authorized"),
            None,
        ]
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(calls=self.CALLS, chain_id=1, refine=False)

        assert result is not None
        mock_simulate.assert_not_called()
        prompt = provider.complete.call_args[0][0]
        self.assertIn("Call 1 (simulated in batch order, first call):", prompt)
        self.assertNotIn("independent simulation", prompt)
        self.assertNotIn("not authorized", prompt)
        self.assertIn("**Batch simulation diagnostic:** execution reverted: not authorized", result.report)
        self.assertIn("**Batch simulation:** not reached — call 2 reverted first in batch order", result.report)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.simulate_bundle", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_unavailable_bundle_does_not_fall_back_to_single_calls(
        self,
        _mock_decode: MagicMock,
        _mock_bundle: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """One-by-one sims falsely reverted dependent calls; a failed bundle leaves the batch unsimulated."""
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(calls=self.CALLS, chain_id=1, refine=False)

        assert result is not None
        mock_simulate.assert_not_called()
        prompt = provider.complete.call_args[0][0]
        self.assertIn("Batch simulation unavailable", prompt)
        self.assertNotIn("independent simulation", prompt)
        self.assertEqual(result.report.count("**Batch simulation:** unavailable"), 3)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction")
    @patch("utils.llm.ai_explainer.simulate_bundle")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_skip_simulation_skips_the_bundle(
        self,
        _mock_decode: MagicMock,
        mock_bundle: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        mock_get_provider.return_value = self._provider()
        explain_batch_transaction(calls=self.CALLS, chain_id=1, skip_simulation=True, refine=False)
        mock_bundle.assert_not_called()
        mock_simulate.assert_not_called()
