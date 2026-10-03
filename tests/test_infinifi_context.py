"""Tests for Infinifi-specific LLM farm and token enrichment."""

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import infinifi_context
from utils.llm.infinifi_context import (
    InfinifiEscrowContext,
    RateChange,
    TokenContext,
    WhitelistChange,
    _candidate_addresses,
    _EscrowState,
    _farm_matches_escrow,
    _FarmRecord,
    _fetch_farm_records,
    _fetch_whitelist_targets,
    _looks_like_escrow,
    _rate_and_overrides,
    _rate_percent,
    _resolve_configured_tokens,
    _resolve_whitelist,
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

    @patch.object(infinifi_context, "_keeper_label", return_value="")
    @patch.object(infinifi_context, "_whitelist_changes", return_value=())
    @patch.object(infinifi_context, "_rate_and_overrides", return_value=(None, ()))
    @patch.object(infinifi_context, "_resolve_whitelist")
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
        mock_whitelist: MagicMock,
        _mock_rate: MagicMock,
        _mock_changes: MagicMock,
        _mock_keeper: MagicMock,
    ) -> None:
        state = _EscrowState(ESCROW, FARM, USDC, 3_003_294_554_623)
        mock_escrow.side_effect = lambda _chain, address: state if address.lower() == ESCROW.lower() else None
        mock_farms.return_value = (_FarmRecord(FARM, "New Silver 2 Senior", "new-silver-senior"),)
        mock_token.return_value = TokenContext(USDC, "USD Coin", "USDC", 6)
        mock_whitelist.return_value = (_resolved_context().configured_tokens, ())

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

    @patch.object(infinifi_context, "_contract_label", return_value="Jane")
    @patch.object(infinifi_context, "_read_token")
    @patch.object(infinifi_context, "_fetch_whitelist_targets")
    def test_keeps_only_non_accounting_erc20_targets(
        self,
        mock_candidates: MagicMock,
        mock_read: MagicMock,
        _mock_label: MagicMock,
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
        mock_read.side_effect = [drop, None]
        tokens, contracts = _resolve_whitelist(1, state)
        self.assertEqual((tokens, contracts), ((drop,), ((zero_token, "Jane"),)))


class TestInfinifiContextFormatting(unittest.TestCase):
    def test_prompt_names_farm_and_drop_token(self) -> None:
        result = format_infinifi_prompt([_resolved_context()])
        self.assertIn("New Silver 2 Senior", result)
        self.assertIn("New Silver Series 2 DROP", result)
        self.assertIn("3,003,294.554623 USDC", result)
        self.assertIn("Whitelisted ERC20 call target", result)

    def test_report_links_all_context_addresses(self) -> None:
        context = _resolved_context()
        report = format_infinifi_report([context], 1, context.labels)
        self.assertIn("**Farm:** New Silver 2 Senior", report)
        self.assertIn(f"https://etherscan.io/address/{FARM}", report)
        self.assertIn(f"https://etherscan.io/address/{DROP}", report)
        self.assertIn("New Silver Series 2 DROP", report)
        self.assertIn("Whitelisted call targets", report)


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


class TestCandidateAddresses(unittest.TestCase):
    def test_collects_target_and_address_params(self) -> None:
        call = DecodedCall(
            function_name="setRate",
            signature="setRate(address,uint256)",
            params=[("address", ESCROW), ("uint256", 1)],
        )
        result = _candidate_addresses([(MANAGER, call)])
        self.assertEqual(result, [MANAGER, ESCROW])

    def test_dedupes_across_calls(self) -> None:
        call = DecodedCall(function_name="setRate", signature="setRate(address,uint256)", params=[("address", ESCROW)])
        self.assertEqual(_candidate_addresses([(ESCROW, call), (MANAGER, call)]), [ESCROW, MANAGER])


class TestLooksLikeEscrow(unittest.TestCase):
    def test_requires_all_three_getters(self) -> None:
        abi = [
            {"type": "function", "name": "assetToken", "outputs": []},
            {"type": "function", "name": "owner", "outputs": []},
            {"type": "function", "name": "totalAssets", "outputs": []},
        ]
        self.assertTrue(_looks_like_escrow(abi))
        self.assertFalse(_looks_like_escrow(abi[:-1]))
        self.assertFalse(_looks_like_escrow([]))


class TestFarmLookupAndParsing(unittest.TestCase):
    def setUp(self) -> None:
        infinifi_context.reset_cache()

    @patch.object(infinifi_context, "fetch_json")
    def test_fetch_farm_records_parses_api_shape(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = {
            "code": "OK",
            "data": {
                "farms": [
                    {"name": "new-silver-senior", "label": "New Silver 2 Senior", "address": FARM},
                    {"name": "broken", "label": "", "address": "not-an-address"},
                    {"name": "no-address", "label": "", "address": None},
                ]
            },
        }
        records = _fetch_farm_records()
        self.assertEqual(records, (_FarmRecord(FARM, "New Silver 2 Senior", "new-silver-senior"),))

    @patch.object(infinifi_context, "fetch_json", return_value={"code": "ERROR"})
    def test_fetch_farm_records_empty_on_bad_response(self, _mock_fetch: MagicMock) -> None:
        self.assertEqual(_fetch_farm_records(), ())


class TestFormattingEdgeCases(unittest.TestCase):
    def test_report_omits_configured_tokens_section_when_empty(self) -> None:
        context = InfinifiEscrowContext(
            escrow_address=ESCROW,
            farm_address=FARM,
            farm_name="New Silver 2 Senior",
            farm_slug="new-silver-senior",
            accounting_asset=TokenContext(USDC, "USD Coin", "USDC", 6),
            total_assets_raw=3_003_294_554_623,
            configured_tokens=(),
        )
        report = format_infinifi_report([context], 1, context.labels)
        self.assertIn("**Farm:** New Silver 2 Senior", report)
        self.assertNotIn("Configured non-accounting ERC-20 targets", report)

    @patch.object(infinifi_context, "_fetch_farm_records")
    @patch.object(infinifi_context, "_read_token", return_value=None)
    @patch.object(infinifi_context, "_read_escrow_state", return_value=_EscrowState(ESCROW, FARM, USDC, 1))
    def test_skips_escrow_with_non_erc20_accounting_asset(
        self,
        _mock_escrow: MagicMock,
        _mock_token: MagicMock,
        mock_farms: MagicMock,
    ) -> None:
        self.assertEqual(resolve_infinifi_context("INFINIFI", 1, [(MANAGER, _set_rate_call())]), [])
        mock_farms.assert_not_called()


if __name__ == "__main__":
    unittest.main()


ROUTER = "0x7912Eaff92B2f5Bc64Cdd21C76d79FFC12eA855E"
MGLO = "0x1DD91a111606382B77A917633ED90feAf25E0F76"


class TestEscrowCallMeaning(unittest.TestCase):
    """setRate is an annual WAD rate; reports had read it as a relative change of a raw number."""

    def _context(self, **overrides: object) -> InfinifiEscrowContext:
        base = _resolved_context()
        return replace(base, **{"total_assets_raw": 2_607_624_600_324, **overrides})

    def test_rate_percent(self) -> None:
        self.assertEqual(_rate_percent(1_086_830_000_000_000_000), "+8.683%")
        self.assertEqual(_rate_percent(950_000_000_000_000_000), "-5%")
        self.assertEqual(_rate_percent(10**18), "+0%")

    def test_rate_line_states_annual_percentages_and_yearly_amount(self) -> None:
        context = self._context(rate_change=RateChange(1_075_100_000_000_000_000, 1_086_830_000_000_000_000))
        (line,) = context.rate_lines()
        self.assertIn("+7.51% a year (raw 1075100000000000000) → +8.683% a year", line)
        self.assertIn("accrues about 226,420.044046 USDC a year", line)
        self.assertIn("bounded to ±20%", line)

    def test_unset_rate_and_assets_override(self) -> None:
        context = self._context(
            rate_change=RateChange(0, 1_070_000_000_000_000_000), assets_overrides=(2_500_000_000_000,)
        )
        rate, override = context.rate_lines()
        self.assertIn("unset (no accrual) → +7%", rate)
        self.assertIn("booking a loss of 107,624.600324 USDC", override)

    def test_router_custody_and_whitelisted_token(self) -> None:
        mglo = TokenContext(MGLO, "Midas Fasanara Global Open", "mGLO", 18)
        change = WhitelistChange(MGLO, "mGLO", False, True, mglo, 12_043_884_528_783_500_000_000_000)
        context = self._context(is_router=True, receiver=ROUTER, keeper=MANAGER, whitelist_changes=(change,))
        self.assertIn("externalCall — no timelock", context.custody_line())
        (line,) = context.whitelist_lines()
        self.assertIn("false → true", line)
        self.assertIn("router balance 12,043,884.5287835 mGLO", line)
        self.assertIn("can transfer or approve that balance", line)
        prompt = format_infinifi_prompt([context])
        self.assertIn("totalAssets is a reported value", prompt)

    def test_plain_escrow_names_its_off_chain_receiver(self) -> None:
        receiver = "0x4831C121879d3DE0E2B181d9d55E9B0724f5D926"
        line = self._context(receiver=receiver, keeper=MANAGER, keeper_label="RWAEscrowRateManager").custody_line()
        self.assertIn(f"forwarded to the off-chain receiver {receiver}", line)
        self.assertIn("RWAEscrowRateManager", line)

    @patch.object(infinifi_context.ChainManager, "get_client")
    def test_rate_and_overrides_read_the_stored_rate(self, mock_client: MagicMock) -> None:
        rates = MagicMock()
        rates.call.return_value = 1_075_100_000_000_000_000
        mock_client.return_value.get_contract.return_value.functions.rates.return_value = rates
        override = DecodedCall(
            "governanceUpdateTotalAssets",
            "governanceUpdateTotalAssets(address,uint256)",
            [("address", ESCROW), ("uint256", 5)],
        )
        change, overrides = _rate_and_overrides(1, ESCROW, [(MANAGER, _set_rate_call()), (MANAGER, override)])
        self.assertEqual(change, RateChange(1_075_100_000_000_000_000, 1_067_660_000_000_000_000))
        self.assertEqual(overrides, (5,))
