"""Shared reads for infiniFi RWA escrows."""

from eth_utils import to_checksum_address

from utils.web3_wrapper import Web3Client

WHITELIST_EVENT_ABI = [
    {
        "anonymous": False,
        "name": "WhitelistUpdated",
        "type": "event",
        "inputs": [
            {"indexed": True, "name": "timestamp", "type": "uint256"},
            {"indexed": False, "name": "target", "type": "address"},
            {"indexed": False, "name": "enabled", "type": "bool"},
        ],
    }
]


def fetch_whitelist_targets(client: Web3Client, escrow_address: str) -> list[str]:
    """Rebuild an RWAEscrowRouter's current ``externalCall`` whitelist from its WhitelistUpdated events.

    The router keeps the whitelist in a mapping with no enumeration, so the
    events are the only on-chain list of targets.
    """
    escrow = client.get_contract(to_checksum_address(escrow_address), WHITELIST_EVENT_ABI)
    events = escrow.events.WhitelistUpdated().get_logs(from_block=0, to_block="latest")
    enabled_by_address: dict[str, tuple[str, bool]] = {}
    for event in events:
        args = event.get("args", {})
        raw_address = args.get("target")
        enabled = args.get("enabled")
        if not isinstance(raw_address, str) or not isinstance(enabled, bool):
            continue
        try:
            address = to_checksum_address(raw_address)
        except ValueError:
            continue
        enabled_by_address[address.lower()] = (address, enabled)
    return [address for address, enabled in enabled_by_address.values() if enabled]
