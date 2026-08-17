"""Tests for Infinifi-specific LLM farm and token enrichment."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import infinifi_context
from utils.llm.infinifi_context import (
    InfinifiEscrowContext,
    TokenContext,
    _EscrowState,
    _farm_matches_escrow,
    _FarmRecord,
    _fetch_whitelist_targets,
    _resolve_configured_tokens,
    _TokenCandidate,
    format_infinifi_prompt,
    format_infinifi_report,
    resolve_infinifi_context,
)

MANAGER = "0x11F6FAb3f4D8635880C3e80cbae8AEF8136D4189"
ESCROW = "0x6439eb9DADC7977BC1ADC027B10Fb1749AF869A5"
FARM = "0x79e1B8e45932A7C802eA3dAb3844e5DEa68d971f"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DROP = "0xE4C72b4dE5b0F9ACcEA880Ad0b1F944F85A9dAA0"


def _set_rate_call() -> DecodedCall:
    return DecodedCall(
        function_name="setRate",
        signature="setRate(address,uint256)",
        params=[("address", ESCROW), ("uint256", 1_067_660_000_000_000_000)],
    )


def _resolved_context() -> InfinifiEscrowContext:
    return InfinifiEscrowContext(
        escrow_address=ESCROW,
        farm_address=FARM,
        farm_name="New Silver 2 Senior",
        farm_slug="new-silver-senior",
        accounting_asset=TokenContext(USDC, "USD Coin", "USDC", 6),
        total_assets_raw=3_003_294_554_623,
        configured_tokens=(
            TokenContext(
                DROP,
                "New Silver Series 2 DROP",
                "NS2DRP",
                18,
            ),
        ),
    )


class TestResolveInfinifiContext(unittest.TestCase):
    def setUp(self) -> None:
        infinifi_context.reset_cache()

    @patch.object(infinifi_context, "_resolve_configured_tokens")
    @patch.object(infinifi_context, "_farm_matches_escrow", return_value=True)
    @patch.object(infinifi_context, "_read_token")
    @patch.object(infinifi_context, "_fetch_farm_records")
    @patch.object(infinifi_context, "_read_escrow_state")
    def test_resolves_farm_accounting_asset_and_configured_token(
        self,
        mock_escrow: MagicMock,
        mock_farms: MagicMock,
        mock_token: MagicMock,
        _mock_relationship: MagicMock,
        mock_configured_tokens: MagicMock,
    ) -> None:
        state = _EscrowState(ESCROW, FARM, USDC, 3_003_294_554_623)
        mock_escrow.side_effect = lambda _chain, address: state if address.lower() == ESCROW.lower() else None
        mock_farms.return_value = (_FarmRecord(FARM, "New Silver 2 Senior", "new-silver-senior"),)
        mock_token.return_value = TokenContext(USDC, "USD Coin", "USDC", 6)
        mock_configured_tokens.return_value = _resolved_context().configured_tokens

        result = resolve_infinifi_context("INFINIFI", 1, [(MANAGER, _set_rate_call())])

        self.assertEqual(result, [_resolved_context()])
        self.assertIn(DROP, result[0].addresses)
        self.assertIn("New Silver Series 2 DROP", result[0].labels[DROP])

    @patch.object(infinifi_context, "_read_escrow_state")
    def test_skips_other_protocols_without_lookups(self, mock_read: MagicMock) -> None:
        self.assertEqual(resolve_infinifi_context("AAVE", 1, [(MANAGER, _set_rate_call())]), [])
        mock_read.assert_not_called()

    @patch.object(infinifi_context, "_read_escrow_state", side_effect=RuntimeError("RPC down"))
    def test_lookup_failure_does_not_block_alert(self, _mock_read: MagicMock) -> None:
        self.assertEqual(resolve_infinifi_context("INFINIFI", 1, [(MANAGER, _set_rate_call())]), [])

    @patch.object(infinifi_context, "_farm_matches_escrow")
    @patch.object(infinifi_context, "_read_token", return_value=TokenContext(USDC, "USD Coin", "USDC", 6))
    @patch.object(infinifi_context, "_fetch_farm_records", return_value=())
    @patch.object(infinifi_context, "_read_escrow_state", return_value=_EscrowState(ESCROW, FARM, USDC, 1))
    def test_rejects_escrow_without_infinifi_farm(
        self,
        _mock_escrow: MagicMock,
        _mock_farms: MagicMock,
        _mock_token: MagicMock,
        mock_relationship: MagicMock,
    ) -> None:
        self.assertEqual(resolve_infinifi_context("INFINIFI", 1, [(MANAGER, _set_rate_call())]), [])
        mock_relationship.assert_not_called()


class TestFarmRelationship(unittest.TestCase):
    @patch.object(infinifi_context.ChainManager, "get_client")
    def test_requires_farm_escrow_getter_to_match_candidate(self, mock_client: MagicMock) -> None:
        escrow_call = MagicMock()
        escrow_call.call.return_value = ESCROW
        farm = MagicMock()
        farm.functions.escrow.return_value = escrow_call
        mock_client.return_value.get_contract.return_value = farm

        self.assertTrue(_farm_matches_escrow(1, FARM, ESCROW))
        self.assertFalse(_farm_matches_escrow(1, FARM, MANAGER))


class TestConfiguredTokenDiscovery(unittest.TestCase):
    @patch.object(infinifi_context.ChainManager, "get_client")
    def test_reconstructs_current_whitelist_from_events(self, mock_client: MagicMock) -> None:
        event_reader = MagicMock()
        event_reader.get_logs.return_value = [
            {"args": {"target": DROP, "enabled": True}},
            {"args": {"target": USDC, "enabled": True}},
            {"args": {"target": DROP, "enabled": False}},
            {"args": {"target": DROP, "enabled": True}},
        ]
        contract = MagicMock()
        contract.events.WhitelistUpdated.return_value = event_reader
        mock_client.return_value.get_contract.return_value = contract

        self.assertEqual(_fetch_whitelist_targets(1, ESCROW), [DROP, USDC])

    @patch.object(infinifi_context, "_read_token")
    @patch.object(infinifi_context, "_fetch_whitelist_targets")
    def test_keeps_only_non_accounting_erc20_targets(
        self,
        mock_candidates: MagicMock,
        mock_read: MagicMock,
    ) -> None:
        zero_token = "0x333333330522F64EE8d0b3039c460b41670e3404"
        mock_candidates.return_value = [
            USDC,
            DROP,
            zero_token,
        ]
        drop = TokenContext(DROP, "New Silver Series 2 DROP", "NS2DRP", 18)
        mock_read.side_effect = [drop, None]

        state = _EscrowState(ESCROW, FARM, USDC, 0)
        self.assertEqual(_resolve_configured_tokens(1, state), (drop,))
        self.assertEqual(mock_read.call_count, 2)


class TestInfinifiContextFormatting(unittest.TestCase):
    def test_prompt_names_farm_and_drop_token(self) -> None:
        result = format_infinifi_prompt([_resolved_context()])
        self.assertIn("New Silver 2 Senior", result)
        self.assertIn("New Silver Series 2 DROP", result)
        self.assertIn("3,003,294.554623 USDC", result)
        self.assertIn("Configured non-accounting ERC20 target", result)

    def test_report_links_all_context_addresses(self) -> None:
        context = _resolved_context()
        report = format_infinifi_report([context], 1, context.labels)
        self.assertIn("**Farm:** New Silver 2 Senior", report)
        self.assertIn(f"https://etherscan.io/address/{FARM}", report)
        self.assertIn(f"https://etherscan.io/address/{DROP}", report)
        self.assertIn("New Silver Series 2 DROP", report)
        self.assertIn("Configured non-accounting ERC-20 targets", report)


class TestReadToken(unittest.TestCase):
    @patch.object(infinifi_context.ChainManager, "get_client")
    @patch.object(infinifi_context, "fetch_erc20_metadata", return_value=ERC20Metadata("NS2DRP", 18))
    def test_reads_name_on_chain(self, _mock_meta: MagicMock, mock_client: MagicMock) -> None:
        name_call = MagicMock()
        name_call.call.return_value = "New Silver Series 2 DROP"
        contract = MagicMock()
        contract.functions.name.return_value = name_call
        mock_client.return_value.get_contract.return_value = contract

        token = infinifi_context._read_token(1, _TokenCandidate(DROP, "fallback"))

        self.assertEqual(token, TokenContext(DROP, "New Silver Series 2 DROP", "NS2DRP", 18))


if __name__ == "__main__":
    unittest.main()
