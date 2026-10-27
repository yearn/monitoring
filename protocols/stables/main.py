"""Stablecoin depeg price monitoring — runs on a fast schedule (every 10-15 min).

Fetches all monitored stablecoin prices in a single DeFiLlama call and routes
depeg alerts to the owning protocol's Telegram channel.
"""

from decimal import Decimal

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.defillama import fetch_prices
from utils.logger import get_logger

logger = get_logger("stables")

# Consecutive below-threshold checks before a depeg may trigger the emergency
# withdrawal dispatch. A single DeFiLlama sample is one third-party datapoint and
# occasionally wrong; the alert still fires on the first breach, but funds only
# move once the next run (~10 min later) confirms it.
DISPATCH_CONFIRMATION_RUNS = 2
CACHE_KEY_BREACH_PREFIX = "stables_depeg_breaches_"

# (display_name, defillama_key, protocol for telegram routing)
MONITORED_TOKENS: list[tuple[str, str, str, Decimal]] = [
    # ethena
    ("USDe", "ethereum:0x4c9EDD5852cd905f086C759E8383e09bff1E68B3", "ethena", Decimal("0.97")),
    ("sUSDe", "ethereum:0x9D39A5DE30e57443BfF2A8307A4256c8797A3497", "ethena", Decimal("1.1")),
    # infinifi
    ("iUSD", "ethereum:0x48f9e38f3070AD8945DFEae3FA70987722E3D89c", "infinifi", Decimal("0.97")),
    # maple
    ("syrupUSDC", "ethereum:0x80ac24aa929eaf5013f6436cda2a7ba190f5cc0b", "maple", Decimal("1.1")),
    ("syrupUSDT", "ethereum:0x356b8d89c1e1239cbbb9de4815c39a1474d5ba7d", "maple", Decimal("1.1")),
    # syrupUSDG is a newer pool with PPS ~1.01, so 1.1 would alert on every run
    ("syrupUSDG", "ethereum:0x87b65c4aaffa76881f9e96f3e7ed945ddfc3cd7a", "maple", Decimal("1.0")),
    # ustb (superstate)
    ("USTB", "ethereum:0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e", "ustb", Decimal("10.5")),
    # cap
    # (
    #     "cUSD",
    #     "ethereum:0xcccc62962d17b8914c62d74ffb843d73b2a3cccc",
    #     "cap",
    #     Decimal("0.9"),
    # ),  # much lower because price is very volatile
]


def main() -> None:
    """Check all stablecoin prices with one DeFiLlama call, alerting per protocol."""
    try:
        prices = fetch_prices([key for _, key, _, _ in MONITORED_TOKENS])
    except Exception as exc:
        logger.error("Failed to fetch DeFiLlama prices: %s", exc)
        return

    # Group depegged tokens by protocol: (name, address, price, threshold, consecutive breaches)
    depegged_by_protocol: dict[str, list[tuple[str, str, Decimal, Decimal, int]]] = {}
    for name, key, protocol, depeg_threshold in MONITORED_TOKENS:
        price = prices.get(key)
        if price is None:
            # Leave the breach counter alone: a missing sample neither confirms nor clears.
            logger.warning("No price returned for %s (%s)", name, key)
            continue
        logger.info("%s price: $%s", name, price)
        breaches = _update_breach_count(key, price < depeg_threshold)
        if breaches:
            # key is a DeFiLlama id like "ethereum:0x...", so the address is the part after ":"
            address = key.split(":", 1)[1]
            depegged_by_protocol.setdefault(protocol, []).append((name, address, price, depeg_threshold, breaches))

    for protocol, depegged in depegged_by_protocol.items():
        lines = [
            f"[{name}](https://etherscan.io/token/{address}): ${price} (below ${depeg_threshold})"
            for name, address, price, depeg_threshold, _ in depegged
        ]
        confirmed = any(breaches >= DISPATCH_CONFIRMATION_RUNS for *_, breaches in depegged)
        message = f"Stablecoin Depeg ({protocol}):\n" + "\n".join(lines)
        if not confirmed:
            message += (
                "\nUnconfirmed: first DeFiLlama sample below threshold; emergency dispatch waits for the next check."
            )
        send_alert(Alert(AlertSeverity.CRITICAL, message, protocol, dispatch=confirmed))

    logger.info("Stablecoin price check complete (%d tokens)", len(MONITORED_TOKENS))


def _update_breach_count(key: str, breached: bool) -> int:
    """Record whether ``key`` is below its threshold this run; return the consecutive breach count."""
    cache_key = CACHE_KEY_BREACH_PREFIX + key
    previous = int(get_last_value_for_key_from_file(cache_filename, cache_key) or 0)
    current = previous + 1 if breached else 0
    if current != previous:
        write_last_value_to_file(cache_filename, cache_key, current)
    return current


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Multi-safe script with per-safe routing; crash alerts go to the general ops channel.
    run_with_alert(main, "yearn")
