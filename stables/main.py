"""Stablecoin depeg price monitoring — runs on a fast schedule (every 10-15 min).

Fetches all monitored stablecoin prices in a single DeFiLlama call and routes
depeg alerts to the owning protocol's Telegram channel.
"""

from decimal import Decimal

from utils.alert import Alert, AlertSeverity, send_alert
from utils.defillama import fetch_prices
from utils.logging import get_logger

logger = get_logger("stables")

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

    # Group depegged tokens by protocol
    depegged_by_protocol: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for name, key, protocol, depeg_threshold in MONITORED_TOKENS:
        price = prices.get(key)
        if price is None:
            logger.warning("No price returned for %s (%s)", name, key)
            continue
        logger.info("%s price: $%s", name, price)
        if price < depeg_threshold:
            depegged_by_protocol.setdefault(protocol, []).append((name, price, depeg_threshold))

    for protocol, depegged in depegged_by_protocol.items():
        lines = [f"*{name}*: ${price} (below ${depeg_threshold})" for name, price, depeg_threshold in depegged]
        message = f"Stablecoin Depeg ({protocol}):\n" + "\n".join(lines)
        send_alert(Alert(AlertSeverity.CRITICAL, message, protocol))

    logger.info("Stablecoin price check complete (%d tokens)", len(MONITORED_TOKENS))


if __name__ == "__main__":
    main()
