"""Tests for Infinifi Outland LLM governance context."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import abi_exposure, infinifi_outland_context
from utils.llm.infinifi_outland_context import (
    ZERO_ADDRESS,
    ConnectorRouteContext,
    FarmTypeContext,
    HubVaultContext,
    OracleAssignmentContext,
    format_outland_prompt,
    format_outland_report,
    resolve_outland_context,
)

ACCOUNTING = "0x7A5C5dbA4fbD0e1e1A2eCDBe752fAe55f6E842B3"
HUB = "0x13025F34C1ec2A16bF68f3a3c4e986a3E85CED61"
FARM = "0xA7c1DAEAA5D97e1319B4Ff6Cdf658F5C4582A27E"
REGISTRY = "0xF5f2718708f471e43968271956CC01aaA8c46119"
CONNECTOR = "0x3373784A7a52A07F9339aA8F60403420cC602c52"
VAULT = "0x77776F422B7EB0A95ccD35fBd088A5957D4408eA"
BASE_VAULT = "0xf0d0F1fdEE5595628De17B37E4134a5bAc4441C3"
ORACLE = "0x168DF792845BA1bd80d485399de63a4110b03242"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
PEER = "0x1111111111111111111111111111111111111111"


def _call(name: str, *params: tuple[str, object]) -> DecodedCall:
    return DecodedCall(function_name=name, signature=f"{name}()", params=list(params))


def _client(batch_results: list[list[object]], single_call: object = None) -> MagicMock:
    """A ChainManager client whose batches return ``batch_results`` in order."""
    client = MagicMock()
    client.execute_batch.side_effect = batch_results
    client.get_contract.return_value.functions.getVault.return_value.call.return_value = single_call
    client.get_contract.return_value.functions.price.return_value.call.return_value = single_call
    return client


class TestGuards(unittest.TestCase):
    def test_other_protocol_resolves_nothing(self) -> None:
        call = _call("addFarms", ("uint256", 2), ("address[]", (FARM,)))
        self.assertEqual(resolve_outland_context("3jane", 1, [(REGISTRY, call)]), [])

    def test_other_chain_resolves_nothing(self) -> None:
        call = _call("addFarms", ("uint256", 2), ("address[]", (FARM,)))
        self.assertEqual(resolve_outland_context("infinifi", 8453, [(REGISTRY, call)]), [])

    def test_resolution_failure_does_not_raise(self) -> None:
        call = _call("setOracle", ("address", VAULT), ("address", ORACLE))
        with patch.object(infinifi_outland_context, "exposes", side_effect=RuntimeError("etherscan down")):
            self.assertEqual(resolve_outland_context("infinifi", 1, [(ACCOUNTING, call)]), [])


class TestFarmType(unittest.TestCase):
    def test_add_farms_type_is_named_without_rpc(self) -> None:
        call = _call("addFarms", ("uint256", 2), ("address[]", (FARM.lower(),)))
        with patch.object(infinifi_outland_context, "exposes") as probe:
            contexts = resolve_outland_context("infinifi", 1, [(REGISTRY, call)])
        probe.assert_not_called()
        self.assertEqual(contexts, [FarmTypeContext(REGISTRY, "addFarms", 2, (FARM,))])
        prompt = format_outland_prompt(contexts)
        self.assertIn("farm type 2 = FarmTypes.MATURITY", prompt)
        self.assertIn("principal is locked until the farm's maturity", prompt)

    def test_unknown_farm_type_is_flagged(self) -> None:
        context = FarmTypeContext(REGISTRY, "addFarms", 7, (FARM,))
        self.assertIn("FarmTypes.UNKNOWN — not a FarmTypes constant", format_outland_prompt([context]))


class TestOracle(unittest.TestCase):
    def test_price_is_scaled_by_asset_decimals(self) -> None:
        call = _call("setOracle", ("address", VAULT), ("address", ORACLE))
        with (
            patch.object(infinifi_outland_context, "exposes", return_value=True),
            patch.object(infinifi_outland_context, "fetch_erc20_metadata", return_value=ERC20Metadata("OV-143", 18)),
            patch.object(infinifi_outland_context.ChainManager, "get_client", return_value=_client([], 10**18)),
        ):
            contexts = resolve_outland_context("infinifi", 1, [(ACCOUNTING, call)])
        self.assertEqual(contexts, [OracleAssignmentContext(ACCOUNTING, VAULT, "OV-143", 18, ORACLE, 10**18)])
        self.assertIn("one whole OV-143 is valued at 1 reference units", format_outland_prompt(contexts))

    def test_usdc_scale_reads_as_parity(self) -> None:
        # IOracle convention: a 6-decimal stable at parity is quoted at 1e30.
        context = OracleAssignmentContext(ACCOUNTING, USDC, "USDC", 6, ORACLE, 10**30)
        self.assertIn("= `1` reference units", format_outland_report([context], 1, {}))

    def test_removing_an_oracle_is_not_priced(self) -> None:
        call = _call("setOracle", ("address", VAULT), ("address", ZERO_ADDRESS))
        with patch.object(infinifi_outland_context, "exposes") as probe:
            self.assertEqual(resolve_outland_context("infinifi", 1, [(ACCOUNTING, call)]), [])
        probe.assert_not_called()


class TestHubVault(unittest.TestCase):
    def _resolve(self, registered: list[int], existing: str | None = None) -> list:
        call = _call("setVault", ("address", VAULT))
        with (
            patch.object(infinifi_outland_context, "exposes", return_value=True),
            patch.object(
                infinifi_outland_context.ChainManager, "get_client", return_value=_client([[143, registered]], existing)
            ),
        ):
            return resolve_outland_context("infinifi", 1, [(HUB, call)])

    def test_new_chain_replaces_nothing(self) -> None:
        contexts = self._resolve([8453])
        self.assertEqual(contexts, [HubVaultContext(HUB, VAULT, 143, (8453,), None)])
        prompt = format_outland_prompt(contexts)
        self.assertIn("adds chain 143; no existing vault is replaced", prompt)
        self.assertIn("Chains registered before this call: 8453", prompt)

    def test_existing_chain_names_the_replaced_vault(self) -> None:
        contexts = self._resolve([8453, 143], existing=BASE_VAULT)
        self.assertEqual(contexts[0].replaced_vault, BASE_VAULT)
        self.assertIn(f"REPLACES the existing chain-143 vault {BASE_VAULT}", format_outland_prompt(contexts))

    def test_farm_set_vault_is_not_treated_as_hub(self) -> None:
        # OutlandFarm.setVault shares the selector but has no per-chain registry.
        call = _call("setVault", ("address", VAULT))
        with patch.object(infinifi_outland_context, "exposes", return_value=False):
            self.assertEqual(resolve_outland_context("infinifi", 1, [(FARM, call)]), [])


class TestConnectorRoute(unittest.TestCase):
    def _resolve(self, calls: list[DecodedCall], config: tuple[str, int, int]) -> list:
        with (
            patch.object(infinifi_outland_context, "exposes", return_value=True),
            patch.object(infinifi_outland_context.ChainManager, "get_client", return_value=_client([[config]])),
        ):
            return resolve_outland_context("infinifi", 1, [(CONNECTOR, call) for call in calls])

    def test_unconfigured_route_is_not_live(self) -> None:
        calls = [
            _call("enableChainAsset", ("uint256", 143), ("address", USDC)),
            _call("setCctpDomain", ("uint256", 143), ("uint32", 15)),
        ]
        contexts = self._resolve(calls, (ZERO_ADDRESS, 0, 0))
        # Both calls name chain 143, so it is read once.
        self.assertEqual(contexts, [ConnectorRouteContext(CONNECTOR, 143, ZERO_ADDRESS, 0, 0, False)])
        prompt = format_outland_prompt(contexts)
        self.assertIn("NOT configured", prompt)
        self.assertIn("route is not live after this batch", prompt)
        self.assertIn("**not configured**", format_outland_report(contexts, 1, {}))

    def test_configured_route_names_the_peer(self) -> None:
        contexts = self._resolve([_call("setCctpDomain", ("uint256", 143), ("uint32", 15))], (PEER, 200_000, 99))
        self.assertIn(f"configured — peer {PEER}, gas limit 200,000", format_outland_prompt(contexts))
        self.assertIn(PEER, contexts[0].addresses)

    def test_configuration_in_the_same_batch_is_recognised(self) -> None:
        calls = [
            _call("setCctpDomain", ("uint256", 143), ("uint32", 15)),
            _call("setConfiguration", ("uint256", 143), ("address", PEER), ("uint256", 99), ("uint256", 200_000)),
        ]
        contexts = self._resolve(calls, (ZERO_ADDRESS, 0, 0))
        self.assertIn("this batch calls setConfiguration for it", format_outland_prompt(contexts))


class TestAbiExposure(unittest.TestCase):
    def setUp(self) -> None:
        abi_exposure.reset_cache()

    def test_proxy_falls_back_to_implementation(self) -> None:
        def abi_for(_chain_id: int, address: str) -> list[dict]:
            name = "getVaultChainIds" if address == "0ximpl" else "upgradeTo"
            return [{"type": "function", "name": name}]

        with (
            patch.object(abi_exposure, "fetch_abi_entries", side_effect=abi_for),
            patch("utils.proxy.get_current_implementation", return_value="0ximpl"),
        ):
            self.assertTrue(abi_exposure.exposes(1, HUB, {"getVaultChainIds"}))

    def test_own_abi_skips_the_proxy_lookup(self) -> None:
        with (
            patch.object(abi_exposure, "fetch_abi_entries", return_value=[{"type": "function", "name": "price"}]),
            patch("utils.proxy.get_current_implementation") as lookup,
        ):
            self.assertTrue(abi_exposure.exposes(1, ORACLE, {"price"}))
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
