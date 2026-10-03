"""Tests for explainer addresses."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.formatting import format_decimal_amount, normalize_token_amount
from utils.llm.ai_explainer import (
    _build_prompt,
    _collect_token_flows,
    _token_label,
    _with_onchain_symbols,
    collect_unique_addresses,
    explain_transaction,
)
from utils.tenderly.simulation import AssetChange, SimulationResult

from .helpers import make_provider


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


class TestAddressLabels(unittest.TestCase):
    """Tests for address-argument annotation in the LLM prompt."""

    REGISTRY = "0xF5f2718708F471e43968271956cC01Aaa8C46119"
    FARM = "0xac21b22b5aeb11bc32de4ecf59e4538fca48b694"
    FARM_CKS = "0xAc21B22B5aEb11bc32De4ecF59E4538fCa48b694"

    def setUp(self) -> None:
        """Isolate address resolution from external providers for each test."""
        prefix = "utils.llm.ai_explainer."
        self.enterContext(patch(prefix + "get_source_context", return_value=None))
        self.enterContext(patch(prefix + "simulate_transaction", return_value=None))
        self.enterContext(patch(prefix + "fetch_erc20_metadata", return_value=None))
        self.decode = self.enterContext(patch(prefix + "decode_calldata"))
        self.label = self.enterContext(patch(prefix + "get_contract_label"))
        self.get_provider = self.enterContext(patch(prefix + "get_llm_provider"))

    def test_address_array_arg_is_labeled(self) -> None:
        self.decode.return_value = DecodedCall(
            function_name="addFarms",
            signature="addFarms(uint256,address[])",
            params=[("uint256", 1), ("address[]", (self.FARM,))],
        )
        self.label.return_value = "MorphoFarm"
        provider = make_provider("TLDR: adds farm. LOW.", model_name="test-model")
        self.get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xabcdef10" + "00" * 64, chain_id=1)

        prompt = provider.complete.call_args[0][0]
        self.assertIn("MorphoFarm", prompt)
        self.assertIn(self.FARM_CKS, prompt)
        # Address goes on its own line, bulleted, under the type label.
        self.assertIn("address[]:", prompt)
        self.assertIn(f"- {self.FARM_CKS} (MorphoFarm)", prompt)

    def test_scalar_address_arg_is_labeled(self) -> None:
        self.decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", self.FARM)],
        )
        self.label.return_value = "ChainlinkOracle"
        provider = make_provider("TLDR: rewires oracle. MEDIUM.", model_name="test-model")
        self.get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)

        prompt = provider.complete.call_args[0][0]
        self.assertIn(f"address: {self.FARM_CKS} (ChainlinkOracle)", prompt)

    def test_target_appearing_as_arg_is_deduped(self) -> None:
        self.decode.return_value = DecodedCall(
            function_name="selfWire",
            signature="selfWire(address)",
            params=[("address", self.REGISTRY.lower())],
        )
        self.label.return_value = ""
        provider = make_provider("TLDR: wires self. LOW.", model_name="test-model")
        self.get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xdeadbeef" + "00" * 32, chain_id=1)

        self.assertEqual(self.label.call_count, 1)

    def test_unverified_address_left_unannotated(self) -> None:
        self.decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", self.FARM)],
        )
        self.label.return_value = ""  # unverified / EOA / no API key
        provider = make_provider("TLDR: rewires. MEDIUM.", model_name="test-model")
        self.get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)

        prompt = provider.complete.call_args[0][0]
        # Address shows up, but with no `(Label)` suffix.
        self.assertIn(self.FARM_CKS, prompt)
        self.assertNotIn(f"{self.FARM_CKS} (", prompt)

    def test_address_inside_nested_bytes_is_labeled(self) -> None:
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

        self.decode.side_effect = routed_decode
        self.label.return_value = "ImplContract"

        provider = make_provider("TLDR: upgrades. MEDIUM.", model_name="test-model")
        self.get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0xUPGRADE_CALLDATA", chain_id=1)

        addresses_looked_up = {call.args[1].lower() for call in self.label.call_args_list}
        self.assertIn("0x" + "11" * 20, addresses_looked_up)

    def test_zero_address_not_queried(self) -> None:
        zero = "0x" + "00" * 20
        self.decode.return_value = DecodedCall(
            function_name="setOracle",
            signature="setOracle(address)",
            params=[("address", zero)],
        )
        self.label.return_value = ""
        provider = make_provider("TLDR: unsets oracle. LOW.", model_name="test-model")
        self.get_provider.return_value = provider

        explain_transaction(target=self.REGISTRY, calldata="0x7adbf973" + "00" * 32, chain_id=1)
        # Resolver called exactly once — for the target, not for the zero arg.
        addresses_queried = {call.args[1].lower() for call in self.label.call_args_list}
        self.assertNotIn(zero, addresses_queried)


class TestOnchainSymbols(unittest.TestCase):
    """Tenderly's lowercased symbols are replaced by the token's own symbol()."""

    TOKEN = "0xd4fa2d31b7968e448877f69a96de69f5de8cd23e"

    def _sim(self, token_address: str) -> SimulationResult:
        change = AssetChange(
            token_address=token_address,
            token_name="Wrapped Aave Ethereum USDC",
            token_symbol="waethusdc",
            from_address="0x1",
            to_address="0x2",
            amount="31685.345344",
            raw_amount="31685345344",
            decimals=6,
        )
        return SimulationResult(success=True, gas_used=1, asset_changes=[change])

    def test_symbol_comes_from_the_token(self) -> None:
        with patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=ERC20Metadata("waEthUSDC", 6)):
            sim = _with_onchain_symbols(self._sim(self.TOKEN), 1)
        assert sim is not None
        self.assertEqual(sim.asset_changes[0].token_symbol, "waEthUSDC")

    def test_tenderly_symbol_kept_when_metadata_unavailable(self) -> None:
        with patch("utils.llm.ai_explainer.fetch_erc20_metadata", return_value=None):
            sim = _with_onchain_symbols(self._sim(self.TOKEN), 1)
        assert sim is not None
        self.assertEqual(sim.asset_changes[0].token_symbol, "waethusdc")

    def test_no_lookup_without_asset_changes_or_address(self) -> None:
        with patch("utils.llm.ai_explainer.fetch_erc20_metadata") as fetch:
            self.assertIsNone(_with_onchain_symbols(None, 1))
            _with_onchain_symbols(SimulationResult(success=True), 1)
            _with_onchain_symbols(self._sim(""), 1)
        fetch.assert_not_called()


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


class TestTokenLabel(unittest.TestCase):
    """On-chain name() distinguishes deployments that share a contract-type label."""

    def test_name_leads_when_base_is_generic(self) -> None:
        meta = ERC20Metadata("yETH-Recovery", 18, "Yearn yETH Recovery Vault")
        self.assertEqual(
            _token_label("Yearn V3 Vault", meta), "Yearn yETH Recovery Vault (yETH-Recovery, 18 dec) — Yearn V3 Vault"
        )

    def test_base_kept_when_it_already_names_the_token(self) -> None:
        meta = ERC20Metadata("USDC", 6, "USD Coin")
        self.assertEqual(_token_label("Centre: USD Coin", meta), "Centre: USD Coin (USDC, 6 dec)")

    def test_name_without_base(self) -> None:
        meta = ERC20Metadata("ysWETH", 18, "wstETH/WETH Spark Looper")
        self.assertEqual(_token_label("", meta), "wstETH/WETH Spark Looper (ysWETH, 18 dec)")

    def test_no_name_keeps_previous_format(self) -> None:
        self.assertEqual(
            _token_label("Circle: USDC Token", ERC20Metadata("USDC", 6)), "Circle: USDC Token (USDC, 6 dec)"
        )
        self.assertEqual(_token_label("", ERC20Metadata("USDC", 6)), "USDC, 6 dec")
