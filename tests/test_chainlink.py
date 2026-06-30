import unittest
from decimal import Decimal

from utils.chainlink import (
    FeedReading,
    RoundData,
    scale_price,
)


def _round(
    round_id: int = 10,
    answer: int = 100_000_000,
    started_at: int = 1_000,
    updated_at: int = 1_000,
    answered_in_round: int = 10,
) -> RoundData:
    return RoundData(round_id, answer, started_at, updated_at, answered_in_round)


class TestScalePrice(unittest.TestCase):
    def test_scales_by_decimals(self):
        self.assertEqual(scale_price(100_000_000, 8), Decimal("1"))

    def test_scales_fractional(self):
        self.assertEqual(scale_price(99_960_043, 8), Decimal("0.99960043"))

    def test_zero_decimals_is_identity(self):
        self.assertEqual(scale_price(42, 0), Decimal("42"))

    def test_negative_decimals_raises(self):
        with self.assertRaises(ValueError):
            scale_price(1, -1)


class TestRoundData(unittest.TestCase):
    def test_from_tuple_decodes_fields(self):
        rd = RoundData.from_tuple((10, 99_851_375, 900, 1_000, 10))
        self.assertEqual(rd.round_id, 10)
        self.assertEqual(rd.answer, 99_851_375)
        self.assertEqual(rd.updated_at, 1_000)
        self.assertEqual(rd.answered_in_round, 10)

    def test_from_tuple_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            RoundData.from_tuple((1, 2, 3))

    def test_feed_reading_price_uses_decimals(self):
        reading = FeedReading(address="0xabc", round_data=_round(answer=605_044_986_7456), decimals=8)
        self.assertEqual(reading.price, scale_price(605_044_986_7456, 8))


if __name__ == "__main__":
    unittest.main()
