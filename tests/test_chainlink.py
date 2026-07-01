import unittest
from decimal import Decimal
from typing import Any, cast

from utils.chainlink import (
    FeedReading,
    RoundData,
    read_feeds,
    scale_price,
)
from utils.web3_wrapper import Web3Client


def _round(
    round_id: int = 10,
    answer: int = 100_000_000,
    started_at: int = 1_000,
    updated_at: int = 1_000,
    answered_in_round: int = 10,
) -> RoundData:
    return RoundData(round_id, answer, started_at, updated_at, answered_in_round)


class _FakeFeedFunctions:
    def __init__(self, address: str) -> None:
        self.address = address

    def latestRoundData(self) -> tuple[str, str]:
        return ("latestRoundData", self.address)

    def decimals(self) -> tuple[str, str]:
        return ("decimals", self.address)


class _FakeContract:
    def __init__(self, address: str) -> None:
        self.functions = _FakeFeedFunctions(address)


class _FakeBatch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> "_FakeBatch":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    def add(self, call: tuple[str, str]) -> None:
        self.calls.append(call)


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.contract_addresses: list[str] = []
        self.batch = _FakeBatch()
        self.executed_calls: list[tuple[str, str]] = []

    def get_contract(self, address: str, _abi: Any) -> _FakeContract:
        self.contract_addresses.append(address)
        return _FakeContract(address)

    def batch_requests(self) -> _FakeBatch:
        return self.batch

    def execute_batch(self, batch: _FakeBatch) -> list[Any]:
        self.executed_calls = list(batch.calls)
        return self.responses


class TestScalePrice(unittest.TestCase):
    def test_scales_by_decimals(self) -> None:
        self.assertEqual(scale_price(100_000_000, 8), Decimal("1"))

    def test_scales_fractional(self) -> None:
        self.assertEqual(scale_price(99_960_043, 8), Decimal("0.99960043"))

    def test_zero_decimals_raises(self) -> None:
        with self.assertRaises(ValueError):
            scale_price(1, 0)

    def test_negative_decimals_raises(self) -> None:
        with self.assertRaises(ValueError):
            scale_price(1, -1)


class TestRoundData(unittest.TestCase):
    def test_from_tuple_decodes_fields(self) -> None:
        rd = RoundData.from_tuple((10, 99_851_375, 900, 1_000, 10))
        self.assertEqual(rd.round_id, 10)
        self.assertEqual(rd.answer, 99_851_375)
        self.assertEqual(rd.updated_at, 1_000)
        self.assertEqual(rd.answered_in_round, 10)

    def test_from_tuple_wrong_length_raises(self) -> None:
        with self.assertRaises(ValueError):
            RoundData.from_tuple((1, 2, 3))

    def test_feed_reading_price_uses_decimals(self) -> None:
        reading = FeedReading(address="0xabc", round_data=_round(answer=605_044_986_7456), decimals=8)
        self.assertEqual(reading.price, scale_price(605_044_986_7456, 8))


class TestReadFeeds(unittest.TestCase):
    def test_reads_round_data_and_decimals_for_each_feed(self) -> None:
        client = _FakeClient(
            [
                (10, 101_000_000, 900, 1_000, 10),
                8,
                (20, 202_000_000, 1_900, 2_000, 20),
                8,
            ]
        )

        readings = read_feeds(cast(Web3Client, client), ["0xFeedA", "0xFeedB"])

        self.assertEqual(client.contract_addresses, ["0xFeedA", "0xFeedB"])
        self.assertEqual(
            client.executed_calls,
            [
                ("latestRoundData", "0xFeedA"),
                ("decimals", "0xFeedA"),
                ("latestRoundData", "0xFeedB"),
                ("decimals", "0xFeedB"),
            ],
        )
        self.assertEqual(readings["0xFeedA"].round_data.answer, 101_000_000)
        self.assertEqual(readings["0xFeedA"].decimals, 8)
        self.assertEqual(readings["0xFeedB"].round_data.updated_at, 2_000)
        self.assertEqual(readings["0xFeedB"].price, Decimal("2.02"))

    def test_empty_feed_list_returns_empty_mapping(self) -> None:
        client = _FakeClient([])

        self.assertEqual(read_feeds(cast(Web3Client, client), []), {})
        self.assertEqual(client.executed_calls, [])


if __name__ == "__main__":
    unittest.main()
