"""Chainlink aggregator helpers shared across peg / oracle monitors.

Generalises the inline ``latestRoundData`` handling from ``protocols/ustb/main.py``:
a batched feed reader plus pure helpers for staleness, round sanity checks and
price scaling. Pure helpers take primitive values so they are trivially unit
testable without a chain connection.
"""

from dataclasses import dataclass
from decimal import Decimal

from utils.abi import load_abi
from utils.logger import get_logger
from utils.web3_wrapper import Web3Client

logger = get_logger("chainlink")

# Shared Chainlink AggregatorV3Interface ABI (latestRoundData + decimals).
CHAINLINK_ABI = load_abi("common-abi/ChainlinkAggregator.json")


@dataclass(frozen=True)
class RoundData:
    """Decoded Chainlink ``latestRoundData`` tuple."""

    round_id: int
    answer: int
    started_at: int
    updated_at: int
    answered_in_round: int

    @classmethod
    def from_tuple(cls, data: tuple | list) -> "RoundData":
        """Build a ``RoundData`` from a raw ``latestRoundData`` response.

        Args:
            data: The 5-element tuple/list returned by ``latestRoundData``.

        Raises:
            ValueError: If ``data`` does not have exactly five elements.
        """
        if len(data) != 5:
            raise ValueError(f"latestRoundData expects 5 fields, got {len(data)}: {data!r}")
        return cls(
            round_id=int(data[0]),
            answer=int(data[1]),
            started_at=int(data[2]),
            updated_at=int(data[3]),
            answered_in_round=int(data[4]),
        )


@dataclass(frozen=True)
class FeedReading:
    """A single feed's decoded round data, decimals and scaled price."""

    address: str
    round_data: RoundData
    decimals: int

    @property
    def price(self) -> Decimal:
        """Answer scaled to a human-readable value by the feed's decimals."""
        return scale_price(self.round_data.answer, self.decimals)


# ---------------------------------------------------------------------------
# Pure helpers (no chain connection required — unit tested directly)
# ---------------------------------------------------------------------------


def scale_price(answer: int, decimals: int) -> Decimal:
    """Scale a raw integer answer to a decimal price using the feed decimals.

    Args:
        answer: Raw integer answer from the aggregator.
        decimals: Number of decimals reported by the feed.

    Returns:
        The answer divided by ``10 ** decimals`` as a ``Decimal``.

    Raises:
        ValueError: If ``decimals`` is negative.
    """
    if decimals < 0:
        raise ValueError(f"decimals must be non-negative, got {decimals}")
    return Decimal(answer) / (Decimal(10) ** decimals)


def is_stale(updated_at: int, heartbeat: int, now: int, buffer: int = 0) -> bool:
    """Return ``True`` if a feed has not updated within its heartbeat window.

    Args:
        updated_at: ``updatedAt`` timestamp from the latest round (unix seconds).
        heartbeat: Expected maximum interval between updates (seconds).
        now: Current unix timestamp (seconds).
        buffer: Extra grace period added to the heartbeat before flagging
            staleness (seconds). Defaults to ``0``.

    Returns:
        ``True`` when ``now - updated_at`` exceeds ``heartbeat + buffer``, or when
        ``updated_at`` is non-positive (uninitialised / invalid round).
    """
    if updated_at <= 0:
        return True
    return (now - updated_at) > (heartbeat + buffer)


def round_issues(round_data: RoundData) -> list[str]:
    """Collect Chainlink round sanity-check failures.

    Checks the standard freshness/consistency invariants Chainlink consumers are
    expected to enforce: a positive answer, an initialised round, and an
    ``answeredInRound`` that is not behind the current ``roundId`` (a stale answer
    carried over from an earlier round).

    Args:
        round_data: The decoded round data to validate.

    Returns:
        A list of human-readable problem descriptions; empty when healthy.
    """
    issues: list[str] = []
    if round_data.answer <= 0:
        issues.append(f"non-positive answer ({round_data.answer})")
    if round_data.updated_at <= 0:
        issues.append("round not complete (updatedAt is 0)")
    if round_data.answered_in_round < round_data.round_id:
        issues.append(f"stale round (answeredInRound {round_data.answered_in_round} < roundId {round_data.round_id})")
    return issues


def is_round_healthy(round_data: RoundData) -> bool:
    """Return ``True`` if the round passes all sanity checks in :func:`round_issues`."""
    return not round_issues(round_data)


# ---------------------------------------------------------------------------
# Batched on-chain reader
# ---------------------------------------------------------------------------


def read_feeds(client: Web3Client, feed_addresses: list[str]) -> dict[str, FeedReading]:
    """Read ``latestRoundData`` and ``decimals`` for several feeds in one batch.

    Args:
        client: Connected ``Web3Client`` for the target chain.
        feed_addresses: Chainlink aggregator addresses to read.

    Returns:
        Mapping of feed address to its :class:`FeedReading`, preserving input order.
    """
    if not feed_addresses:
        return {}

    contracts = [client.get_contract(address, CHAINLINK_ABI) for address in feed_addresses]

    with client.batch_requests() as batch:
        for contract in contracts:
            batch.add(contract.functions.latestRoundData())
            batch.add(contract.functions.decimals())
        responses = client.execute_batch(batch)

    readings: dict[str, FeedReading] = {}
    for index, address in enumerate(feed_addresses):
        round_data = RoundData.from_tuple(responses[2 * index])
        decimals = int(responses[2 * index + 1])
        readings[address] = FeedReading(address=address, round_data=round_data, decimals=decimals)
        logger.info("Chainlink feed %s: price=%s decimals=%d", address, readings[address].price, decimals)

    return readings
