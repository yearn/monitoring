"""Peak-drawdown gate deciding whether a share-price decrease may trigger emergency dispatch."""

from collections.abc import Callable

BPS_DENOMINATOR = 10_000


def peak_drawdown_dispatch_eligible[T: (int, float)](
    previous: T,
    current: T,
    min_decrease_bps: int,
    read_peak: Callable[[], T],
    write_peak: Callable[[T], None],
) -> bool:
    """Return whether a share-price decrease warrants dispatch, and advance the stored peak.

    Dust-sized decreases (ERC-4626 rounding, float noise) must not move funds, so
    the decrease is measured from the peak since the last dispatch-eligible alert
    rather than from the previous run: a loss spread over many small steps still
    dispatches once it adds up.

    Args:
        previous: Share price from the previous run (0 when unknown).
        current: Share price from this run.
        min_decrease_bps: Smallest drawdown from the peak that dispatches, in basis points.
        read_peak: Returns the stored peak (0 when absent).
        write_peak: Persists a new peak.

    Returns:
        True when ``current`` fell below ``previous`` and the drawdown reaches
        ``min_decrease_bps``. A drop to zero is a complete loss and always qualifies.
    """
    stored_peak = read_peak()
    peak = max(stored_peak, previous)
    eligible = current < previous and (peak - current) * BPS_DENOMINATOR >= peak * min_decrease_bps
    # After a dispatch-eligible alert the next leg is measured from the new level.
    new_peak = current if eligible else max(peak, current)
    if new_peak != stored_peak:
        write_peak(new_peak)
    return eligible
