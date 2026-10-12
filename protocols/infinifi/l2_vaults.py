"""Watch the value each infiniFi L2 vault (OutlandVault) reports for its chain.

An OutlandVault on mainnet mirrors infiniFi's deposits on one L2. Its value
(``portalAssetsReport().totalAssetsValue``) is the vault's farm shares minus its
current local liquidity. Assets updates reconcile the farm shares and can book
profit or loss; token transfers also change the reported value as local
liquidity moves. Only assets updates refresh ``lastPortalUpdate``. Messages
normally arrive over the bridge, but governance can inject any message with
``Connector.govReceive``: on 23/09/2026 an injected all-zero update wrote the old
Base vault down to 0.

For every chain the PortalHub lists, this script alerts (MEDIUM) when:

- an executed assets update books a farm-share value loss exceeding
  ``INFINIFI_L2_VAULT_DROP_THRESHOLD`` (default 50%), including a fall to 0,
  and at least ``INFINIFI_L2_VAULT_MIN_VALUE`` USD (default 10,000);
- the vault has not received a report for ``INFINIFI_L2_VAULT_STALE_HOURS``
  (default 48h) while it holds at least the minimum value.

The monitor uses only mainnet reads. Comparing the mirrored value with L2 farm
totals produces false mismatches while outbound transfers await settlement,
even when both totals are read on-chain.

Reads and event cursors use finalized Ethereum blocks. The first reading of a
vault initializes its cursor; subsequent runs inspect every assets update since
that cursor, including across monitoring gaps. Token transfers never serve as
loss baselines. Cursors are keyed by vault address, so migrations start fresh.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from eth_utils import to_checksum_address

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    HOURLY_CACHE_STALE_AFTER_SECONDS,
    cache_filename,
    get_fresh_last_value_for_key_from_file,
    get_last_value_for_key_from_file,
    write_last_value_with_timestamp_to_file,
)
from utils.chains import Chain
from utils.config import Config
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "infinifi"
logger = get_logger(f"{PROTOCOL}.l2_vaults")

PORTAL_HUB = "0x13025F34C1ec2A16bF68f3a3c4e986a3E85CED61"
EXPLORER = "https://etherscan.io/address"

DROP_THRESHOLD = Decimal(Config.get_env("INFINIFI_L2_VAULT_DROP_THRESHOLD", "0.5") or "0.5")
MIN_VALUE = Decimal(Config.get_env("INFINIFI_L2_VAULT_MIN_VALUE", "10000") or "10000")
STALE_HOURS = Config.get_env_int("INFINIFI_L2_VAULT_STALE_HOURS", 48)
LOG_BLOCK_RANGE = 2000

_HUB_ABI = [
    {
        "name": "getVaultChainIds",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256[]"}],
    },
    {
        "name": "getVault",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]
_VAULT_ABI = [
    {
        "name": "portalAssetsReport",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"} for _ in range(4)],
    },
    {
        "name": "PortalUpdate",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "timestamp", "type": "uint256", "indexed": True},
            {"name": "oldAssets", "type": "uint256", "indexed": False},
            {"name": "newAssets", "type": "uint256", "indexed": False},
        ],
    },
]


@dataclass(frozen=True)
class VaultReport:
    """What one chain's OutlandVault reports on mainnet."""

    chain_id: int
    vault: str
    value: Decimal  # USD, from 18 decimals
    last_update: int  # unix seconds of the last executed assets update


@dataclass(frozen=True)
class VaultUpdate:
    """The farm-share value change booked by one executed assets update."""

    timestamp: int
    previous_value: Decimal
    value: Decimal
    transaction_hash: str


def _link(address: str) -> str:
    return f"[{address}]({EXPLORER}/{address})"


def _chain_name(chain_id: int) -> str:
    try:
        return f"{Chain.from_chain_id(chain_id).network_name} ({chain_id})"
    except ValueError:
        return f"chain {chain_id}"


def _time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%d/%m/%Y %H:%M UTC")


def fetch_reports(client: Web3Client, block_number: int) -> list[VaultReport]:
    """Read every registered vault at the same finalized Ethereum block.

    Args:
        client: Ethereum RPC client.
        block_number: Finalized block used for all registry and vault reads.

    Returns:
        The mirrored value and update timestamp for every registered vault.
    """
    hub = client.get_contract(PORTAL_HUB, _HUB_ABI).functions
    reports = []
    for chain_id in hub.getVaultChainIds().call(block_identifier=block_number):
        vault = to_checksum_address(hub.getVault(chain_id).call(block_identifier=block_number))
        total_assets, _, _, last_update = (
            client.get_contract(vault, _VAULT_ABI).functions.portalAssetsReport().call(block_identifier=block_number)
        )
        reports.append(
            VaultReport(int(chain_id), vault, Decimal(int(total_assets)) / Decimal(10**18), int(last_update))
        )
    return reports


def fetch_updates(client: Web3Client, vault: str, from_block: int, to_block: int) -> list[VaultUpdate]:
    """Read assets-update events in bounded block ranges, preserving execution order.

    Args:
        client: Ethereum RPC client.
        vault: Mainnet vault emitting the events.
        from_block: First block to inspect, inclusive.
        to_block: Last finalized block to inspect, inclusive.

    Returns:
        Each update's booked before/after values in execution order.
    """
    event = client.get_contract(vault, _VAULT_ABI).events.PortalUpdate()
    updates = []
    for start in range(from_block, to_block + 1, LOG_BLOCK_RANGE):
        logs = event.get_logs(from_block=start, to_block=min(start + LOG_BLOCK_RANGE - 1, to_block))
        for log in sorted(logs, key=lambda entry: (entry["blockNumber"], entry["logIndex"])):
            args = log["args"]
            updates.append(
                VaultUpdate(
                    int(args["timestamp"]),
                    Decimal(int(args["oldAssets"])) / Decimal(10**18),
                    Decimal(int(args["newAssets"])) / Decimal(10**18),
                    f"0x{log['transactionHash'].hex()}",
                )
            )
    return updates


def drop_message(report: VaultReport, update: VaultUpdate) -> str | None:
    """Describe a loss booked by an assets update that exceeds both loss thresholds.

    Args:
        report: Identifies the chain and mainnet vault.
        update: The event's before/after farm-share values and transaction.

    Returns:
        The loss alert, or None when the update does not breach both thresholds.
    """
    fall = update.previous_value - update.value
    if update.previous_value <= 0 or fall < MIN_VALUE or fall / update.previous_value <= DROP_THRESHOLD:
        return None
    return (
        "⚠️ *Infinifi L2 Vault Value Drop*\n\n"
        f"Chain: {_chain_name(report.chain_id)}\n"
        f"Vault: {_link(report.vault)}\n"
        f"Booked farm value: ${update.previous_value:,.2f} → ${update.value:,.2f} "
        f"(−{fall / update.previous_value:.1%})\n"
        f"Assets update: {_time(update.timestamp)}\n"
        f"Transaction: [{update.transaction_hash}](https://etherscan.io/tx/{update.transaction_hash})\n\n"
        "This assets update burned the vault's farm shares, booking a loss on mainnet. "
        "Check whether the update arrived over the bridge or through govReceive."
    )


def stale_message(report: VaultReport, now: int) -> str | None:
    """Alert text when a vault holding real value has not been updated recently, else None."""
    age_hours = (now - report.last_update) / 3600
    if report.value < MIN_VALUE or age_hours <= STALE_HOURS:
        return None
    return (
        "⚠️ *Infinifi L2 Vault Report Stale*\n\n"
        f"Chain: {_chain_name(report.chain_id)}\n"
        f"Vault: {_link(report.vault)}\n"
        f"Reported value: ${report.value:,.2f}\n"
        f"Last assets update: {_time(report.last_update)} ({age_hours:.0f}h ago, threshold {STALE_HOURS}h)\n\n"
        "Mainnet is carrying this chain's deposits at a value nobody has confirmed recently."
    )


def _alert_once(cache_key: str, message: str | None) -> None:
    """Alert on the first breach; stay quiet until it clears or the state goes stale."""
    if message is None:
        write_last_value_with_timestamp_to_file(cache_filename, cache_key, 0)
        return
    if int(get_fresh_last_value_for_key_from_file(cache_filename, cache_key, HOURLY_CACHE_STALE_AFTER_SECONDS)) == 0:
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    write_last_value_with_timestamp_to_file(cache_filename, cache_key, 1)


def check_report(report: VaultReport, now: int, client: Web3Client, block_number: int) -> None:
    """Inspect every new finalized assets update for losses and check report staleness.

    Args:
        report: The vault state at the finalized block.
        now: Current Unix time used to check staleness.
        client: Ethereum RPC client used to read update events.
        block_number: Finalized block through which to advance the vault's cursor.

    Raises:
        ValueError: The RPC snapshot is older than the persisted cursor.
    """
    cursor_key = f"{PROTOCOL}_l2_vault_update_block_{report.vault.lower()}"
    previous_block = int(get_last_value_for_key_from_file(cache_filename, cursor_key))
    if previous_block > block_number:
        raise ValueError(
            f"Vault {report.vault}: cached block {previous_block} exceeds finalized block "
            f"{block_number}; check whether the Ethereum RPC is behind."
        )
    if previous_block:
        for update in fetch_updates(client, report.vault, previous_block + 1, block_number):
            message = drop_message(report, update)
            if message is not None:
                send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    write_last_value_with_timestamp_to_file(cache_filename, cursor_key, block_number)
    _alert_once(f"{PROTOCOL}_l2_vault_stale_{report.vault.lower()}", stale_message(report, now))


def main() -> None:
    """Read finalized mainnet vault state and alert on booked losses and stale reports."""
    client = ChainManager.get_client(Chain.MAINNET)
    block_number = int(client.eth.get_block("finalized")["number"])
    now = int(datetime.now(UTC).timestamp())
    for report in fetch_reports(client, block_number):
        logger.info(
            "L2 vault %s %s: %s USD, last update %s",
            _chain_name(report.chain_id),
            report.vault,
            f"{report.value:,.2f}",
            _time(report.last_update),
        )
        check_report(report, now, client, block_number)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
