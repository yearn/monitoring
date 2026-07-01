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
    next_cached_round,
)
from utils.alert import AlertSeverity
from utils.chainlink import FeedReading, RoundData
from utils.dispatch import DISPATCHABLE_PROTOCOLS
from utils.pegged_assets import ChainlinkFeed, PeggedAsset, PegTarget, RateOracle, get_asset

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
        # cbBTC has no dispatchable owner: protocol is "pegs", channel override empty.
        self.assertEqual(alert.channel, "pegs")
        self.assertEqual(alert.protocol, "coinbase")

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
        # cbBTC is downside_only; oracle $58,200 vs $60,000 peg = -3% < -2% tolerance
        obs = _cbbtc_obs(reading=_reading(get_asset("cbBTC").chainlink_feed.address, 58_200 * 10**8))
        alert = check_peg_deviation(obs)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)

    def test_upside_does_not_fire_for_downside_only(self):
        # cbBTC can legitimately trade above BTC; +5% upside must NOT alert.
        obs = _cbbtc_obs(reading=_reading(get_asset("cbBTC").chainlink_feed.address, 63_000 * 10**8))
        self.assertIsNone(check_peg_deviation(obs))


class TestMarketDivergence(unittest.TestCase):
    def test_aligned_ok(self):
        self.assertIsNone(check_market_divergence(_cbbtc_obs()))

    def test_within_feed_band_is_quiet(self):
        # The production false positive: cbBTC feed band is 2%, so the oracle lagging
        # the live market by ~1.14% is normal update lag, NOT an anomaly.
        obs = _cbbtc_obs(
            reading=_reading(get_asset("cbBTC").chainlink_feed.address, 59_211 * 10**8),
            market_price_usd=Decimal("58544"),
        )
        self.assertIsNone(check_market_divergence(obs))  # ~1.14% < 2% band + 0.5% buffer

    def test_volatile_feed_uses_wider_buffer(self):
        # cbBTC overrides the buffer to 0.5%; band 2% -> trigger 2.5%.
        addr = get_asset("cbBTC").chainlink_feed.address
        quiet = _cbbtc_obs(reading=_reading(addr, 61_320 * 10**8), market_price_usd=Decimal("60000"))
        self.assertIsNone(check_market_divergence(quiet))  # 2.2% < 2.5%
        fires = _cbbtc_obs(reading=_reading(addr, 61_560 * 10**8), market_price_usd=Decimal("60000"))
        self.assertIsNotNone(check_market_divergence(fires))  # 2.6% > 2.5%

    def test_stable_feed_uses_tight_default_buffer(self):
        # USDC: 0.25% band + 0.25% default buffer -> 0.5% trigger (no per-feed override).
        usdc = get_asset("USDC")

        def usdc_obs(oracle_usd: str, market_usd: str) -> OracleObservation:
            return OracleObservation(
                asset=usdc,
                reading=_reading(usdc.chainlink_feed.address, int(Decimal(oracle_usd) * 10**8)),
                peg_price_usd=Decimal("1"),
                quote_price_usd=Decimal("1"),
                now=NOW,
                market_price_usd=Decimal(market_usd),
            )

        self.assertIsNone(check_market_divergence(usdc_obs("1.004", "1")))  # 0.4% < 0.5%
        self.assertIsNotNone(check_market_divergence(usdc_obs("1.006", "1")))  # 0.6% > 0.5%

    def test_forced_divergence_fires(self):
        # oracle $60,100 vs market $50,000 ~ +20% >> 2% band + buffer
        obs = _cbbtc_obs(market_price_usd=Decimal("50000"))
        alert = check_market_divergence(obs)
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
            protocol="pegs",
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


class TestDispatchRouting(unittest.TestCase):
    """Alerts must carry the asset's logical protocol so emergency dispatch can fire."""

    def test_owned_asset_uses_dispatchable_protocol(self):
        usde = get_asset("USDe")  # owner "ethena", peg USD, 3% tolerance
        obs = OracleObservation(
            asset=usde,
            reading=_reading(usde.chainlink_feed.address, 90 * 10**6),  # $0.90 -> off peg
            peg_price_usd=Decimal("1"),
            quote_price_usd=Decimal("1"),
            now=NOW,
            market_price_usd=Decimal("0.90"),
        )
        alert = check_peg_deviation(obs)
        self.assertIsNotNone(alert)
        # protocol (not channel) carries the owner; dispatch keys off alert.protocol.
        self.assertEqual(alert.protocol, "ethena")
        self.assertIn(alert.protocol, DISPATCHABLE_PROTOCOLS)


class TestRoundMetadataGate(unittest.TestCase):
    """Feeds that don't report reliable round metadata skip staleness + round checks."""

    def _flat_obs(self) -> OracleObservation:
        # Feed flagged reports_round_metadata=False, with a reading that would
        # otherwise trip both staleness (updatedAt=0) and round-health (round_id=0).
        asset = PeggedAsset(
            name="fakeFlat",
            defillama_key="ethereum:0x0000000000000000000000000000000000000002",
            protocol="pegs",
            peg=PegTarget.USD,
            depeg_pct=Decimal("0.02"),
            chainlink_feed=ChainlinkFeed("0xFeed", HEARTBEAT, "FLAT/USD", reports_round_metadata=False),
        )
        return OracleObservation(
            asset=asset,
            reading=_reading("0xFeed", 10**8, round_id=0, updated_at=0, answered_in_round=0),
            peg_price_usd=Decimal("1"),
            quote_price_usd=Decimal("1"),
            now=NOW,
            market_price_usd=Decimal("1"),
        )

    def test_unreliable_feed_skips_staleness_and_round(self):
        # On-peg, aligned price -> only peg/divergence run, and both pass -> no alerts.
        self.assertEqual(evaluate_chainlink_asset(self._flat_obs()), [])

    def test_unreliable_feed_still_flags_peg_deviation(self):
        # Off-peg must still fire even when round metadata is untrusted.
        obs = OracleObservation(
            asset=self._flat_obs().asset,
            reading=_reading("0xFeed", 90 * 10**6, round_id=0, updated_at=0),  # $0.90
            peg_price_usd=Decimal("1"),
            quote_price_usd=Decimal("1"),
            now=NOW,
            market_price_usd=Decimal("0.90"),
        )
        alerts = evaluate_chainlink_asset(obs)
        self.assertTrue(any("off peg" in a.message for a in alerts))


class TestNextCachedRound(unittest.TestCase):
    def _rd(self, round_id: int, answer: int = 60_100 * 10**8, updated_at: int = NOW - 100) -> RoundData:
        return RoundData(round_id, answer, updated_at, updated_at, round_id)

    def test_first_run_caches_current(self):
        self.assertEqual(next_cached_round(None, self._rd(100)), 100)

    def test_advances_on_increase(self):
        self.assertEqual(next_cached_round(100, self._rd(101)), 101)

    def test_keeps_high_water_mark_on_regression(self):
        # Backwards round must NOT lower the cached baseline (no poisoning).
        self.assertEqual(next_cached_round(100, self._rd(99)), 100)

    def test_broken_round_does_not_poison_even_if_higher(self):
        # answer == 0 -> unhealthy; keep last-good rather than caching a broken round.
        self.assertEqual(next_cached_round(100, self._rd(200, answer=0)), 100)


if __name__ == "__main__":
    unittest.main()
