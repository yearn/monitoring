from dataclasses import dataclass
from datetime import datetime
from typing import Any

from web3 import Web3

from protocols.morpho._alerts import VaultDiff, due_loss_notice, send_vault_alerts
from protocols.morpho._shared import (
    MORPHO_BLUE_ABI_PATH,
    PROTOCOL,
    MorphoMonitoringError,
    execute_graphql,
    fetch_market_metadata,
    format_amount_at_risk,
    format_countdown,
    format_timelock,
    format_ts,
    get_market_url,
    get_vault_url,
    to_assets_down,
)
from protocols.morpho.config import VAULTS_V1_BY_CHAIN
from utils.abi import load_abi
from utils.alert import AlertSeverity
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
REMOVAL_LOSS_TYPE = "removable_at_loss"
REMOVAL_REMINDER_TYPE = "removable_at_reminder"
ABI_MORPHO = load_abi("protocols/morpho/abi/morpho.json")
ABI_MORPHO_BLUE = load_abi(MORPHO_BLUE_ABI_PATH)


@dataclass(frozen=True)
class MarketGovernanceState:
    """Pending governance state for one market in a V1 vault."""

    vault_address: str
    market_id: str
    pending_cap: int
    pending_cap_timestamp: int
    current_cap: int
    removable_at: int
    # The vault's own supply in the market on Morpho Blue. A removal that executes
    # while this is non-zero writes the position off and the vault takes the loss.
    supply_shares: int = 0
    supply_assets: int = 0
    vault_total_assets: int | None = None


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


def _load_vault_market_ids(
    morpho_contract: Any,
    chain: Chain,
    client: Any,
    block_number: int,
) -> list[bytes]:
    """Load accepted and pending-cap market IDs for one V1 vault."""
    with client.batch_requests() as batch:
        batch.add(morpho_contract.functions.supplyQueueLength().call(block_identifier=block_number))
        batch.add(morpho_contract.functions.withdrawQueueLength().call(block_identifier=block_number))
        lengths = client.execute_batch(batch)
    if len(lengths) != 2:
        raise ValueError(f"Expected 2 queue length responses, got {len(lengths)}")

    supply_length, withdraw_length = lengths
    with client.batch_requests() as batch:
        for index in range(supply_length):
            batch.add(morpho_contract.functions.supplyQueue(index).call(block_identifier=block_number))
        for index in range(withdraw_length):
            batch.add(morpho_contract.functions.withdrawQueue(index).call(block_identifier=block_number))
        queued_markets = client.execute_batch(batch)
    expected_count = supply_length + withdraw_length
    if len(queued_markets) != expected_count:
        raise ValueError(f"Expected {expected_count} queue responses, got {len(queued_markets)}")

    pending_markets = {
        bytes.fromhex(market_id.removeprefix("0x"))
        for market_id in fetch_pending_cap_market_ids(morpho_contract.address, chain)
    }
    return list(set(queued_markets) | pending_markets)


def load_market_governance_states(
    morpho_contract: Any,
    market_ids: list[bytes],
    client: Any,
    block_number: int,
) -> list[MarketGovernanceState]:
    """Batch pending-cap, config, and vault-supply reads for V1 vault markets.

    None of these reads go through a market's IRM, so they can't revert on a broken
    market. ``totalAssets`` does (it values every withdraw-queue market), so it is
    read separately and only feeds the optional "% of vault" figure.
    """
    morpho_address = morpho_contract.functions.MORPHO().call(block_identifier=block_number)
    total_assets = _try_total_assets(morpho_contract, block_number)
    blue = client.eth.contract(address=morpho_address, abi=ABI_MORPHO_BLUE)

    per_market = 4
    with client.batch_requests() as batch:
        for market_id in market_ids:
            batch.add(morpho_contract.functions.pendingCap(market_id).call(block_identifier=block_number))
            batch.add(morpho_contract.functions.config(market_id).call(block_identifier=block_number))
            batch.add(blue.functions.position(market_id, morpho_contract.address).call(block_identifier=block_number))
            batch.add(blue.functions.market(market_id).call(block_identifier=block_number))
        responses = client.execute_batch(batch)
    expected_count = len(market_ids) * per_market
    if len(responses) != expected_count:
        raise ValueError(f"Expected {expected_count} pendingCap/config/position/market responses, got {len(responses)}")

    states = []
    for index, market_id in enumerate(market_ids):
        offset = index * per_market
        pending_cap, pending_timestamp = responses[offset]
        config = responses[offset + 1]
        supply_shares = responses[offset + 2][0]
        market = responses[offset + 3]
        states.append(
            MarketGovernanceState(
                vault_address=morpho_contract.address,
                market_id=Web3.to_hex(market_id),
                pending_cap=pending_cap,
                pending_cap_timestamp=pending_timestamp,
                current_cap=config[0],
                removable_at=config[2],
                supply_shares=supply_shares,
                supply_assets=to_assets_down(supply_shares, market[0], market[1]),
                vault_total_assets=total_assets,
            )
        )
    return states


def _try_total_assets(morpho_contract: Any, block_number: int) -> int | None:
    """Best-effort ``totalAssets()``; None when it reverts (e.g. a withdraw-queue market's IRM reverts)."""
    try:
        return int(morpho_contract.functions.totalAssets().call(block_identifier=block_number))
    except Exception as e:
        logger.warning("totalAssets() failed for vault %s, alerts omit the vault share: %s", morpho_contract.address, e)
        return None


def vault_header(name: str, vault_address: str, chain: Chain) -> str:
    """One-line header for the vault's grouped alert."""
    return f"[{name}]({get_vault_url(vault_address, chain)}) on {chain.name}"


def _check_pending_cap(name: str, state: MarketGovernanceState, chain: Chain, diff: VaultDiff) -> None:
    """Buffer a section for a new pending V1 market cap."""
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
    market_name, decimals = fetch_market_info(state.market_id, chain)
    pending_cap = format_cap(state.pending_cap, decimals)
    queued_for = datetime.fromtimestamp(state.pending_cap_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    if state.current_cap == 0:
        body = f"➕ Adding new market [{market_name}]({market_url}) with cap {pending_cap}.\nQueued for {queued_for}"
    else:
        difference = ((state.pending_cap - state.current_cap) / state.current_cap) * 100
        current_cap = format_cap(state.current_cap, decimals)
        body = (
            f"📊 Updating cap for market [{market_name}]({market_url}): "
            f"{current_cap} → {pending_cap}, difference: {difference:.2f}%.\n"
            f"Queued for {queued_for}"
        )
    diff.alert(AlertSeverity.MEDIUM, body)
    diff.defer(
        write_last_executed_morpho_to_file,
        state.vault_address,
        state.market_id,
        PENDING_CAP_TYPE,
        state.pending_cap_timestamp,
    )


def _removal_loss_body(state: MarketGovernanceState, chain: Chain, *, reminder: bool) -> str:
    """Render the potential-loss section for a queued removal of a market the vault still supplies."""
    market_url = get_market_url(state.market_id, chain)
    metadata = fetch_market_metadata(state.market_id, chain)
    if metadata:
        market_label = f"{metadata['name']} ({metadata['lltv'] / 1e18 * 100:.2f}%)"
        decimals, symbol = metadata["loan_decimals"], metadata["loan_symbol"]
    else:
        market_label, decimals, symbol = state.market_id, None, None
    at_risk = format_amount_at_risk(state.supply_assets, decimals, symbol, state.vault_total_assets)
    title = "⏳ Reminder: market removal is close" if reminder else "🔴 Market removal queued with funds still supplied"
    return (
        f"{title}\n"
        f"Market: [{market_label}]({market_url})\n"
        f"💸 At risk: {at_risk}\n"
        f"⏰ Removable at: {format_ts(state.removable_at)} {format_countdown(state.removable_at)}\n"
        "Removing the market writes off the remaining supply. Withdraw it before the removal, "
        "or have the guardian/curator call `revokePendingMarketRemoval`."
    )


def _check_market_removal(state: MarketGovernanceState, chain: Chain, diff: VaultDiff) -> None:
    """Buffer a section for a queued V1 market removal.

    A removal of a market the vault still supplies is a HIGH potential loss, for
    any amount: once ``removableAt`` passes, ``updateWithdrawQueue`` can drop the
    market and the supply leaves ``totalAssets``. It is re-alerted once inside the
    reminder window, and a removal first seen on an empty market escalates as soon
    as the vault supplies it. A removal of an empty market is routine cleanup and
    stays MEDIUM.
    """
    if state.removable_at <= 0:
        return
    last_timestamp = get_last_executed_morpho_from_file(
        state.vault_address,
        state.market_id,
        REMOVABLE_AT_TYPE,
    )
    seen = state.removable_at <= last_timestamp
    if state.supply_shares > 0:
        _check_removal_loss(state, chain, diff)
    elif not seen:
        market_url = get_market_url(state.market_id, chain)
        market_name, _ = fetch_market_info(state.market_id, chain)
        diff.alert(
            AlertSeverity.MEDIUM,
            f"➖ Queued to remove market [{market_name}]({market_url}) at {format_ts(state.removable_at)} "
            "(no vault supply in the market)",
        )
    else:
        logger.info(
            "Skipping previously alerted market removal for vault %s market %s",
            state.vault_address,
            state.market_id,
        )
    if not seen:
        diff.defer(
            write_last_executed_morpho_to_file,
            state.vault_address,
            state.market_id,
            REMOVABLE_AT_TYPE,
            state.removable_at,
        )


def _check_removal_loss(state: MarketGovernanceState, chain: Chain, diff: VaultDiff) -> None:
    """Alert once on a queued removal of a supplied market, then once more near ``removableAt``."""
    vault, market = state.vault_address, state.market_id
    notice = due_loss_notice(
        state.removable_at,
        get_last_executed_morpho_from_file(vault, market, REMOVAL_LOSS_TYPE),
        get_last_executed_morpho_from_file(vault, market, REMOVAL_REMINDER_TYPE),
    )
    if notice is None:
        return
    diff.loss(_removal_loss_body(state, chain, reminder=notice.reminder))
    diff.defer(write_last_executed_morpho_to_file, vault, market, REMOVAL_LOSS_TYPE, state.removable_at)
    if notice.in_window:
        diff.defer(write_last_executed_morpho_to_file, vault, market, REMOVAL_REMINDER_TYPE, state.removable_at)


def check_market_governance_state(name: str, state: MarketGovernanceState, chain: Chain, diff: VaultDiff) -> None:
    """Check pending cap and removal changes for one V1 market."""
    _check_pending_cap(name, state, chain, diff)
    _check_market_removal(state, chain, diff)


def check_markets_pending_cap(
    name: str,
    morpho_contract: Any,
    chain: Chain,
    client: Any,
    diff: VaultDiff,
    *,
    block_number: int,
) -> None:
    """Check V1 market cap and removal governance for one vault."""
    market_ids = _load_vault_market_ids(morpho_contract, chain, client, block_number)
    for state in load_market_governance_states(morpho_contract, market_ids, client, block_number):
        check_market_governance_state(name, state, chain, diff)


def check_pending_role_change(
    morpho_contract: Any,
    role_type: str,
    timestamp: int,
    diff: VaultDiff,
    message: str | None = None,
) -> None:
    """Alert HIGH once per pending ``role_type`` change, keyed by its ``validAt`` timestamp."""
    market_id = ""  # use empty string for all markets because the value is used per vault
    if timestamp > get_last_executed_morpho_from_file(morpho_contract.address, market_id, role_type):
        diff.alert(AlertSeverity.HIGH, message or f"🚨 {role_type.capitalize()} is changing")
        diff.defer(write_last_executed_morpho_to_file, morpho_contract.address, market_id, role_type, timestamp)


def format_timelock_decrease(current: int, new: int, valid_at: int) -> str:
    """Render a pending V1 timelock change; only decreases are ever pending on V1."""
    return (
        f"🚨 Timelock decrease: {format_timelock(current)} → {format_timelock(new)}\n"
        f"⏰ Executable at: {format_ts(valid_at)} {format_countdown(valid_at)}\n"
        "A shorter timelock shrinks the window to react to cap changes and market removals."
    )


def check_timelock_and_guardian(
    morpho_contract: Any,
    client: Any,
    diff: VaultDiff,
    block_number: int,
) -> None:
    with morpho_contract.w3.batch_requests() as batch:
        batch.add(morpho_contract.functions.pendingTimelock().call(block_identifier=block_number))
        batch.add(morpho_contract.functions.pendingGuardian().call(block_identifier=block_number))
        batch.add(morpho_contract.functions.timelock().call(block_identifier=block_number))
        responses = client.execute_batch(batch)
        if len(responses) != 3:
            raise ValueError(f"Expected 3 responses from batch, got {len(responses)}")

    pending_timelock_value, timelock_valid_at = responses[0]
    guardian_valid_at = responses[1][1]  # [1] to get the timestamp
    current_timelock = responses[2]

    timelock_message = None
    if timelock_valid_at > 0:
        timelock_message = format_timelock_decrease(current_timelock, pending_timelock_value, timelock_valid_at)
    check_pending_role_change(morpho_contract, "timelock", timelock_valid_at, diff, timelock_message)
    check_pending_role_change(morpho_contract, "guardian", guardian_valid_at, diff)


def get_data_for_chain(chain: Chain) -> None:
    client = ChainManager.get_client(chain)
    vaults = VAULTS_V1_BY_CHAIN[chain]

    logger.info("Processing Morpho Vaults on %s ...", chain.name)
    logger.debug("Vaults: %s", vaults)
    block_number = int(client.eth.block_number)

    for vault in vaults:
        morpho_contract = client.eth.contract(address=vault.address, abi=ABI_MORPHO)
        # Buffer every finding for this vault, then send them as one message and
        # only then record them as alerted.
        diff = VaultDiff()
        check_markets_pending_cap(
            vault.name,
            morpho_contract,
            chain,
            client,
            diff,
            block_number=block_number,
        )
        check_timelock_and_guardian(morpho_contract, client, diff, block_number)
        send_vault_alerts(vault_header(vault.name, vault.address, chain), diff.alerts, PROTOCOL)
        diff.commit()


def main() -> None:
    """Check V1 governance on every configured chain, continuing past per-chain failures."""
    failures: list[str] = []
    for chain in VAULTS_V1_BY_CHAIN:
        try:
            get_data_for_chain(chain)
        except Exception as e:
            logger.exception("Failed to check Morpho V1 governance on %s", chain.name)
            failures.append(f"{chain.name}: {type(e).__name__}: {e}")

    if failures:
        raise MorphoMonitoringError("Failed Morpho V1 governance checks: " + "; ".join(failures))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
