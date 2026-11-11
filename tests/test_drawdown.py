"""Tests for utils/drawdown.py."""

import pytest

from utils.drawdown import peak_drawdown_dispatch_eligible


class _Peak:
    """In-memory stand-in for a cached peak."""

    def __init__(self, value: float = 0) -> None:
        self.value = value
        self.writes: list[float] = []

    def read(self) -> float:
        return self.value

    def write(self, value: float) -> None:
        self.value = value
        self.writes.append(value)

    def check(self, previous: float, current: float, min_decrease_bps: int = 1) -> bool:
        return peak_drawdown_dispatch_eligible(previous, current, min_decrease_bps, self.read, self.write)


def test_increase_is_not_eligible_and_raises_the_peak() -> None:
    peak = _Peak(100)
    assert peak.check(100, 110) is False
    assert peak.value == 110


def test_threshold_is_inclusive() -> None:
    assert _Peak().check(10_000, 9_999) is True
    assert _Peak().check(100_000, 99_991) is False


@pytest.mark.parametrize("previous,current", [(1.0, 0.9999), (2.0, 1.9998), (1.1, 1.09989)])
def test_float_threshold_is_inclusive(previous: float, current: float) -> None:
    """Dispatch an exact one-basis-point loss despite binary float rounding."""
    assert _Peak().check(previous, current) is True


def test_float_just_below_threshold_is_not_eligible() -> None:
    """Keep the threshold strict instead of accepting nearby sub-threshold losses."""
    assert _Peak().check(1.0, 0.9999000000000001) is False


def test_integer_just_below_threshold_is_not_eligible() -> None:
    """Preserve wei precision when a loss misses the threshold by one wei."""
    previous = 10**18
    assert _Peak().check(previous, previous - previous // 10_000 + 1) is False


def test_cumulative_drawdown_from_peak_is_eligible() -> None:
    peak = _Peak(100_000)
    assert peak.check(100_000, 99_995) is False
    assert peak.value == 100_000
    assert peak.check(99_995, 99_990) is True


def test_eligible_alert_resets_the_peak_to_the_new_level() -> None:
    peak = _Peak(100_000)
    assert peak.check(100_000, 90_000) is True
    assert peak.value == 90_000


def test_drop_to_zero_is_eligible_for_int_and_float() -> None:
    assert _Peak().check(10**18, 0) is True
    assert _Peak().check(1.05, 0.0) is True


def test_unknown_previous_is_not_eligible() -> None:
    peak = _Peak()
    assert peak.check(0, 100) is False
    assert peak.check(0, 0) is False
    assert peak.writes == [100]


def test_unchanged_peak_is_not_rewritten() -> None:
    peak = _Peak(100)
    peak.check(100, 100)
    assert peak.writes == []


def test_exact_integers_are_kept_at_18_decimals() -> None:
    peak = _Peak(1_100_000_000_000_000_001)
    assert peak.check(1_100_000_000_000_000_001, 1_100_000_000_000_000_000) is False
    assert peak.value == 1_100_000_000_000_000_001
