"""Tests for the Yearn Auction protocol-context adapter."""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import yearn_auction_context
from utils.llm.yearn_auction_context import (
    format_yearn_auction_prompt,
    format_yearn_auction_report,
    resolve_yearn_auction_context,
)

# Strategist Safe nonce 3372: two Grove compounders, values read at block 26226663.
BIG = "0xe060B80438771f13078048c3b0d930efECA6E622"
SMALL = "0x47c640fDA687B7D20d50D4464e302e36D3D312Ed"
BIG_AUCTION = "0xF3318007c41539b691b49D3e898560AB5BB966A3"
SMALL_AUCTION = "0x645ecF8D3355A8ce243B6706bB685F66B3fEe71A"
GROVE = "0xB30FE1Cf884B48a22a50D22a9282004F2c5E9406"
USDS = "0xdC035D45d973E3EC169d2276DDab16f1e407384F"
GOV = "0x16388463d60FFE0661Cf7F1f31a7D658aC790ff7"
PLAIN = "0x1111111111111111111111111111111111111111"
NOW = 1_792_412_580
E18 = 10**18

BALANCES = {
    (GROVE, BIG_AUCTION): 698_582_228783993151690507,
    (GROVE, BIG): 0,
    (GROVE, SMALL_AUCTION): 0,
    (GROVE, SMALL): 0,
    (USDS, BIG): 0,
    (USDS, SMALL): 879_757690781347611660,
}


def _auction(strategy: str, kicked: int) -> dict[str, object]:
    return {
        "want()": USDS,
        "startingPrice()": 10_000 * E18,
        "getAllEnabledAuctions()": (GROVE.lower(),),
        "receiver()": strategy,
        "governance()": strategy,
        "governanceOnlyKick()": True,
        "version()": "1.0.5",
        "minimumPrice()": 6 * 10**15,
        "stepDecayRate()": 30,
        "stepDuration()": 60,
        "auctionLength()": 86_400,
        "kicked(address)": kicked,
        "isActive(address)": False,
        "available(address)": 0,
    }


def _strategy(auction: str, claimable: int, total_assets: int, min_amount: int) -> dict[str, object]:
    return {
        "auction()": auction,
        "claimableRewards()": claimable,
        "totalAssets()": total_assets,
        "minAmountToSell(address)": min_amount,
    }


CHAIN: dict[str, dict[str, object]] = {
    BIG: _strategy(BIG_AUCTION, 284_291_941468462897306235, 9_756_926_290000000000000000, 30_000 * E18),
    SMALL: _strategy(SMALL_AUCTION, 142_141_890111750914867167, 1_001_639 * E18, 40_000 * E18),
    BIG_AUCTION: _auction(BIG, 1_792_246_835),
    SMALL_AUCTION: _auction(SMALL, 0),
}

STRATEGY_SOURCE = """
contract GroveCompounder {
    /// @notice Set the minimum GROVE auction price required by the strategy.
    function setMinimumAuctionPrice(uint256 _minimumAuctionPrice) external onlyManagement {
        Auction(auction).setMinimumPrice(_minimumAuctionPrice);
        minimumAuctionPrice = _minimumAuctionPrice;
    }

    function _harvestAndReport() internal override returns (uint256 _totalAssets) {
        _claimRewards();
        uint256 rewardsBalance = balanceOfRewards();
        if (rewardsBalance > minAmountToSell[REWARDS_TOKEN]) {
            _kickAuction(REWARDS_TOKEN, rewardsBalance);
        }
        _totalAssets = balanceOfStake() + balanceOfAsset();
    }

    /// @notice Only records the value; the Auction is left alone.
    function setAuctionStartingPriceHint(uint256 _price) external onlyManagement {
        startingPriceHint = _price;
    }
}
"""


def _call_view(client: object, address: str, signature: str, output: str, args: tuple = ()) -> object | None:
    if signature == "balanceOf(address)":
        return BALANCES.get((address, args[0]))
    return CHAIN.get(address, {}).get(signature)


def _metadata(chain_id: int, address: str) -> ERC20Metadata | None:
    return {GROVE: ERC20Metadata("GROVE", 18), USDS: ERC20Metadata("USDS", 18)}.get(address)


def _call(name: str, signature: str, *params: tuple[str, object]) -> DecodedCall:
    return DecodedCall(name, signature, list(params))


SET_FLOOR = _call("setMinimumAuctionPrice", "setMinimumAuctionPrice(uint256)", ("uint256", 5_036_110_000_000_000))
REPORT = _call("report", "report()")
PRICES = {f"ethereum:{GROVE}": Decimal("0.005751304992410269"), f"ethereum:{USDS}": Decimal("1")}


@patch.object(yearn_auction_context, "get_contract_label", return_value="")
@patch.object(yearn_auction_context, "fetch_prices", return_value=PRICES)
@patch.object(yearn_auction_context, "fetch_function_input_names", return_value=["_minimumAuctionPrice"])
@patch.object(yearn_auction_context, "fetch_source", return_value=("GroveCompounder", STRATEGY_SOURCE))
@patch.object(yearn_auction_context, "fetch_erc20_metadata", side_effect=_metadata)
@patch.object(yearn_auction_context, "call_view", side_effect=_call_view)
@patch.object(yearn_auction_context, "ChainManager")
class TestYearnAuctionContext(unittest.TestCase):
    def _resolve(self, mock_cm: MagicMock, calls: list[tuple[str, DecodedCall]]) -> list:
        mock_cm.get_client.return_value.eth.get_block.return_value = {"timestamp": NOW}
        return resolve_yearn_auction_context("YEARN_MS", 1, calls)

    def test_grove_batch_lowers_the_auction_floor_and_kicks_the_whole_balance(
        self, mock_cm: MagicMock, *_: MagicMock
    ) -> None:
        contexts = self._resolve(mock_cm, [(BIG, SET_FLOOR), (BIG, REPORT), (SMALL, SET_FLOOR), (SMALL, REPORT)])
        self.assertEqual([c.address for c in contexts], [BIG_AUCTION, SMALL_AUCTION])
        prompt = format_yearn_auction_prompt(contexts)

        self.assertIn(f"Yearn Auction {BIG_AUCTION} for strategy {BIG} (version 1.0.5)", prompt)
        self.assertIn(f"proceeds go to receiver {BIG} (the strategy itself)", prompt)
        self.assertIn("starts at startingPrice = 10,000.00 USDS and drops 0.3% (30 bps) every 1m", prompt)
        self.assertIn("698,582.22 GROVE unsold in the auction from the lot kicked", prompt)
        self.assertIn("Market (DeFiLlama, live): 1 GROVE ≈ 0.005751 USDS; minimumPrice is 4.32% above market", prompt)
        # The strategy setter is the Auction's floor, read from its verified body.
        self.assertIn(
            "setMinimumAuctionPrice on the strategy forwards to Auction.setMinimumPrice (verified body): "
            "minimumPrice 0.006 USDS per GROVE → 0.00503611 USDS per GROVE (-16.06%)",
            prompt,
        )
        self.assertIn("the floor moves from 4.32% above market to 12.44% below market", prompt)
        # The lot is the leftover plus the fresh claim, priced at the floor set earlier in the batch.
        self.assertIn("report: kicks the GROVE auction. The lot is the auction's whole balance", prompt)
        self.assertIn(
            "≈ 982,874.17 GROVE (698,582.22 GROVE already in the auction + 0 GROVE the strategy holds + "
            "284,291.94 GROVE it claims",
            prompt,
        )
        self.assertIn("(0.00503611 USDS per GROVE) the full lot clears for at least 4,949.86 USDS", prompt)
        self.assertIn("142,141.89 GROVE it claims", prompt)
        self.assertIn("no lot pending (kicked = 0: never kicked, or the last lot sold out)", prompt)
        self.assertIn("The strategy holds 879.75 USDS idle", prompt)
        self.assertIn("that is 0.088% of totalAssets booked as profit", prompt)

        report = format_yearn_auction_report(contexts, 1, {})
        self.assertIn(f"[`{BIG_AUCTION}`](https://etherscan.io/address/{BIG_AUCTION})", report)
        self.assertIn("- **Proposed:**", report)

    def test_report_before_the_setter_kicks_at_the_old_floor(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        (context,) = self._resolve(mock_cm, [(BIG, REPORT), (BIG, SET_FLOOR)])
        kick, setter = context.proposal_lines(str)
        self.assertIn("(0.006 USDS per GROVE) the full lot clears for at least 5,897.25 USDS", kick)
        self.assertIn("minimumPrice 0.006 USDS per GROVE → 0.00503611", setter)

    def test_small_balance_below_min_amount_likely_does_not_kick(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        CHAIN[SMALL]["claimableRewards()"] = 10_000 * E18
        try:
            (context,) = self._resolve(mock_cm, [(SMALL, REPORT)])
        finally:
            CHAIN[SMALL]["claimableRewards()"] = 142_141_890111750914867167
        self.assertIn("≈ 10,000 GROVE now, so it likely does not kick", context.proposal_lines(str)[0])

    def test_report_kick_is_hedged_when_the_harvest_is_not_verified(
        self, mock_cm: MagicMock, *mocks: MagicMock
    ) -> None:
        fetch_source = mocks[2]
        fetch_source.return_value = None
        (context,) = self._resolve(mock_cm, [(BIG, REPORT)])
        self.assertIn(
            "report: may kick the GROVE auction (its harvest is not confirmed", context.proposal_lines(str)[0]
        )

    def test_setter_whose_body_does_not_forward_is_ignored(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        hint = _call("setAuctionStartingPriceHint", "setAuctionStartingPriceHint(uint256)", ("uint256", 1))
        (context,) = self._resolve(mock_cm, [(BIG, hint), (BIG, REPORT)])
        self.assertEqual([entry.kind for entry in context.calls], ["kick"])

    def test_direct_auction_calls(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        calls = [
            (BIG_AUCTION, _call("setStartingPrice", "setStartingPrice(uint256)", ("uint256", 5_000 * E18))),
            (BIG_AUCTION, _call("setReceiver", "setReceiver(address)", ("address", GOV))),
            (BIG_AUCTION, _call("sweep", "sweep(address)", ("address", GROVE))),
        ]
        (context,) = self._resolve(mock_cm, calls)
        # The receiver answers auction() with this auction, so it is the strategy.
        self.assertEqual(context.strategy, BIG)
        starting, receiver, sweep = context.proposal_lines(str)
        self.assertIn("startingPrice 10,000.00 USDS → 5,000.00 USDS for the whole lot", starting)
        self.assertIn(f"receiver {BIG} → {GOV}. Sale proceeds stop going to the strategy.", receiver)
        self.assertIn("sweep: sends the auction's entire balance of GROVE (698,582.22 GROVE) to the caller", sweep)

    def test_strategy_without_an_auction_resolves_nothing(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        self.assertEqual(self._resolve(mock_cm, [(PLAIN, REPORT)]), [])

    def test_unrelated_calls_make_no_rpc(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        calls = [(BIG, _call("setKeeper", "setKeeper(address)", ("address", GOV)))]
        self.assertEqual(resolve_yearn_auction_context("YEARN_MS", 1, calls), [])
        mock_cm.get_client.assert_not_called()

    def test_market_price_is_optional(self, mock_cm: MagicMock, *mocks: MagicMock) -> None:
        fetch_prices = mocks[4]
        fetch_prices.side_effect = RuntimeError("down")
        (context,) = self._resolve(mock_cm, [(BIG, SET_FLOOR)])
        prompt = format_yearn_auction_prompt([context])
        self.assertNotIn("Market (DeFiLlama", prompt)
        self.assertIn("→ 0.00503611 USDS per GROVE (-16.06%).", prompt)


if __name__ == "__main__":
    unittest.main()
