"""Common formatting helpers for monitoring scripts."""


def format_with_suffix(number: float) -> str:
    """Format number with K, M, B suffixes for readability."""
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.2f}K"
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
