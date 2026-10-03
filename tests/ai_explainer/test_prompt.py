"""Tests for explainer prompt."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm.ai_explainer import (
    MAX_PROMPT_CALLDATA_CHARS,
    MAX_PROMPT_SIMULATIONS,
    SYSTEM_INSTRUCTIONS,
    _build_prompt,
    _sole_token_by_target,
    explain_batch_transaction,
    explain_transaction,
)
from utils.related_tokens import RelatedToken
from utils.source_context import SourceContext
from utils.tenderly.simulation import AssetChange, SimulationResult

from .helpers import PAUSE, PAUSE_DATA, UNKNOWN_DATA, _addr, make_provider


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

        provider = make_provider("TLDR: tiny transfer. LOW.")
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
        provider = make_provider("TLDR: registers farm. LOW.")
        mock_get_provider.return_value = provider

        explain_transaction(target="0x" + "ff" * 20, calldata="0xabcdef10" + "00" * 64, chain_id=1)
        prompt = provider.complete.call_args[0][0]
        # FarmRegistry label without ERC20 decoration.
        self.assertIn("FarmRegistry", prompt)
        self.assertNotIn("dec)", prompt)


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


class TestPromptSizeGuards(unittest.TestCase):
    """Batch prompts stay bounded, and quantities keep one unit throughout."""

    @staticmethod
    def _provider() -> MagicMock:
        provider = make_provider("TLDR: big batch. LOW.\n\nDETAIL:\nanalysis.")
        return provider

    @staticmethod
    def _calldata_section(prompt: str) -> str:
        return prompt.split("--- Decoded Calldata ---", 1)[1].split("\n---", 1)[0]

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_bundle")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_successful_simulations_are_capped(
        self,
        _mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Every sim used to be rendered in full; a 30-call batch could overflow the context."""
        total = MAX_PROMPT_SIMULATIONS + 2
        mock_simulate.return_value = [SimulationResult(success=True, gas_used=100 + i) for i in range(total)]
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[{"target": _addr(i), "data": PAUSE_DATA, "value": "0"} for i in range(total)],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertEqual(prompt.count("(simulated in batch order"), MAX_PROMPT_SIMULATIONS)
        self.assertEqual(prompt.count("token transfers only):"), 2)
        self.assertIn("2 further successful simulations are shown as token transfers only", prompt)

    @patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None)
    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_bundle")
    @patch("utils.llm.ai_explainer.decode_calldata", return_value=PAUSE)
    def test_calls_past_the_cap_keep_labelled_transfers(
        self,
        _mock_decode: MagicMock,
        mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_label: MagicMock,
        _mock_source: MagicMock,
        _mock_meta: MagicMock,
    ) -> None:
        """A CAP batch's last two calls (the OndoHolder deposit) were dropped, and the
        harvest's fee receiver appeared only as raw hex, so the report could not name it."""
        receiver = "0x0000000000000000000000000000000000000FEE"  # checksummed, as rendered
        sender = "0x00000000000000000000000000000000000000AB"
        mock_label.side_effect = lambda _chain, addr: "FeeAuction" if addr == receiver else ""
        total = MAX_PROMPT_SIMULATIONS + 1
        transfer = AssetChange(
            token_address=_addr(0xC0),
            token_name="USD Coin",
            token_symbol="USDC",
            from_address=sender.lower(),
            to_address=receiver.lower(),
            amount="98999.899127",
            raw_amount="98999899127",
            decimals=6,
        )
        sims = [SimulationResult(success=True, gas_used=100 + i) for i in range(total - 1)]
        sims.append(SimulationResult(success=True, gas_used=999, asset_changes=[transfer]))
        mock_simulate.return_value = sims
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[{"target": _addr(i), "data": PAUSE_DATA, "value": "0"} for i in range(total)],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertIn(f"Call {total} (batch order, after calls 1-{total - 1}; token transfers only):", prompt)
        self.assertIn(f"98999.899127 USDC from {sender} to {receiver} (FeeAuction)", prompt)
        links = prompt.split("--- Address Links", 1)[1].split("\n---", 1)[0]
        self.assertIn(receiver, links)

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

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_zero_value_is_omitted_for_decoded_and_undecoded_alike(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        """Governance calls carry no value ~always; a 0 line on every entry is noise."""
        mock_decode.side_effect = lambda data, chain_id=None, target=None: PAUSE if data == PAUSE_DATA else None
        provider = self._provider()
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            calls=[
                {"target": "0xT1", "data": PAUSE_DATA, "value": "0"},
                {"target": "0xT2", "data": UNKNOWN_DATA, "value": "0"},
                {"target": "0xT3", "data": "0x", "value": "0"},
            ],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        prompt = provider.complete.call_args[0][0]
        self.assertNotIn("ETH value:", prompt)
        self.assertNotIn("0.000000 ETH", prompt)
        self.assertIn("invokes nothing and transfers nothing", prompt)
        self.assertNotIn("**ETH value:**", result.report)
        # The entries themselves must survive; only the noisy zero line goes.
        self.assertIn("Call 2: UNDECODED", prompt)
        self.assertIn("2. **Undecoded calldata**", result.report)
        self.assertIn("3. **Empty calldata**", result.report)
