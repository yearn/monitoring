"""Tests for explainer transaction."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm.ai_explainer import (
    explain_batch_transaction,
    explain_transaction,
)
from utils.llm.base import LLMError
from utils.tenderly.simulation import SimulationResult

from .helpers import make_provider


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
        # The summary carries every fact, so no gist is published for it.
        self.assertEqual(result.report, "")
        self.assertEqual(result.title, "")
        self.assertIn("Empty calldata", result.summary)
        self.assertIn("no function selector", result.summary)
        self.assertIn(self.TARGET, result.summary)
        self.assertIn("1.000000 ETH", result.summary)
        self.assertNotIn("Native ETH transfer", result.summary)
        self.assertNotIn("delivered", result.summary.lower())

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
        self.assertEqual(result.report, "")
        self.assertIn("Could not decode", result.summary)
        self.assertIn("selector 0x1234", result.summary)
        self.assertIn(self.TARGET, result.summary)
        self.assertNotIn("empty calldata", result.summary)
        self.assertNotIn("Native ETH transfer", result.summary)

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
        self.assertEqual(result.report, "")
        self.assertIn("Could not decode", result.summary)
        self.assertIn("selector 0x11223344", result.summary)
        self.assertIn(self.TARGET, result.summary)

    @patch("utils.llm.ai_explainer.get_llm_provider", side_effect=LLMError("LLM_API_KEY is not set"))
    def test_unconfigured_llm_skips_deterministic_path(self, _mock_get_provider: MagicMock) -> None:
        self.assertIsNone(explain_transaction(target=self.TARGET, calldata="0x", chain_id=1, value=10**18))


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
        provider = make_provider("TLDR: pauses. LOW.")
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x8456cb59", chain_id=1)
        prompt = provider.complete.call_args[0][0]

        self.assertNotIn("--- Simulation Results ---", prompt)
        self.assertNotIn("FAILED", prompt)
        self.assertNotIn("execution reverted", prompt)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_bundle")
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_failed_sim_omitted_from_batch_prompt(
        self,
        mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_label: MagicMock,
        mock_source: MagicMock,
    ) -> None:

        mock_decode.return_value = DecodedCall(function_name="pause", signature="pause()")
        mock_simulate.return_value = [SimulationResult(success=False, gas_used=0, error_message="reverted"), None]
        provider = make_provider("TLDR: pauses both. LOW.")
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
        provider = make_provider("TLDR: tightens slippage. LOW.")
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
        provider = make_provider("TLDR: tightens slippage. LOW.")
        mock_get_provider.return_value = provider

        explain_transaction(target="0xT", calldata="0x736defe0" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        # Without ABI names, params render as plain `type: value`.
        decoded_section = prompt.split("--- Decoded Calldata ---")[1]
        self.assertIn("uint256: 1", decoded_section)


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
        provider = make_provider("TLDR: hands ownership. HIGH.")
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
        provider = make_provider("TLDR: enables a Safe module. CRITICAL.")
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
        provider = make_provider("TLDR: tightens slippage. LOW.")
        mock_get_provider.return_value = provider

        explain_transaction(target="0x" + "ff" * 20, calldata="0x736defe0" + "00" * 32, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        self.assertNotIn("--- Risk Anchors ---", prompt)


class TestNestedProxyUpgradeInfo(unittest.TestCase):
    """A Safe tx calling a timelock's executeBatch still gets each upgrade's impl diff."""

    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    @patch("utils.llm.ai_explainer.format_impl_diff", side_effect=lambda diff: f"IMPL DIFF {diff}")
    @patch("utils.llm.ai_explainer.diff_implementations", side_effect=lambda old, new, chain_id: f"{old}->{new}")
    @patch("utils.llm.ai_explainer.get_current_implementation")
    def test_each_nested_upgrade_is_diffed(self, current_impl, _diff, _fmt, _verified) -> None:
        from tests.test_calldata_wrappers import (
            CUSD,
            NEW_CUSD_IMPL,
            NEW_ORACLE_IMPL,
            ORACLE,
            execute_batch,
            upgrade_to_and_call,
        )
        from utils.llm.ai_explainer import _get_proxy_upgrade_info

        old_impls = {ORACLE: "0xOldOracle", CUSD: "0xOldCusd"}
        current_impl.side_effect = lambda proxy, chain_id: old_impls[proxy]
        data = execute_batch([ORACLE, CUSD], [upgrade_to_and_call(NEW_ORACLE_IMPL), upgrade_to_and_call(NEW_CUSD_IMPL)])

        info = _get_proxy_upgrade_info(data, "0xD8236031d8279d82E615aF2BFab5FC0127A329ab", 1)

        self.assertEqual(info.count("This is a PROXY UPGRADE on"), 2)
        self.assertIn(f"PROXY UPGRADE on {ORACLE}. It is nested inside", info)
        self.assertIn("(executeBatch call 1)", info)
        self.assertIn("(executeBatch call 2)", info)
        self.assertIn(f"IMPL DIFF 0xOldOracle->{NEW_ORACLE_IMPL}", info)
        self.assertIn(f"IMPL DIFF 0xOldCusd->{NEW_CUSD_IMPL}", info)
        # Call order is kept even though diffs are fetched in parallel.
        self.assertLess(info.index(ORACLE), info.index(CUSD))

    def test_non_upgrade_wrapper_adds_nothing(self) -> None:
        from tests.test_calldata_wrappers import CUSD, execute_batch
        from utils.llm.ai_explainer import _get_proxy_upgrade_info

        transfer = "0xa9059cbb" + "00" * 64
        self.assertEqual(_get_proxy_upgrade_info(execute_batch([CUSD], [transfer]), CUSD, 1), "")
