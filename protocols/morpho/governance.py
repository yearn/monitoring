from dataclasses import dataclass
from datetime import datetime
from typing import Any

from web3 import Web3

from protocols.morpho._shared import (
    PROTOCOL,
    MorphoMonitoringError,
    execute_graphql,
    fetch_market_metadata,
    get_market_url,
    get_vault_url,
)
from protocols.morpho.config import VAULTS_V1_BY_CHAIN
from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    get_last_executed_morpho_from_file,
    write_last_executed_morpho_to_file,
)
from utils.chains import Chain
from utils.formatting import format_token_amount, format_with_suffix
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("morpho.governance")

PENDING_CAP_TYPE = "pending_cap"
REMOVABLE_AT_TYPE = "removable_at"
ABI_MORPHO = load_abi("protocols/morpho/abi/morpho.json")


@dataclass(frozen=True)
class MarketGovernanceState:
    """Pending governance state for one market in a V1 vault."""

    vault_address: str
    market_id: str
    pending_cap: int
    pending_cap_timestamp: int
    current_cap: int
    removable_at: int


def fetch_pending_cap_market_ids(vault_address: str, chain: Chain) -> list[str]:
    """Fetch market unique keys with pending cap submissions for a vault from Morpho GraphQL API.

    Catches brand-new markets where submitCap has been called but acceptCap has not run yet —
    those markets are not yet in supplyQueue or withdrawQueue, so the on-chain queue iteration
    misses them.

    Returns a list of hex-encoded market IDs, or an empty list on failure.
    """
    query = """
    query GetVaultPendingCaps($address: String!, $chainId: Int!) {
        vaultByAddress(address: $address, chainId: $chainId) {
            state {
                pendingConfigs {
                    items {
                        functionName
                        decodedData {
                            __typename
                            ... on VaultSetCapPendingData {
                                market { marketId }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    try:
        data = execute_graphql(
            query,
            {"address": vault_address, "chainId": chain.chain_id},
            f"pending caps for {vault_address} on {chain.name}",
        )
        items = (((data.get("vaultByAddress") or {}).get("state") or {}).get("pendingConfigs") or {}).get("items") or []
        market_ids = []
        for item in items:
            if item.get("functionName") != "SetCap":
                continue
            decoded = item.get("decodedData") or {}
            market = decoded.get("market") or {}
            marketId = market.get("marketId")
            if marketId:
                market_ids.append(marketId)
        return market_ids
    except MorphoMonitoringError as e:
        logger.warning("Failed to fetch pending caps for vault %s: %s", vault_address, e)
        return []


def fetch_market_info(market_id: str, chain: Chain) -> tuple[str, int | None]:
    """Return the shared market label with LLTV and loan-token decimals."""
    metadata = fetch_market_metadata(market_id, chain)
    if metadata is None:
        return market_id, None
    lltv_pct = metadata["lltv"] / 1e18 * 100
    return f"{metadata['name']} ({lltv_pct:.2f}%)", int(metadata["loan_decimals"])


def format_cap(cap: int, decimals: int | None) -> str:
    """Format a raw supply cap as a human-readable amount with K/M/B suffix.

    Falls back to comma-separated raw if decimals are unknown.
    """
    if decimals is None or decimals <= 0:
        return f"{cap:,}"
    return str(format_with_suffix(format_token_amount(cap, decimals)))


def _load_vault_market_ids(morpho_contract: Any, chain: Chain, client: Any) -> list[bytes]:
    """Load accepted and pending-cap market IDs for one V1 vault."""
    with client.batch_requests() as batch:
        batch.add(morpho_contract.functions.supplyQueueLength())
        batch.add(morpho_contract.functions.withdrawQueueLength())
        lengths = client.execute_batch(batch)
    if len(lengths) != 2:
        raise ValueError(f"Expected 2 queue length responses, got {len(lengths)}")

    supply_length, withdraw_length = lengths
    with client.batch_requests() as batch:
        for index in range(supply_length):
            batch.add(morpho_contract.functions.supplyQueue(index))
        for index in range(withdraw_length):
            batch.add(morpho_contract.functions.withdrawQueue(index))
        queued_markets = client.execute_batch(batch)
    expected_count = supply_length + withdraw_length
    if len(queued_markets) != expected_count:
        raise ValueError(f"Expected {expected_count} queue responses, got {len(queued_markets)}")

    pending_markets = {
        bytes.fromhex(market_id.removeprefix("0x"))
        for market_id in fetch_pending_cap_market_ids(morpho_contract.address, chain)
    }
    return list(set(queued_markets) | pending_markets)


def _load_market_governance_states(
    morpho_contract: Any,
    market_ids: list[bytes],
    client: Any,
) -> list[MarketGovernanceState]:
    """Batch pending-cap and config reads for V1 vault markets."""
    with client.batch_requests() as batch:
        for market_id in market_ids:
            batch.add(morpho_contract.functions.pendingCap(market_id))
            batch.add(morpho_contract.functions.config(market_id))
        responses = client.execute_batch(batch)
    expected_count = len(market_ids) * 2
    if len(responses) != expected_count:
        raise ValueError(f"Expected {expected_count} pendingCap/config responses, got {len(responses)}")

    states = []
    for index, market_id in enumerate(market_ids):
        pending_cap, pending_timestamp = responses[index * 2]
        config = responses[index * 2 + 1]
        states.append(
            MarketGovernanceState(
                vault_address=morpho_contract.address,
                market_id=Web3.to_hex(market_id),
                pending_cap=pending_cap,
                pending_cap_timestamp=pending_timestamp,
                current_cap=config[0],
                removable_at=config[2],
            )
        )
    return states


def _check_pending_cap(name: str, state: MarketGovernanceState, chain: Chain) -> None:
    """Alert once for a new pending V1 market cap."""
    if state.pending_cap_timestamp <= 0:
        return
    last_timestamp = get_last_executed_morpho_from_file(
        state.vault_address,
        state.market_id,
        PENDING_CAP_TYPE,
    )
    if state.pending_cap_timestamp <= last_timestamp:
        logger.info("Skipping previously alerted cap update for %s market %s", name, state.market_id)
        return

    market_url = get_market_url(state.market_id, chain)
    vault_url = get_vault_url(state.vault_address, chain)
    market_name, decimals = fetch_market_info(state.market_id, chain)
    pending_cap = format_cap(state.pending_cap, decimals)
    queued_for = datetime.fromtimestamp(state.pending_cap_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    if state.current_cap == 0:
        message = (
            f"Adding new market [{market_name}]({market_url}) with cap {pending_cap} "
            f"to vault [{name}]({vault_url}) on {chain.name}. Queued for {queued_for}"
        )
    else:
        difference = ((state.pending_cap - state.current_cap) / state.current_cap) * 100
        current_cap = format_cap(state.current_cap, decimals)
        message = (
            f"Updating cap to new cap {pending_cap}, current cap {current_cap}, difference: {difference:.2f}%. \n"
            f"For vault [{name}]({vault_url}) for market: [{market_name}]({market_url}) on {chain.name}. "
            f"Queued for {queued_for}"
        )
    send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    write_last_executed_morpho_to_file(
        state.vault_address,
        state.market_id,
        PENDING_CAP_TYPE,
        state.pending_cap_timestamp,
    )


def _check_market_removal(name: str, state: MarketGovernanceState, chain: Chain) -> None:
    """Alert once for a newly queued V1 market removal."""
    if state.removable_at <= 0:
        return
    last_timestamp = get_last_executed_morpho_from_file(
        state.vault_address,
        state.market_id,
        REMOVABLE_AT_TYPE,
    )
    if state.removable_at <= last_timestamp:
        logger.info("Skipping previously alerted market removal for %s market %s", name, state.market_id)
        return

    market_url = get_market_url(state.market_id, chain)
    vault_url = get_vault_url(state.vault_address, chain)
    market_name, _ = fetch_market_info(state.market_id, chain)
    removable_at = datetime.fromtimestamp(state.removable_at).strftime("%Y-%m-%d %H:%M:%S")
    message = f"Vault [{name}]({vault_url}) queued to remove market: [{market_name}]({market_url}) at {removable_at}"
    send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    write_last_executed_morpho_to_file(
        state.vault_address,
        state.market_id,
        REMOVABLE_AT_TYPE,
        state.removable_at,
    )


def check_market_governance_state(name: str, state: MarketGovernanceState, chain: Chain) -> None:
    """Check pending cap and removal changes for one V1 market."""
    _check_pending_cap(name, state, chain)
    _check_market_removal(name, state, chain)


def check_markets_pending_cap(name: str, morpho_contract: Any, chain: Chain, client: Any) -> None:
    """Check V1 market cap and removal governance for one vault."""
    market_ids = _load_vault_market_ids(morpho_contract, chain, client)
    for state in _load_market_governance_states(morpho_contract, market_ids, client):
        check_market_governance_state(name, state, chain)


def check_pending_role_change(
    name: str,
    morpho_contract: Any,
    role_type: str,
    timestamp: int,
    chain: Chain,
) -> None:
    market_id = ""  # use empty string for all markets because the value is used per vault
    if timestamp > get_last_executed_morpho_from_file(morpho_contract.address, market_id, role_type):
        vault_url = get_vault_url(morpho_contract.address, chain)
        send_alert(
            Alert(
                AlertSeverity.HIGH,
                f"{role_type.capitalize()} is changing for vault [{name}]({vault_url})",
                PROTOCOL,
            )
        )
        write_last_executed_morpho_to_file(morpho_contract.address, market_id, role_type, timestamp)


def check_timelock_and_guardian(name: str, morpho_contract: Any, chain: Chain, client: Any) -> None:
    with morpho_contract.w3.batch_requests() as batch:
        batch.add(morpho_contract.functions.pendingTimelock())
        batch.add(morpho_contract.functions.pendingGuardian())
        responses = client.execute_batch(batch)
        if len(responses) != 2:
            raise ValueError("Expected 2 responses from batch, got: ", len(responses))

        timelock = responses[0][1]  # [1] to get the timestamp
        guardian = responses[1][1]  # [1] to get the timestamp

    check_pending_role_change(name, morpho_contract, "timelock", timelock, chain)
    check_pending_role_change(name, morpho_contract, "guardian", guardian, chain)


def get_data_for_chain(chain: Chain) -> None:
    client = ChainManager.get_client(chain)
    vaults = VAULTS_V1_BY_CHAIN[chain]

    logger.info("Processing Morpho Vaults on %s ...", chain.name)
    logger.debug("Vaults: %s", vaults)

    for vault in vaults:
        morpho_contract = client.eth.contract(address=vault.address, abi=ABI_MORPHO)
        check_markets_pending_cap(vault.name, morpho_contract, chain, client)
        check_timelock_and_guardian(vault.name, morpho_contract, chain, client)


def main() -> None:
    get_data_for_chain(Chain.MAINNET)
    get_data_for_chain(Chain.KATANA)
    get_data_for_chain(Chain.BASE)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
