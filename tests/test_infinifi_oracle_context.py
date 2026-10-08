"""Tests for the Infinifi Accounting.setOracle context adapter."""

import unittest
from unittest.mock import MagicMock, patch

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import infinifi_oracle_context
from utils.llm.infinifi_oracle_context import (
    FeedDetails,
    OracleAssignmentContext,
    format_infinifi_oracle_prompt,
    format_infinifi_oracle_report,
    resolve_infinifi_oracle_context,
)

ACCOUNTING = "0x7A5C5dbA4fbD0e1e1A2eCDBe752fAe55f6E842B3"
REUSD = "0x5086bf358635B81D8C47C66d1C8b9E567Db70c72"
ORACLE = "0xC86Fb9B40E4C3eae6fe17D3AecDf12B721a89F02"
FEED = to_checksum_address("0x86761b940034eff28001706dc12b50467d69665b")
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
PRICE = 1_105_302_434_183_988_800
READ_AT = 1_791_290_891
UPDATED_AT = READ_AT - 44_568  # 12.4h earlier


def _call(name: str, *params: tuple[str, object]) -> DecodedCall:
    return DecodedCall(function_name=name, signature=f"{name}()", params=list(params))


def _client() -> MagicMock:
    """Oracle → feed reads as the reUSD ChainlinkOracle returns them."""
    client = MagicMock()
    functions = client.get_contract.return_value.functions
    functions.price.return_value.call.return_value = PRICE
    functions.feed.return_value.call.return_value = FEED
    functions.heartbeat.return_value.call.return_value = 172_800
    functions.decimalNormalization.return_value.call.return_value = 0
    functions.divide.return_value.call.return_value = False
    functions.description.return_value.call.return_value = "reUSD NAV / USD"
    functions.latestRoundData.return_value.call.return_value = (1, PRICE, UPDATED_AT, UPDATED_AT, 1)
    client.w3.eth.get_block.return_value = {"timestamp": READ_AT}
    return client


def _resolve(with_feed: bool = True) -> list[OracleAssignmentContext]:
    call = _call("setOracle", ("address", REUSD), ("address", ORACLE))
    names = {ORACLE: "ChainlinkOracle", FEED: "NAVFeedProxy"}

    def verified(_chain: int, address: str) -> MagicMock:
        record = MagicMock()
        record.contract_name = names[address]
        return record

    def exposes(_chain: int, address: str, wanted: set[str]) -> bool:
        return address != ORACLE or with_feed

    with (
        patch.object(infinifi_oracle_context, "exposes", side_effect=exposes),
        patch.object(infinifi_oracle_context, "fetch_erc20_metadata", return_value=ERC20Metadata("reUSD", 18)),
        patch.object(infinifi_oracle_context, "fetch_verified_contract", side_effect=verified),
        patch.object(infinifi_oracle_context.ChainManager, "get_client", return_value=_client()),
    ):
        return resolve_infinifi_oracle_context("INFINIFI", 1, [(ACCOUNTING, call)])


class TestOracleAssignment(unittest.TestCase):
    def test_price_is_scaled_by_asset_decimals(self) -> None:
        (context,) = _resolve()
        self.assertEqual(context.unit_price_text, "1.1053024341839888")
        self.assertIn(
            "one whole reUSD is valued at 1.1053024341839888 reference units", format_infinifi_oracle_prompt([context])
        )

    def test_feed_identity_and_freshness_rule_are_stated(self) -> None:
        """Reports said freshness and failure behaviour were 'not established'; both are on-chain."""
        (context,) = _resolve()
        assert context.feed is not None
        self.assertEqual(context.feed.age, 44_568)
        prompt = format_infinifi_oracle_prompt([context])
        self.assertIn(f"Oracle {ORACLE} (ChainlinkOracle) assigned to {REUSD}", prompt)
        self.assertIn(f'It reads feed {FEED} (NAVFeedProxy, description "reUSD NAV / USD")', prompt)
        self.assertIn("last update 12.4h before this read", prompt)
        self.assertIn("price() reverts (StalePrice) once the feed is older than the heartbeat of 48h", prompt)
        self.assertIn("The wrapper's name refers to the AggregatorV3 interface it reads", prompt)

    def test_report_lists_the_feed(self) -> None:
        (context,) = _resolve()
        report = format_infinifi_oracle_report([context], 1, {})
        self.assertIn("= `1.1053024341839888` reference units", report)
        self.assertIn('"reUSD NAV / USD"; heartbeat `48h`, updated 12.4h before this alert', report)

    def test_oracle_without_a_feed_still_renders_its_price(self) -> None:
        (context,) = _resolve(with_feed=False)
        self.assertIsNone(context.feed)
        self.assertNotIn("feed", format_infinifi_oracle_prompt([context]))

    def test_usdc_scale_reads_as_parity(self) -> None:
        # IOracle convention: a 6-decimal stable at parity is quoted at 1e30.
        context = OracleAssignmentContext(ACCOUNTING, USDC, "USDC", 6, ORACLE, 10**30)
        self.assertIn("= `1` reference units", format_infinifi_oracle_report([context], 1, {}))

    def test_removing_an_oracle_is_not_priced(self) -> None:
        call = _call("setOracle", ("address", REUSD), ("address", ZERO_ADDRESS))
        with patch.object(infinifi_oracle_context, "exposes") as probe:
            self.assertEqual(resolve_infinifi_oracle_context("infinifi", 1, [(ACCOUNTING, call)]), [])
        probe.assert_not_called()

    def test_other_protocol_or_chain_resolves_nothing(self) -> None:
        call = _call("setOracle", ("address", REUSD), ("address", ORACLE))
        self.assertEqual(resolve_infinifi_oracle_context("3jane", 1, [(ACCOUNTING, call)]), [])
        self.assertEqual(resolve_infinifi_oracle_context("infinifi", 8453, [(ACCOUNTING, call)]), [])

    def test_feed_normalization_is_described(self) -> None:
        feed = FeedDetails(FEED, "", "X / USD", 3600, None, None, normalization=10**10, divide=False)
        self.assertIn("answer multiplied by 10,000,000,000", feed.describe())
        self.assertIn("(unverified, ", feed.describe())


if __name__ == "__main__":
    unittest.main()
