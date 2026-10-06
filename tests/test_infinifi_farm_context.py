"""Tests for the Infinifi farm-configuration context adapter (swap pairs, enabled assets, PT discounts)."""

import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import infinifi_farm_context
from utils.llm.infinifi_farm_context import (
    AssetEnableContext,
    PendleDiscountContext,
    SwapPairContext,
    format_infinifi_farm_prompt,
    format_infinifi_farm_report,
    resolve_infinifi_farm_context,
)

ACCOUNTING = "0x7A5C5dbA4fbD0e1e1A2eCDBe752fAe55f6E842B3"
SWAP_FARM = "0x90787c1b99F47EFfEE0db0aB9D4e33CdC3e6bFa7"
PENDLE_FARM = "0xe9E44C0A68C49aDe51CdA5e027909C5996B5c71a"
REGISTRY = "0xF5f2718708f471e43968271956CC01aaA8c46119"
REUSD = "0x5086bf358635B81D8C47C66d1C8b9E567Db70c72"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
PT = "0xeCfaFdC7741323a945A163ed068B5a3C43483957"
ORACLE = "0xC86Fb9B40E4C3eae6fe17D3AecDf12B721a89F02"
USDC_ORACLE = "0x1111111111111111111111111111111111111111"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
E18 = 10**18
E6 = 10**6

METADATA = {
    USDC: ERC20Metadata("USDC", 6),
    REUSD: ERC20Metadata("reUSD", 18),
    PT: ERC20Metadata("PT-reUSD-10DEC2026", 6),
}


class _Call:
    def __init__(self, value: object) -> None:
        self.value = value

    def call(self) -> object:
        return self.value


class _Functions:
    """``contract.functions.<name>(*args).call()`` answered from a (address, name) table."""

    def __init__(self, address: str, store: dict[tuple[str, str], object]) -> None:
        self.address = address.lower()
        self.store = store

    def __getattr__(self, name: str) -> Callable[..., _Call]:
        value = self.store[(self.address, name)]

        def function(*args: object) -> _Call:
            return _Call(value(*args) if callable(value) else value)

        return function


class _Contract:
    def __init__(self, address: str, store: dict[tuple[str, str], object]) -> None:
        self.functions = _Functions(address, store)


class _Client:
    def __init__(self, store: dict[tuple[str, str], object]) -> None:
        self.store = store

    def get_contract(self, address: str, _abi: Any) -> _Contract:
        return _Contract(address, self.store)


def _store(
    *,
    reusd_supported: bool = False,
    reusd_oracle: str = ZERO_ADDRESS,
    floor: int = 99 * E18 // 100,
    pair: tuple[int, int, int] = (0, 0, 0),
    total_pts: int = 0,
    registered: bool = False,
) -> dict[tuple[str, str], object]:
    oracles = {USDC.lower(): USDC_ORACLE, REUSD.lower(): reusd_oracle}
    swap, pendle = SWAP_FARM.lower(), PENDLE_FARM.lower()
    return {
        (swap, "accounting"): ACCOUNTING,
        (swap, "assetToken"): USDC,
        (swap, "assets"): 0,
        (swap, "isAssetSupported"): lambda token: token.lower() == USDC.lower() or reusd_supported,
        (swap, "maxSlippage"): floor,
        (swap, "_MAX_COOLDOWN"): 43_200,
        (swap, "getSwapPairConfig"): lambda _a, _b: pair,
        (ACCOUNTING.lower(), "oracle"): lambda token: oracles[token.lower()],
        (pendle, "assetToken"): USDC,
        (pendle, "assets"): total_pts,
        (pendle, "maturity"): 1_796_860_800,
        (pendle, "maturityPTDiscount"): 998 * E18 // 1000,
        (pendle, "totalReceivedPTs"): total_pts,
        (pendle, "ptToAssetsAtMaturity"): lambda amount: amount,
        (pendle, "PT"): PT,
        (REGISTRY.lower(), "isFarm"): lambda _farm: registered,
    }


def _call(name: str, *params: tuple[str, object]) -> DecodedCall:
    return DecodedCall(function_name=name, signature=f"{name}()", params=list(params))


ADD_FARM = _call("addFarms", ("uint256", 2), ("address[]", [PENDLE_FARM]))
SET_ORACLE = _call("setOracle", ("address", REUSD), ("address", ORACLE))
ENABLE_REUSD = _call("enableAssets", ("address[]", [REUSD]))
PAIR = _call(
    "setPairConfig", ("address", USDC), ("address", REUSD), ("uint256", 1_200), ("uint256", 9995 * E18 // 10_000)
)
DISCOUNT = _call("setMaturityPTDiscount", ("uint256", 9995 * E18 // 10_000))


def _resolve(calls: list[tuple[str, DecodedCall]], **state: Any) -> list:
    with (
        patch.object(infinifi_farm_context, "exposes", return_value=True),
        patch.object(infinifi_farm_context, "fetch_erc20_metadata", side_effect=lambda _c, token: METADATA.get(token)),
        patch.object(infinifi_farm_context.ChainManager, "get_client", return_value=_Client(_store(**state))),
    ):
        return resolve_infinifi_farm_context("INFINIFI", 1, calls)


# The scheduled batch: register the Pendle farm, enable reUSD, configure USDC/reUSD — with
# Accounting's reUSD oracle coming from a SEPARATE operation that has not executed.
BATCH = [(REGISTRY, ADD_FARM), (SWAP_FARM, ENABLE_REUSD), (SWAP_FARM, PAIR)]


class TestSwapPair(unittest.TestCase):
    def test_slippage_is_a_minimum_output_ratio(self) -> None:
        """0.9995e18 was reported as a 99.95% loss allowance and rated HIGH; it caps loss at 0.05%."""
        *_, pair = _resolve(BATCH)
        assert isinstance(pair, SwapPairContext)
        prompt = format_infinifi_farm_prompt([pair])
        self.assertIn("_slippage 999500000000000000 is a MINIMUM-OUTPUT ratio, not a loss allowance", prompt)
        self.assertIn("must return at least 99.95% of the oracle-converted input", prompt)
        self.assertIn("at most 0.05% loss versus Accounting prices", prompt)
        self.assertIn("farm-wide floor maxSlippage 99%", prompt)
        self.assertIn("this pair is stricter than the floor", prompt)

    def test_cooldown_is_in_seconds(self) -> None:
        *_, pair = _resolve(BATCH)
        prompt = format_infinifi_farm_prompt([pair])
        self.assertIn("cooldown 1,200 seconds (20 min)", prompt)
        self.assertIn("maximum 12h", prompt)
        self.assertIn("current pair config: unset", prompt)

    def test_token_support_names_the_missing_oracle(self) -> None:
        *_, pair = _resolve(BATCH)
        prompt = format_infinifi_farm_prompt([pair])
        self.assertIn(
            "USDC is the farm's asset token; reUSD: enabled by the earlier enableAssets in this batch once "
            "Accounting has its oracle (NOT set yet)",
            prompt,
        )

    def test_ratio_below_the_floor_reverts(self) -> None:
        low = _call(
            "setPairConfig", ("address", USDC), ("address", REUSD), ("uint256", 0), ("uint256", 98 * E18 // 100)
        )
        (pair,) = _resolve([(SWAP_FARM, low)], reusd_supported=True, reusd_oracle=ORACLE)
        self.assertIn("BELOW the farm-wide floor maxSlippage 99%", format_infinifi_farm_prompt([pair]))

    def test_existing_pair_config_is_shown(self) -> None:
        (pair,) = _resolve(
            [(SWAP_FARM, PAIR)], reusd_supported=True, reusd_oracle=ORACLE, pair=(1, 600, 995 * E18 // 1000)
        )
        self.assertIn("current pair config: cooldown 600s, minimum output 99.5%", format_infinifi_farm_prompt([pair]))


class TestEnableAssets(unittest.TestCase):
    def test_missing_oracle_is_an_ordering_prerequisite(self) -> None:
        enable, pair = _resolve(BATCH)
        assert isinstance(enable, AssetEnableContext)
        prompt = format_infinifi_farm_prompt([enable])
        self.assertIn("oracle(reUSD) is NOT set yet", prompt)
        self.assertIn("this call reverts (InvalidOracle), and an atomic batch reverts with it", prompt)
        self.assertIn("assess the call by its effect once enabled, not as a failed or no-op transaction", prompt)
        # The next call sees reUSD as enabled once its oracle lands, not as unsupported.
        assert isinstance(pair, SwapPairContext)
        self.assertEqual(pair.token_out.status, "enable pending oracle")

    def test_oracle_set_earlier_in_the_batch_satisfies_it(self) -> None:
        enable, pair = _resolve([(ACCOUNTING, SET_ORACLE), (SWAP_FARM, ENABLE_REUSD), (SWAP_FARM, PAIR)])
        self.assertIn(
            "an earlier call in this batch sets it, so the asset is enabled", format_infinifi_farm_prompt([enable])
        )
        assert isinstance(pair, SwapPairContext)
        self.assertEqual(pair.token_out.status, "enabled earlier in this batch")

    def test_already_supported_asset_reverts(self) -> None:
        (enable,) = _resolve([(SWAP_FARM, ENABLE_REUSD)], reusd_supported=True, reusd_oracle=ORACLE)
        self.assertIn("is already supported, so the call reverts (InvalidAsset)", format_infinifi_farm_prompt([enable]))

    def test_report_flags_the_missing_oracle(self) -> None:
        enable, _pair = _resolve(BATCH)
        report = format_infinifi_farm_report([enable], 1, {})
        self.assertIn("**no Accounting oracle — reverts until one is set**", report)


class TestPendleDiscount(unittest.TestCase):
    def test_no_pts_held_means_no_immediate_change(self) -> None:
        (context,) = _resolve([(PENDLE_FARM, DISCOUNT)])
        assert isinstance(context, PendleDiscountContext)
        prompt = format_infinifi_farm_prompt([context])
        self.assertIn("maturityPTDiscount 99.8% → 99.95% (haircut 0.2% → 0.05%)", prompt)
        self.assertIn("PT-reUSD-10DEC2026, matures 10/12/2026", prompt)
        self.assertIn("the farm holds 0 PT and reports assets() = 0 USDC", prompt)
        self.assertIn("reported assets do not change now", prompt)
        self.assertIn("NOT yet registered in FarmRegistry", prompt)

    def test_held_pts_change_reported_assets(self) -> None:
        (context,) = _resolve([(PENDLE_FARM, DISCOUNT)], total_pts=1_000_000 * E6, registered=True)
        prompt = format_infinifi_farm_prompt([context])
        self.assertIn("reported assets change immediately by 1,500 USDC", prompt)
        self.assertIn("the farm is registered in FarmRegistry", prompt)
        self.assertIn("assets() changes by `1,500 USDC`", format_infinifi_farm_report([context], 1, {}))

    def test_add_farms_earlier_in_the_batch_registers_it(self) -> None:
        (context,) = _resolve([(REGISTRY, ADD_FARM), (PENDLE_FARM, DISCOUNT)])
        self.assertIn("the farm is registered in FarmRegistry", format_infinifi_farm_prompt([context]))

    def test_factor_above_one_is_flagged(self) -> None:
        above = _call("setMaturityPTDiscount", ("uint256", E18 + 1))
        (context,) = _resolve([(PENDLE_FARM, above)])
        self.assertIn("ABOVE 100%", format_infinifi_farm_prompt([context]))


class TestGuards(unittest.TestCase):
    def test_other_protocol_or_chain_resolves_nothing(self) -> None:
        self.assertEqual(resolve_infinifi_farm_context("3jane", 1, BATCH), [])
        self.assertEqual(resolve_infinifi_farm_context("infinifi", 8453, BATCH), [])

    def test_batch_without_explained_calls_skips_rpc(self) -> None:
        with patch.object(infinifi_farm_context.ChainManager, "get_client") as client:
            self.assertEqual(resolve_infinifi_farm_context("infinifi", 1, [(ACCOUNTING, SET_ORACLE)]), [])
        client.assert_not_called()

    def test_read_failure_is_swallowed(self) -> None:
        with (
            patch.object(infinifi_farm_context, "exposes", return_value=True),
            patch.object(infinifi_farm_context.ChainManager, "get_client", return_value=_Client({})),
        ):
            self.assertEqual(resolve_infinifi_farm_context("infinifi", 1, [(SWAP_FARM, PAIR)]), [])


if __name__ == "__main__":
    unittest.main()
