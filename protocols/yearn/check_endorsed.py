#!/usr/bin/env python3
"""Verify Yearn v3 Kong vaults are endorsed on-chain in the registry.

Fetches vault metadata from Kong per chain and checks each address via
the registry contract's isEndorsed function. Sends a Telegram alert if any
vaults are not endorsed.
"""

from typing import Dict, List

import requests
from dotenv import load_dotenv
from web3 import Web3

from protocols.yearn.kong import STRATEGY_SOURCE_DEFAULT_QUEUE, fetch_kong_vaults
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

load_dotenv()

logger = get_logger("yearn.check_endorsed")

PROTOCOL = "yearn"

REGISTRY_ADDRESS = Web3.to_checksum_address("0xd40ecF29e001c76Dcc4cC0D9cd50520CE845B038")
CACHE_KEY_PREFIX = "yearn_endorsed_alerted"
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


def alerted_cache_key(chain: Chain, address: str) -> str:
    """Return the persistent cache key for an already-alerted vault."""
    return f"{CACHE_KEY_PREFIX}_{chain.chain_id}_{address.lower()}"


def was_already_alerted(chain: Chain, address: str) -> bool:
    """Return whether this unendorsed vault has already triggered an alert."""
    cached = get_last_value_for_key_from_file(cache_filename, alerted_cache_key(chain, address))
    return str(cached) == "1"


def mark_alerted(chain: Chain, address: str) -> None:
    """Persist that this unendorsed vault has triggered an alert."""
    write_last_value_to_file(cache_filename, alerted_cache_key(chain, address), 1)


def filter_new_unendorsed(errors: Dict[Chain, List[str]]) -> Dict[Chain, List[str]]:
    """Remove unendorsed vaults that have already been alerted before."""
    new_errors: Dict[Chain, List[str]] = {}
    for chain, addresses in errors.items():
        new_addresses = [addr for addr in addresses if not was_already_alerted(chain, addr)]
        suppressed = len(addresses) - len(new_addresses)
        if suppressed:
            logger.info(
                "Suppressed %d previously alerted unendorsed vaults on %s",
                suppressed,
                chain.name,
            )
        if new_addresses:
            new_errors[chain] = new_addresses
    return new_errors


def mark_alerted_errors(errors: Dict[Chain, List[str]]) -> None:
    """Mark every vault included in a sent alert as already alerted."""
    for chain, addresses in errors.items():
        for address in addresses:
            mark_alerted(chain, address)


def fetch_kong_vault_addresses(chain: Chain) -> List[str]:
    """Fetch active vault addresses from Kong for a given chain.

    Args:
        chain: The chain to fetch vaults for.

    Returns:
        List of vault addresses.
    """
    vaults = fetch_kong_vaults(chain, strategy_source=STRATEGY_SOURCE_DEFAULT_QUEUE)
    return [str(vault["address"]) for vault in vaults]


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
        "👹 *Kong Endorsed Check*",
        f"Checked {total_checked} vaults, found {total_errors} newly unendorsed:\n",
    ]
    for chain, addresses in errors.items():
        lines.append(f"*{chain.name}* ({len(addresses)}):")
        for addr in addresses:
            lines.append(f"  `{addr}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Run the endorsed vault check across all configured chains."""
    logger.info("Starting Kong endorsed vault check")

    all_errors: Dict[Chain, List[str]] = {}
    total_checked = 0

    for chain in CHAINS:
        logger.info("Checking chain %s (id=%d)", chain.name, chain.chain_id)
        try:
            addresses = fetch_kong_vault_addresses(chain)
        except requests.RequestException as e:
            logger.error("Failed to fetch Kong data for %s: %s", chain.name, e)
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

    new_errors = filter_new_unendorsed(all_errors)
    new_total_errors = sum(len(addrs) for addrs in new_errors.values())

    if not all_errors:
        logger.info("All vaults endorsed, no alert needed")
        return

    if not new_errors:
        logger.info(
            "All %d unendorsed vaults were previously alerted, no alert needed",
            total_errors,
        )
        return

    logger.info("Alerting on %d newly unendorsed vaults", new_total_errors)
    message = build_alert_message(new_errors, total_checked)
    send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    mark_alerted_errors(new_errors)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
