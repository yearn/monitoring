"""Watch the value each infiniFi L2 vault (OutlandVault) reports for its chain.

An OutlandVault on mainnet mirrors infiniFi's deposits on one L2. Its value
(``portalAssetsReport().totalAssetsValue``) only changes when an assets-update
message is executed, and executing one mints or burns the vault's farm shares,
booking the change as profit or loss on mainnet. Messages normally arrive over
the bridge, but governance can inject any message with ``Connector.govReceive``:
on 23/09/2026 an injected all-zero update wrote the old Base vault down to 0.

For every chain the PortalHub lists, this script alerts (MEDIUM) when:

- the vault's value falls by more than ``INFINIFI_L2_VAULT_DROP_THRESHOLD``
  (default 50%) since the previous run, including a fall to 0, and the fall is at
  least ``INFINIFI_L2_VAULT_MIN_VALUE`` USD (default 10,000);
- the vault has not received a report for ``INFINIFI_L2_VAULT_STALE_HOURS``
  (default 48h) while it holds at least the minimum value.

The baseline is keyed by vault address, so a planned migration, where the hub
is pointed at a new vault, starts a fresh baseline and does not alert.
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
    }
]


@dataclass(frozen=True)
class VaultReport:
    """What one chain's OutlandVault reports on mainnet."""

    chain_id: int
    vault: str
    value: Decimal  # USD, from 18 decimals
    last_update: int  # unix seconds of the last executed assets update


def _link(address: str) -> str:
    return f"[{address}]({EXPLORER}/{address})"


def _chain_name(chain_id: int) -> str:
    try:
        return f"{Chain.from_chain_id(chain_id).network_name} ({chain_id})"
    except ValueError:
        return f"chain {chain_id}"


def _time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%d/%m/%Y %H:%M UTC")


def fetch_reports(client: Web3Client) -> list[VaultReport]:
    """The current report of every vault the PortalHub routes a chain to."""
    hub = client.get_contract(PORTAL_HUB, _HUB_ABI).functions
    reports = []
    for chain_id in hub.getVaultChainIds().call():
        vault = to_checksum_address(hub.getVault(chain_id).call())
        total_assets, _, _, last_update = client.get_contract(vault, _VAULT_ABI).functions.portalAssetsReport().call()
        reports.append(
            VaultReport(int(chain_id), vault, Decimal(int(total_assets)) / Decimal(10**18), int(last_update))
        )
    return reports


def drop_message(report: VaultReport, previous: Decimal) -> str | None:
    """Alert text when the vault's value fell too far since the previous run, else None."""
    fall = previous - report.value
    if previous <= 0 or fall < MIN_VALUE or fall / previous <= DROP_THRESHOLD:
        return None
    return (
        "⚠️ *Infinifi L2 Vault Value Drop*\n\n"
        f"Chain: {_chain_name(report.chain_id)}\n"
        f"Vault: {_link(report.vault)}\n"
        f"Reported value: ${previous:,.2f} → ${report.value:,.2f} (−{fall / previous:.1%})\n"
        f"Last assets update: {_time(report.last_update)}\n\n"
        "An executed assets update burned the vault's farm shares, booking the fall as a loss on mainnet. "
        "Check whether it came over the bridge or through govReceive."
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


def check_report(report: VaultReport, now: int) -> None:
    """Compare a vault's report with the previous run and its update age, and alert on a breach."""
    value_key = f"{PROTOCOL}_l2_vault_value_{report.vault.lower()}"
    previous = Decimal(
        str(get_fresh_last_value_for_key_from_file(cache_filename, value_key, HOURLY_CACHE_STALE_AFTER_SECONDS))
    )
    # A drop is a one-off event: alert on it directly instead of holding breach state.
    message = drop_message(report, previous)
    if message is not None:
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    write_last_value_with_timestamp_to_file(cache_filename, value_key, str(report.value))
    _alert_once(f"{PROTOCOL}_l2_vault_stale_{report.vault.lower()}", stale_message(report, now))


def main() -> None:
    client = ChainManager.get_client(Chain.MAINNET)
    now = int(datetime.now(UTC).timestamp())
    for report in fetch_reports(client):
        logger.info(
            "L2 vault %s %s: %s USD, last update %s",
            _chain_name(report.chain_id),
            report.vault,
            f"{report.value:,.2f}",
            _time(report.last_update),
        )
        check_report(report, now)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
