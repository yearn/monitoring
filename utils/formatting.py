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
