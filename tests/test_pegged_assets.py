import unittest
from decimal import Decimal
from unittest.mock import patch

from utils.pegged_assets import (
    BTC_USD_DEFILLAMA_KEY,
    PEGGED_ASSETS,
    PEGGED_ASSETS_BY_NAME,
    PegTarget,
    get_asset,
    price_deviation,
    resolve_peg_prices,
)

# Asset set the registry must cover per the issue acceptance criteria.
REQUIRED_ASSETS = {"cbBTC", "LBTC", "iUSD", "cUSD", "USDe", "USDC", "USDT", "USDS"}


class TestPriceDeviation(unittest.TestCase):
    def test_no_deviation(self):
        self.assertEqual(price_deviation(Decimal("1"), Decimal("1")), Decimal("0"))

    def test_positive_deviation(self):
        self.assertEqual(price_deviation(Decimal("1.05"), Decimal("1")), Decimal("0.05"))

    def test_negative_deviation(self):
        self.assertEqual(price_deviation(Decimal("0.97"), Decimal("1")), Decimal("-0.03"))

    def test_btc_denominated_deviation(self):
        # asset at 60,300 vs 60,000 BTC peg -> +0.5%
        self.assertEqual(price_deviation(Decimal("60300"), Decimal("60000")), Decimal("0.005"))

    def test_zero_peg_raises(self):
        with self.assertRaises(ValueError):
            price_deviation(Decimal("1"), Decimal("0"))


class TestIsDepegged(unittest.TestCase):
    def test_within_tolerance_is_not_depegged(self):
        usdc = get_asset("USDC")  # depeg_pct = 0.02
        self.assertFalse(usdc.is_depegged(Decimal("0.99"), Decimal("1")))

    def test_beyond_tolerance_is_depegged(self):
        usdc = get_asset("USDC")
        self.assertTrue(usdc.is_depegged(Decimal("0.97"), Decimal("1")))

    def test_at_threshold_is_depegged(self):
        usdc = get_asset("USDC")
        self.assertTrue(usdc.is_depegged(Decimal("1.02"), Decimal("1")))

    def test_btc_asset_uses_peg_price(self):
        cbbtc = get_asset("cbBTC")  # depeg_pct = 0.02, peg = BTC
        self.assertFalse(cbbtc.is_depegged(Decimal("60500"), Decimal("60000")))
        self.assertTrue(cbbtc.is_depegged(Decimal("58000"), Decimal("60000")))


class TestResolvePegPrices(unittest.TestCase):
    def test_usd_only_does_not_hit_network(self):
        with patch("utils.pegged_assets.fetch_prices") as mock_fetch:
            prices = resolve_peg_prices({PegTarget.USD})
        mock_fetch.assert_not_called()
        self.assertEqual(prices, {PegTarget.USD: Decimal(1)})

    def test_btc_fetches_live_price(self):
        with patch("utils.pegged_assets.fetch_prices", return_value={BTC_USD_DEFILLAMA_KEY: Decimal("60000")}):
            prices = resolve_peg_prices({PegTarget.USD, PegTarget.BTC})
        self.assertEqual(prices[PegTarget.USD], Decimal(1))
        self.assertEqual(prices[PegTarget.BTC], Decimal("60000"))

    def test_missing_btc_price_raises(self):
        with patch("utils.pegged_assets.fetch_prices", return_value={}):
            with self.assertRaises(ValueError):
                resolve_peg_prices({PegTarget.BTC})


class TestRegistry(unittest.TestCase):
    def test_covers_required_assets(self):
        self.assertTrue(REQUIRED_ASSETS.issubset(set(PEGGED_ASSETS_BY_NAME)))

    def test_names_are_unique(self):
        names = [a.name for a in PEGGED_ASSETS]
        self.assertEqual(len(names), len(set(names)))

    def test_address_parsed_from_defillama_key(self):
        self.assertEqual(get_asset("USDC").address, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")

    def test_btc_pegged_assets_target_btc(self):
        self.assertEqual(get_asset("cbBTC").peg, PegTarget.BTC)
        self.assertEqual(get_asset("LBTC").peg, PegTarget.BTC)

    def test_get_asset_unknown_raises(self):
        with self.assertRaises(KeyError):
            get_asset("NOPE")

    def test_every_asset_has_positive_depeg_tolerance(self):
        for asset in PEGGED_ASSETS:
            self.assertGreater(asset.depeg_pct, Decimal("0"), asset.name)


if __name__ == "__main__":
    unittest.main()
