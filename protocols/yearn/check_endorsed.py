#!/usr/bin/env python3
"""Verify Yearn v3 yDaemon vaults are endorsed on-chain in the registry.

Fetches vault metadata from yDaemon per chain and checks each address via
the registry contract's isEndorsed function. Sends a Telegram alert if any
vaults are not endorsed.
"""

from typing import Dict, List

import requests
from dotenv import load_dotenv
from web3 import Web3

from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message_with_fallback
from utils.web3_wrapper import ChainManager

load_dotenv()

logger = get_logger("yearn.check_endorsed")

PROTOCOL = "yearn"

YDAEMON_BASE_URL = "https://ydaemon.yearn.fi/vaults/v3"
YDAEMON_PARAMS = "hideAlways=true&strategiesDetails=withDetails&strategiesCondition=inQueue"

REGISTRY_ADDRESS = Web3.to_checksum_address("0xd40ecF29e001c76Dcc4cC0D9cd50520CE845B038")
REGISTRY_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "isEndorsed",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]

CHAINS = [Chain.MAINNET, Chain.POLYGON, Chain.BASE, Chain.ARBITRUM, Chain.KATANA]


def fetch_ydaemon_vaults(chain: Chain) -> List[str]:
    """Fetch vault addresses from yDaemon API for a given chain.

    Args:
        chain: The chain to fetch vaults for.

    Returns:
        List of vault addresses.
    """
    url = f"{YDAEMON_BASE_URL}?{YDAEMON_PARAMS}&chainIDs={chain.chain_id}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    vaults = response.json()
    return [vault["address"].lower() for vault in vaults if "address" in vault]


def fetch_onchain_endorsed(chain: Chain, addresses: List[str]) -> Dict[str, bool]:
    """Batch-fetch the endorsed status for each address from the on-chain registry.

    Args:
        chain: The chain to query.
        addresses: List of vault addresses to check.

    Returns:
        Mapping of address to its endorsed status.
    """
    client = ChainManager.get_client(chain)
    registry = client.get_contract(REGISTRY_ADDRESS, REGISTRY_ABI)

    with client.batch_requests() as batch:
        for addr in addresses:
            batch.add(registry.functions.isEndorsed(Web3.to_checksum_address(addr)))
        results = batch.execute()

    return dict(zip(addresses, results))


def get_unendorsed(chain: Chain, endorsed_map: Dict[str, bool]) -> List[str]:
    """Return addresses that are not endorsed on-chain.

    Args:
        chain: The chain (used for logging).
        endorsed_map: Mapping of address to endorsed status.

    Returns:
        List of unendorsed vault addresses.
    """
    unendorsed = [addr for addr, endorsed in endorsed_map.items() if not endorsed]
    for addr in unendorsed:
        logger.warning("Not endorsed on %s: %s", chain.name, addr)

    logger.info("Chain %s: %d/%d unendorsed", chain.name, len(unendorsed), len(endorsed_map))
    return unendorsed


def build_alert_message(errors: Dict[Chain, List[str]], total_checked: int) -> str:
    """Build a Telegram alert message from the errors dict.

    Args:
        errors: Mapping of chain to list of unendorsed addresses.
        total_checked: Total number of vaults checked.

    Returns:
        Formatted alert message string.
    """
    total_errors = sum(len(addrs) for addrs in errors.values())
    lines = [
        "👹 *yDaemon Endorsed Check*",
        f"Checked {total_checked} vaults, found {total_errors} unendorsed:\n",
    ]
    for chain, addresses in errors.items():
        lines.append(f"*{chain.name}* ({len(addresses)}):")
        for addr in addresses:
            lines.append(f"  `{addr}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Run the endorsed vault check across all configured chains."""
    logger.info("Starting yDaemon endorsed vault check")

    all_errors: Dict[Chain, List[str]] = {}
    total_checked = 0

    for chain in CHAINS:
        logger.info("Checking chain %s (id=%d)", chain.name, chain.chain_id)
        try:
            addresses = fetch_ydaemon_vaults(chain)
        except requests.RequestException as e:
            logger.error("Failed to fetch yDaemon data for %s: %s", chain.name, e)
            continue

        if not addresses:
            logger.info("No vaults found for %s, skipping", chain.name)
            continue

        logger.info("Found %d vaults for %s", len(addresses), chain.name)
        total_checked += len(addresses)

        endorsed_map = fetch_onchain_endorsed(chain, addresses)
        unendorsed = get_unendorsed(chain, endorsed_map)
        if unendorsed:
            all_errors[chain] = unendorsed

    total_errors = sum(len(addrs) for addrs in all_errors.values())
    logger.info("Done. %d/%d vaults unendorsed", total_errors, total_checked)

    if not all_errors:
        logger.info("All vaults endorsed, no alert needed")
        return

    message = build_alert_message(all_errors, total_checked)
    fallback = (
        f"👹 *yDaemon Endorsed Check*\n"
        f"Found {total_errors} unendorsed vaults across {len(all_errors)} chains.\n"
        f"Too many to list here."
    )
    send_telegram_message_with_fallback(message, PROTOCOL, fallback, max_length=2000)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
