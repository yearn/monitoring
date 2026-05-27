"""Tenderly Simulation API client for transaction simulation.

Simulates transactions to get state changes, token transfers, and emitted events
before execution. Used to provide richer context for AI-powered explanations.
"""

import os
from dataclasses import dataclass, field
from typing import Any

from utils.http import fetch_json
from utils.logging import get_logger

logger = get_logger("utils.tenderly.simulation")

TENDERLY_API_KEY = os.getenv("TENDERLY_API_KEY")
TENDERLY_ACCOUNT = os.getenv("TENDERLY_ACCOUNT", "yearn")
TENDERLY_PROJECT = os.getenv("TENDERLY_PROJECT", "sam")


@dataclass(frozen=True)
class AssetChange:
    """A token transfer or balance change from simulation."""

    token_address: str
    token_name: str
    token_symbol: str
    from_address: str
    to_address: str
    amount: str
    raw_amount: str
    decimals: int


@dataclass(frozen=True)
class StateChange:
    """A storage slot change from simulation."""

    contract_address: str
    key: str
    original: str
    dirty: str


@dataclass(frozen=True)
class SimulationResult:
    """Result of a Tenderly transaction simulation."""

    success: bool
    gas_used: int = 0
    asset_changes: list[AssetChange] = field(default_factory=list)
    state_changes: list[StateChange] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


def _get_simulation_url() -> str:
    """Build the Tenderly simulation API URL."""
    return f"https://api.tenderly.co/api/v1/account/{TENDERLY_ACCOUNT}/project/{TENDERLY_PROJECT}/simulate"


def _parse_asset_changes(raw: list[dict[str, Any]]) -> list[AssetChange]:
    """Parse asset changes from simulation response."""
    changes: list[AssetChange] = []
    for item in raw:
        token_info = item.get("token_info", {})
        changes.append(
            AssetChange(
                token_address=token_info.get("contract_address", ""),
                token_name=token_info.get("name", "Unknown"),
                token_symbol=token_info.get("symbol", "???"),
                from_address=item.get("from", ""),
                to_address=item.get("to", ""),
                amount=item.get("amount", "0"),
                raw_amount=item.get("raw_amount", "0"),
                decimals=int(token_info.get("decimals", 18)),
            )
        )
    return changes


def _parse_state_changes(raw: list[dict[str, Any]]) -> list[StateChange]:
    """Parse state diff from simulation response."""
    changes: list[StateChange] = []
    for item in raw:
        address = item.get("address", "")
        for storage_change in item.get("storage_changes", []):
            changes.append(
                StateChange(
                    contract_address=address,
                    key=storage_change.get("key", ""),
                    original=storage_change.get("original", ""),
                    dirty=storage_change.get("dirty", ""),
                )
            )
    return changes


def _merge_balance_override(
    state_objects: dict[str, dict[str, Any]] | None,
    from_address: str,
    value: int,
) -> dict[str, dict[str, Any]]:
    """Return state overrides with a balance for ``from_address`` when value > 0.

    Governance executors (timelocks, Safes) frequently don't hold the ETH a
    value-bearing call forwards, so a faithful simulation would revert with
    "insufficient funds" — a false negative. Granting the sender exactly the
    forwarded ``value`` removes that class of spurious revert without masking a
    genuine logic failure. Caller-supplied overrides win on conflict.
    """
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (state_objects or {}).items()}
    if value > 0 and from_address:
        slot = merged.setdefault(from_address, {})
        slot.setdefault("balance", hex(value))
    return merged


def simulate_transaction(
    target: str,
    calldata: str,
    chain_id: int,
    value: int = 0,
    from_address: str = "0x0000000000000000000000000000000000000000",
    state_objects: dict[str, dict[str, Any]] | None = None,
) -> SimulationResult | None:
    """Simulate a transaction using the Tenderly API.

    Args:
        target: Target contract address.
        calldata: Hex-encoded calldata (with 0x prefix).
        chain_id: Chain ID (e.g. 1 for mainnet).
        value: ETH value in wei.
        from_address: Sender address for simulation context.
        state_objects: Optional Tenderly state overrides keyed by address
            (e.g. ``{addr: {"balance": "0x...", "storage": {...}}}``). Used to
            reduce false reverts — governance executors often don't hold the ETH
            a value-bearing call forwards, and access-gated setters can be
            unblocked by overriding the relevant role/owner slot.

    Returns:
        SimulationResult with parsed state changes and asset transfers,
        or None if simulation fails entirely.
    """
    api_key = os.getenv("TENDERLY_API_KEY")
    if not api_key:
        logger.warning("TENDERLY_API_KEY not set, skipping simulation")
        return None

    overrides = _merge_balance_override(state_objects, from_address, value)

    body: dict[str, Any] = {
        "network_id": str(chain_id),
        "from": from_address,
        "to": target,
        "input": calldata,
        "value": str(value),
        "save": False,
        "save_if_fails": False,
        "simulation_type": "full",
    }
    if overrides:
        body["state_objects"] = overrides

    headers = {"X-Access-Key": api_key}
    url = _get_simulation_url()

    logger.info("Simulating tx: target=%s chain=%s", target, chain_id)
    data = fetch_json(url, method="post", json=body, headers=headers, timeout=30)

    if not data:
        logger.error("Tenderly simulation returned no data")
        return None

    try:
        tx = data.get("transaction", {})
        tx_info = tx.get("transaction_info", {})

        success = tx.get("status", False)
        gas_used = int(tx_info.get("gas_used", 0))
        error_message = tx_info.get("stack_trace", [{}])[0].get("error_reason", "") if not success else ""

        # Parse asset changes
        raw_asset_changes = tx_info.get("asset_changes", []) or []
        asset_changes = _parse_asset_changes(raw_asset_changes)

        # Parse state changes
        raw_state_diff = tx_info.get("state_diff", []) or []
        state_changes = _parse_state_changes(raw_state_diff)

        # Parse logs/events
        logs = tx_info.get("logs", []) or []

        return SimulationResult(
            success=success,
            gas_used=gas_used,
            asset_changes=asset_changes,
            state_changes=state_changes,
            logs=logs,
            error_message=error_message,
            raw_response=data,
        )

    except Exception:
        logger.exception("Failed to parse Tenderly simulation response")
        return None
