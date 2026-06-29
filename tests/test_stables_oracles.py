import unittest
from decimal import Decimal

from protocols.stables.oracles import (
    OracleObservation,
    check_market_divergence,
    check_peg_deviation,
    check_rate_oracle,
    check_round_health,
    check_staleness,
    evaluate_chainlink_asset,
)
from utils.alert import AlertSeverity
from utils.chainlink import FeedReading, RoundData
from utils.pegged_assets import PeggedAsset, PegTarget, RateOracle, get_asset

NOW = 2_000_000
HEARTBEAT = 86_400  # matches registry _STABLE_HEARTBEAT


def _reading(
    address: str,
    answer: int,
    *,
    decimals: int = 8,
    round_id: int = 100,
    updated_at: int = NOW - 100,
    answered_in_round: int = 100,
) -> FeedReading:
    rd = RoundData(
        round_id=round_id,
        answer=answer,
        started_at=updated_at,
        updated_at=updated_at,
        answered_in_round=answered_in_round,
    )
    return FeedReading(address=address, round_data=rd, decimals=decimals)


def _cbbtc_obs(**overrides) -> OracleObservation:
    """A healthy cbBTC (USD-quoted feed, BTC peg) observation; override per test."""
    asset = get_asset("cbBTC")
    defaults = dict(
        asset=asset,
        reading=_reading(asset.chainlink_feed.address, 60_100 * 10**8),  # $60,100
        peg_price_usd=Decimal("60000"),
        quote_price_usd=Decimal("1"),  # USD-quoted feed
        now=NOW,
        market_price_usd=Decimal("60100"),
        prev_round_id=99,
    )
    defaults.update(overrides)
    return OracleObservation(**defaults)


class TestHealthyAsset(unittest.TestCase):
    def test_no_alerts_when_healthy(self):
        self.assertEqual(evaluate_chainlink_asset(_cbbtc_obs()), [])


class TestStaleness(unittest.TestCase):
    def test_fresh_feed_ok(self):
        self.assertIsNone(check_staleness(_cbbtc_obs(), buffer=600))

    def test_forced_stale_fires(self):
        stale = _cbbtc_obs(
            reading=_reading(
                get_asset("cbBTC").chainlink_feed.address, 60_100 * 10**8, updated_at=NOW - (HEARTBEAT + 1000)
            )
        )
        alert = check_staleness(stale, buffer=600)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)
        self.assertEqual(alert.channel, "pegs")

    def test_zero_updated_at_is_stale(self):
        obs = _cbbtc_obs(reading=_reading(get_asset("cbBTC").chainlink_feed.address, 60_100 * 10**8, updated_at=0))
        self.assertIsNotNone(check_staleness(obs))


class TestRoundHealth(unittest.TestCase):
    def test_healthy_round(self):
        self.assertIsNone(check_round_health(_cbbtc_obs()))

    def test_non_positive_answer_is_critical(self):
        obs = _cbbtc_obs(reading=_reading(get_asset("cbBTC").chainlink_feed.address, 0))
        alert = check_round_health(obs)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)

    def test_lagging_answered_in_round_is_high(self):
        addr = get_asset("cbBTC").chainlink_feed.address
        obs = _cbbtc_obs(reading=_reading(addr, 60_100 * 10**8, round_id=100, answered_in_round=99))
        alert = check_round_health(obs)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)

    def test_roundid_backwards_is_critical(self):
        obs = _cbbtc_obs(prev_round_id=200)  # current round_id is 100
        alert = check_round_health(obs)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)


class TestPegDeviation(unittest.TestCase):
    def test_within_tolerance_ok(self):
        self.assertIsNone(check_peg_deviation(_cbbtc_obs()))

    def test_off_peg_fires(self):
        # oracle $63,000 vs $60,000 peg = +5% > cbBTC 2% tolerance
        obs = _cbbtc_obs(reading=_reading(get_asset("cbBTC").chainlink_feed.address, 63_000 * 10**8))
        alert = check_peg_deviation(obs)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)


class TestMarketDivergence(unittest.TestCase):
    def test_aligned_ok(self):
        self.assertIsNone(check_market_divergence(_cbbtc_obs(), threshold=Decimal("0.01")))

    def test_forced_divergence_fires(self):
        # oracle $60,100 vs market $50,000 ~ +20%
        obs = _cbbtc_obs(market_price_usd=Decimal("50000"))
        alert = check_market_divergence(obs, threshold=Decimal("0.01"))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)

    def test_missing_market_price_skips(self):
        self.assertIsNone(check_market_divergence(_cbbtc_obs(market_price_usd=None)))


class TestQuoteConversion(unittest.TestCase):
    def test_btc_quoted_feed_scales_to_usd(self):
        # LBTC/BTC feed answer 1.004 BTC, BTC at $60,000 -> oracle $60,240
        lbtc = get_asset("LBTC")
        obs = OracleObservation(
            asset=lbtc,
            reading=_reading(lbtc.chainlink_feed.address, 100_400_000),  # 1.004 * 1e8
            peg_price_usd=Decimal("60000"),
            quote_price_usd=Decimal("60000"),  # feed quotes in BTC
            now=NOW,
            market_price_usd=Decimal("60240"),
        )
        self.assertEqual(obs.oracle_price_usd, Decimal("60240.000"))
        self.assertEqual(evaluate_chainlink_asset(obs), [])


class TestRateOracle(unittest.TestCase):
    def _asset(self, monotonic: bool = True) -> PeggedAsset:
        return PeggedAsset(
            name="fakeRate",
            defillama_key="ethereum:0x0000000000000000000000000000000000000001",
            channel="pegs",
            peg=PegTarget.USD,
            depeg_pct=Decimal("0.02"),
            rate_oracle=RateOracle(address="0xRate", monotonic=monotonic),
        )

    def test_no_previous_rate_no_alert(self):
        self.assertEqual(check_rate_oracle(self._asset(), current_rate=10**18, prev_rate=None), [])

    def test_monotonic_decrease_is_critical(self):
        alerts = check_rate_oracle(self._asset(monotonic=True), current_rate=9 * 10**17, prev_rate=10**18)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.CRITICAL)

    def test_large_increase_is_high(self):
        alerts = check_rate_oracle(self._asset(), current_rate=12 * 10**17, prev_rate=10**18, threshold=Decimal("0.05"))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.HIGH)

    def test_small_change_no_alert(self):
        self.assertEqual(
            check_rate_oracle(self._asset(), current_rate=101 * 10**16, prev_rate=10**18, threshold=Decimal("0.05")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
