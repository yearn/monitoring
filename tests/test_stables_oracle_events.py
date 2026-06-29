import unittest

from protocols.stables.oracle_events import (
    OracleRound,
    detect_anomalies,
    next_cursor,
    parse_round,
)
from utils.alert import AlertSeverity
from utils.dispatch import DISPATCHABLE_PROTOCOLS
from utils.pegged_assets import get_asset

USDE = get_asset("USDe")  # protocol "ethena" (dispatchable), USD/USD feed, 24h heartbeat
FEED = USDE.chainlink_feed
AGG = "0xaggregator"
HB = FEED.heartbeat  # 86_400


def _round(round_id: int, answer: int, updated_at: int, *, block_ts: int | None = None) -> OracleRound:
    return OracleRound(
        aggregator=AGG,
        round_id=round_id,
        answer=answer,
        updated_at=updated_at,
        block_timestamp=block_ts if block_ts is not None else updated_at,
        tx_hash="0xtx",
        chain_id=1,
    )


def _detect(rounds, since_ts=0, **kw):
    return detect_anomalies(USDE, FEED, rounds, since_ts=since_ts, **kw)


class TestNoAnomaly(unittest.TestCase):
    def test_healthy_stream_is_quiet(self):
        rounds = [
            _round(100, 100_000_000, 1_000),
            _round(101, 100_010_000, 1_000 + HB - 10),  # small move, within heartbeat
        ]
        self.assertEqual(_detect(rounds), [])

    def test_single_round_has_no_pair(self):
        self.assertEqual(_detect([_round(100, 100_000_000, 1_000)]), [])


class TestJump(unittest.TestCase):
    def test_large_jump_fires_high(self):
        rounds = [_round(100, 100_000_000, 1_000), _round(101, 120_000_000, 1_500)]  # +20%
        alerts = _detect(rounds, jump_threshold=0.10)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.HIGH)
        self.assertIn("jump", alerts[0].message)

    def test_small_move_below_threshold_quiet(self):
        rounds = [_round(100, 100_000_000, 1_000), _round(101, 105_000_000, 1_500)]  # +5%
        self.assertEqual(_detect(rounds, jump_threshold=0.10), [])


class TestHeartbeatGap(unittest.TestCase):
    def test_gap_beyond_heartbeat_fires(self):
        rounds = [_round(100, 100_000_000, 1_000), _round(101, 100_000_001, 1_000 + HB + 5_000)]
        alerts = _detect(rounds, heartbeat_buffer=600)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.HIGH)
        self.assertIn("missed-heartbeat", alerts[0].message)

    def test_within_heartbeat_quiet(self):
        rounds = [_round(100, 100_000_000, 1_000), _round(101, 100_000_001, 1_000 + HB)]
        self.assertEqual(_detect(rounds, heartbeat_buffer=600), [])


class TestSequence(unittest.TestCase):
    def test_non_increasing_round_id_is_critical(self):
        # newer block carries a roundId that did not advance
        rounds = [_round(101, 100_000_000, 1_000), _round(101, 100_000_000, 1_500)]
        alerts = _detect(rounds)
        self.assertTrue(any(a.severity == AlertSeverity.CRITICAL for a in alerts))
        self.assertTrue(any("sequence anomaly" in a.message for a in alerts))


class TestDedup(unittest.TestCase):
    def test_rounds_at_or_before_cursor_not_realerted(self):
        # The jump happens between round 100->101; both at/below the cursor.
        rounds = [_round(100, 100_000_000, 1_000), _round(101, 130_000_000, 1_500)]
        self.assertEqual(_detect(rounds, since_ts=1_500), [])  # cur block_ts == cursor -> skipped

    def test_only_new_rounds_alert_with_prior_context(self):
        # round 101 is context (<= cursor); the anomaly on the new round 102 fires once.
        rounds = [
            _round(101, 100_000_000, 1_500),
            _round(102, 130_000_000, 2_000),  # +30% on the new round
        ]
        alerts = _detect(rounds, since_ts=1_500)
        self.assertEqual(len(alerts), 1)
        self.assertIn("jump", alerts[0].message)


class TestRouting(unittest.TestCase):
    def test_alert_uses_dispatchable_protocol(self):
        rounds = [_round(100, 100_000_000, 1_000), _round(101, 130_000_000, 1_500)]
        alert = _detect(rounds)[0]
        self.assertEqual(alert.protocol, "ethena")
        self.assertIn(alert.protocol, DISPATCHABLE_PROTOCOLS)
        self.assertEqual(alert.channel, "")


class TestCursor(unittest.TestCase):
    def test_advances_to_max_block_timestamp(self):
        rounds = [_round(100, 1, 1_000), _round(101, 1, 2_500)]
        self.assertEqual(next_cursor(1_200, rounds), 2_500)

    def test_never_regresses_below_prev(self):
        rounds = [_round(100, 1, 1_000)]
        self.assertEqual(next_cursor(5_000, rounds), 5_000)

    def test_empty_rounds_keeps_prev(self):
        self.assertEqual(next_cursor(5_000, []), 5_000)


class TestParseRound(unittest.TestCase):
    def test_maps_graphql_row(self):
        row = {
            "aggregatorAddress": "0xABCDEF",
            "roundId": "42",
            "current": "99980000",
            "updatedAt": "1700000000",
            "blockTimestamp": "1700000005",
            "transactionHash": "0xdead",
            "chainId": 1,
        }
        rnd = parse_round(row)
        self.assertEqual(rnd.aggregator, "0xabcdef")  # lowercased
        self.assertEqual(rnd.round_id, 42)
        self.assertEqual(rnd.answer, 99_980_000)
        self.assertEqual(rnd.updated_at, 1_700_000_000)
        self.assertEqual(rnd.block_timestamp, 1_700_000_005)


if __name__ == "__main__":
    unittest.main()
