#!/usr/bin/env python3
"""Verify the Yearn TimelockController minimum delay is at least 7 days on every chain.

Reads getMinDelay() from the Yearn TimelockController on each supported chain and
alerts if any value is below 7 days. The minimum delay is a security parameter and
should never silently drop.
"""

from dataclasses import dataclass

from dotenv import load_dotenv
from web3 import Web3

from utils.chains import EXPLORER_URLS, Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

load_dotenv()

logger = get_logger("yearn.check_timelock_delay")

# Delay-check violations are an internal security concern, not public topic
# noise — route them to the internal-only chat instead of YEARN_TIMELOCK.
ALERT_PROTOCOL = "YEARN_TIMELOCK_INTERNAL"

TIMELOCK_ADDRESS = Web3.to_checksum_address("0x88ba032be87d5ef1fbe87336b7090767f367bf73")
EXPECTED_MIN_DELAY_SECONDS = 7 * 24 * 60 * 60

CHAINS = [Chain.MAINNET, Chain.OPTIMISM, Chain.BASE, Chain.ARBITRUM, Chain.POLYGON, Chain.KATANA]

TIMELOCK_ABI = [
    {
        "inputs": [],
        "name": "getMinDelay",
        "outputs": [{"internalType": "uint256", "name": "duration", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass
class DelayViolation:
    """A chain whose timelock minimum delay is below the expected threshold."""

    chain: Chain
    min_delay_seconds: int


def fetch_min_delay(chain: Chain) -> int:
    """Read getMinDelay() from the Yearn TimelockController on the given chain.

    Args:
        chain: The chain to query.

    Returns:
        Minimum delay in seconds.
    """
    client = ChainManager.get_client(chain)
    timelock = client.get_contract(TIMELOCK_ADDRESS, TIMELOCK_ABI)
    return int(timelock.functions.getMinDelay().call())


def format_seconds(seconds: int) -> str:
    """Format a seconds value as a human-readable duration."""
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


def build_alert_message(violations: list[DelayViolation]) -> str:
    """Build a Telegram alert message listing chains with insufficient delay."""
    lines = [
        "⏰ *Yearn Timelock Delay Check*",
        f"Found {len(violations)} chain(s) with min delay below 7 days:",
    ]
    for v in violations:
        explorer = EXPLORER_URLS.get(v.chain.chain_id)
        if explorer:
            address_md = f"[{TIMELOCK_ADDRESS}]({explorer}/address/{TIMELOCK_ADDRESS})"
        else:
            address_md = f"`{TIMELOCK_ADDRESS}`"
        lines.append(f"*{v.chain.name}*: {format_seconds(v.min_delay_seconds)} — {address_md}")
    return "\n".join(lines)


def main() -> None:
    """Run the min-delay check across all configured chains."""
    logger.info("Starting Yearn timelock min-delay check (expected: %ds)", EXPECTED_MIN_DELAY_SECONDS)

    violations: list[DelayViolation] = []
    for chain in CHAINS:
        min_delay = fetch_min_delay(chain)
        logger.info("Chain %s min delay: %ds (%s)", chain.name, min_delay, format_seconds(min_delay))
        if min_delay < EXPECTED_MIN_DELAY_SECONDS:
            violations.append(DelayViolation(chain=chain, min_delay_seconds=min_delay))

    if not violations:
        logger.info("All chains have min delay >= 7 days")
        return

    message = build_alert_message(violations)
    send_telegram_message(message, ALERT_PROTOCOL, disable_notification=True)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, ALERT_PROTOCOL)
