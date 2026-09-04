"""Common formatting helpers for monitoring scripts."""

from decimal import Decimal


def format_decimal_amount(value: Decimal) -> str:
    """Render a normalized token amount: trim trailing zeros, group the integer part.

    Uses ``Decimal`` end-to-end so a 6-decimal amount like ``50_780000`` formats as
    ``50.78`` exactly, with no float rounding error.
    """
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    int_part, _, frac = s.partition(".")
    int_fmt = f"{int(int_part):,}"
    return f"{int_fmt}.{frac}" if frac else int_fmt


def normalize_token_amount(raw: int, decimals: int) -> Decimal:
    """Exact raw → human token amount, independent of the global Decimal context.

    Built by shifting the exponent rather than dividing: division is evaluated
    at ``decimal.getcontext().prec``, and several modules set that globally at
    import time (``utils/defillama.py`` uses 18), which silently truncated
    large 18-decimal amounts depending on which modules happened to be
    imported. Exponent construction is exact and context-free.
    """
    digits = Decimal(abs(raw)).as_tuple().digits
    return Decimal((1 if raw < 0 else 0, digits, -decimals))


def format_with_suffix(number: float) -> str:
    """Format number with K, M, B suffixes for readability.

    Negative values are suffixed on their magnitude, so a shortfall reads as
    "-2.50M" rather than "-2500000.00".
    """
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if magnitude >= 1_000_000_000:
        return f"{sign}{magnitude / 1_000_000_000:.2f}B"
    if magnitude >= 1_000_000:
        return f"{sign}{magnitude / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{sign}{magnitude / 1_000:.2f}K"
    return f"{number:.2f}"


def format_usd(number: float) -> str:
    """Format number to readable USD string with K, M, B suffixes."""
    return f"${format_with_suffix(number)}"


def format_token_amount(raw: int, decimals: int) -> float:
    """Convert a raw token amount to a human-readable float."""
    return raw / (10**decimals)


def format_duration(seconds: int) -> str:
    """Format a duration in seconds as a compact human-readable string.

    Minutes are dropped once the duration spans days, so long durations stay
    readable (e.g. "12d 3h" rather than "12d 3h 47m").

    Args:
        seconds: Duration in seconds. Zero or negative renders as "now".

    Returns:
        A string like "now", "45s", "12m", "3h 5m" or "12d 3h".
    """
    if seconds <= 0:
        return "now"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else f"{seconds}s"
