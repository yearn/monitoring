"""Morpho VaultV2 governance monitor — GraphQL snapshot diff.

Like v1's ``governance.py`` (which polls ``pendingTimelock``/``pendingGuardian``/
``pendingCap`` on-chain), this monitor pulls the *current* governance state on
every run and diffs it against the cached snapshot. No event-log polling, so
RPC usage stays bounded to a single GraphQL query per chain.

Detected and alerted:

* **Pending timelocked operations** (``vaultV2s.pendingConfigs``) — appearance,
  execution, and revocation. Each pending config is identified by
  ``keccak256(data)`` so two adapters with identical calldata don't collide.
* **Owner / curator changes** — instant, no timelock.
* **Sentinels / allocators / adapters** — added or removed.

NOT covered: ``MorphoMarketV1AdapterV2``'s own internal timelock system. The
GraphQL API does not surface adapter-internal pending operations; replaying
their Submit/Accept/Revoke events would reintroduce the RPC cost we're
explicitly avoiding. Phase-2 candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from web3 import Web3

from protocols.morpho._shared import (
    PROTOCOL,
    MorphoV2MonitoringError,
    execute_graphql,
    get_vault_url,
    require_configured_keys,
)
from protocols.morpho.config import VAULTS_V2_BY_CHAIN, get_vault_query_config
from protocols.morpho.v2_decoders import decode_submit, submit_data_key
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    get_last_value_for_key_from_file,
    morpho_filename,
    morpho_key,
    write_last_value_to_file,
)
from utils.chains import Chain
from utils.logger import get_logger

logger = get_logger("morpho.governance_v2")

# Cache value-type tags used with utils.cache.morpho_key.
PENDING_TYPE = "v2_pending"
PENDING_INDEX_TYPE = "v2_pending_index"
PENDING_FUNCTION_TYPE = "v2_pending_function"
ROLE_TYPE = "v2_role"
SET_TYPE = "v2_set"

# Sentinel cache values for pending operations:
# * positive int: validAt of the latest pending submission we've alerted on
# * -1: pending config was executed (Accept), don't re-alert if it reappears
# * 0: pending config was revoked (Revoke), allow new Submit alerts to fire
EXECUTED = -1
REVOKED = 0

_GOVERNANCE_QUERY = """
query GovernanceV2($addresses: [String!]!, $chainIds: [Int!]!) {
  vaultV2s(first: 200, where: { address_in: $addresses, chainId_in: $chainIds }) {
    items {
      address
      name
      chain { id }
      owner { address }
      curator { address }
      sentinels { sentinel { address } }
      allocators { allocator { address } }
      adapters { items { address type } }
      pendingConfigs {
        items { validAt functionName data txHash }
      }
    }
  }
}
"""


# ----------------------------------------------------------------------------
# Snapshot dataclasses
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingConfig:
    """One pending timelocked operation reported by Morpho GraphQL."""

    valid_at: int
    function_name: str
    data: bytes
    tx_hash: str

    @property
    def data_hash(self) -> str:
        """Stable cache-key hash for this pending operation."""
        return str(submit_data_key(self.data))


@dataclass
class V2GovernanceSnapshot:
    """Per-vault governance state snapshot used for diffing against cache."""

    name: str
    address: str  # checksummed
    chain: Chain
    owner: str  # checksummed
    curator: str
    sentinels: List[str]  # checksummed, sorted
    allocators: List[str]
    adapters: List[str]
    pending_configs: List[PendingConfig] = field(default_factory=list)


# ----------------------------------------------------------------------------
# GraphQL fetch
# ----------------------------------------------------------------------------


def _hex_to_bytes(value: str) -> bytes:
    if value.startswith("0x"):
        value = value[2:]
    return bytes.fromhex(value)


def _checksum_or_empty(value: str) -> str:
    if not value:
        return ""
    return str(Web3.to_checksum_address(value))


def fetch_governance_snapshots() -> Dict[Chain, List[V2GovernanceSnapshot]]:
    """GraphQL fetch of governance state for every vault in ``VAULTS_V2_BY_CHAIN``.

    Issues a single ``vaultV2s(where: { address_in })`` query and joins the
    result back to the static list to inherit the configured risk level.
    """
    addr_to_meta, addresses, chain_ids = get_vault_query_config(VAULTS_V2_BY_CHAIN)

    if not addresses:
        return {}

    data = execute_graphql(
        _GOVERNANCE_QUERY,
        {"addresses": addresses, "chainIds": chain_ids},
        "Vault V2 governance",
        error_type=MorphoV2MonitoringError,
    )
    items = data.get("vaultV2s", {}).get("items") or []
    by_addr: dict[str, dict[str, Any]] = {item["address"].lower(): item for item in items}
    require_configured_keys(
        addr_to_meta,
        by_addr,
        "Vault V2 governance addresses",
        error_type=MorphoV2MonitoringError,
    )

    result: Dict[Chain, List[V2GovernanceSnapshot]] = {chain: [] for chain in VAULTS_V2_BY_CHAIN}

    for addr_lc, (chain, config) in addr_to_meta.items():
        item = by_addr[addr_lc]

        sentinels = [_checksum_or_empty(s["sentinel"]["address"]) for s in (item.get("sentinels") or [])]
        allocators = [_checksum_or_empty(a["allocator"]["address"]) for a in (item.get("allocators") or [])]
        adapters = [_checksum_or_empty(a["address"]) for a in ((item.get("adapters") or {}).get("items") or [])]
        pending = [
            PendingConfig(
                valid_at=int(pc["validAt"]),
                function_name=pc["functionName"],
                data=_hex_to_bytes(pc["data"]),
                tx_hash=pc["txHash"],
            )
            for pc in ((item.get("pendingConfigs") or {}).get("items") or [])
        ]

        result.setdefault(chain, []).append(
            V2GovernanceSnapshot(
                name=config.name,
                address=_checksum_or_empty(item["address"]),
                chain=chain,
                owner=_checksum_or_empty((item.get("owner") or {}).get("address") or ""),
                curator=_checksum_or_empty((item.get("curator") or {}).get("address") or ""),
                sentinels=sorted(sentinels),
                allocators=sorted(allocators),
                adapters=sorted(adapters),
                pending_configs=pending,
            )
        )

    for chain, snapshots in result.items():
        logger.info("Loaded governance snapshot for %d V2 vault(s) on %s", len(snapshots), chain.name)
    return result


# ----------------------------------------------------------------------------
# Cache helpers
# ----------------------------------------------------------------------------


def _read_int(key: str) -> int:
    raw = get_last_value_for_key_from_file(morpho_filename, key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _read_str(key: str) -> str:
    raw = get_last_value_for_key_from_file(morpho_filename, key)
    return str(raw) if raw else ""


def _write(key: str, value: Any) -> None:
    write_last_value_to_file(morpho_filename, key, value)


# ----------------------------------------------------------------------------
# Alerting
# ----------------------------------------------------------------------------


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _format_countdown(ts: int) -> str:
    """Human-friendly time remaining until ``ts``, e.g. ``(3 days)``."""
    seconds = ts - int(datetime.now().timestamp())
    if seconds <= 0:
        return "(now)"
    days = seconds / 86400
    if days >= 1:
        n = round(days)
        return f"({n} day{'s' if n != 1 else ''})"
    hours = seconds / 3600
    if hours >= 1:
        n = round(hours)
        return f"({n} hour{'s' if n != 1 else ''})"
    n = max(round(seconds / 60), 1)
    return f"({n} minute{'s' if n != 1 else ''})"


def _explorer_link(chain: Chain, tx_hash: str) -> str:
    base = chain.explorer_url
    if not base or not tx_hash:
        return tx_hash
    return f"[{tx_hash}…]({base}/tx/{tx_hash})"


def _operation_label(snapshot: V2GovernanceSnapshot, pc: PendingConfig) -> str:
    decoded = decode_submit(pc.data, snapshot.chain)
    if decoded:
        return str(decoded)
    return pc.function_name or f"`{pc.data_hash[:10]}…`"


def _operation_function_name(pc: PendingConfig, operation_label: str) -> str:
    if pc.function_name:
        return pc.function_name.split("(", 1)[0]
    if "(" in operation_label:
        return operation_label.split("(", 1)[0]
    return "" if operation_label.startswith("<") else operation_label


def _pending_function_key(snapshot: V2GovernanceSnapshot, data_hash: str) -> str:
    return str(morpho_key(snapshot.address.lower(), data_hash, PENDING_FUNCTION_TYPE))


def _alert_pending_new(snapshot: V2GovernanceSnapshot, pc: PendingConfig, operation_label: str) -> None:
    send_alert(
        Alert(
            AlertSeverity.MEDIUM,
            f"⏳ V2 [{snapshot.name}]({get_vault_url(snapshot.address, snapshot.chain)}) "
            f"on {snapshot.chain.name}\n"
            f"📥 Submitted: {operation_label}\n"
            f"⏰ Executable at: {_format_ts(pc.valid_at)} {_format_countdown(pc.valid_at)}\n"
            f"🔗 Tx: {_explorer_link(snapshot.chain, pc.tx_hash)}",
            PROTOCOL,
        )
    )


def _alert_pending_resolved(
    snapshot: V2GovernanceSnapshot,
    data_hash: str,
    last_valid_at: int,
    function_name: str,
) -> None:
    """Alert that a previously-pending operation no longer appears in pendingConfigs.

    We can't always distinguish ``Accept`` from ``Revoke`` from a snapshot diff,
    but ``validAt`` gives a strong hint: if it has elapsed, the operation was
    almost certainly executed; otherwise it was almost certainly revoked.
    """
    now = int(datetime.now().timestamp())
    verb = "executed" if last_valid_at <= now else "revoked"
    icon = "✅" if verb == "executed" else "🛑"
    operation = f"`{function_name}()`" if function_name else f"`{data_hash[:10]}…`"
    send_alert(
        Alert(
            AlertSeverity.LOW,
            f"{icon} V2 [{snapshot.name}]({get_vault_url(snapshot.address, snapshot.chain)}) "
            f"on {snapshot.chain.name}\n"
            f"Pending operation {operation} was {verb} "
            f"(was due {_format_ts(last_valid_at)}).",
            PROTOCOL,
        )
    )


def _alert_role_change(snapshot: V2GovernanceSnapshot, role: str, before: str, after: str) -> None:
    icon = "👑" if role == "owner" else "🎩"
    send_alert(
        Alert(
            AlertSeverity.HIGH,
            f"🚨 V2 [{snapshot.name}]({get_vault_url(snapshot.address, snapshot.chain)}) "
            f"on {snapshot.chain.name}\n"
            f"{icon} {role.capitalize()} changed: `{before}` → `{after}`",
            PROTOCOL,
        )
    )


def _alert_set_diff(
    snapshot: V2GovernanceSnapshot,
    set_name: str,
    added: set[str],
    removed: set[str],
) -> None:
    icon = {"sentinels": "🛡️", "allocators": "🎯", "adapters": "🧩"}.get(set_name, "ℹ️")
    lines: list[str] = []
    for addr in sorted(added):
        lines.append(f"  + `{addr}`")
    for addr in sorted(removed):
        lines.append(f"  − `{addr}`")
    send_alert(
        Alert(
            AlertSeverity.LOW,
            f"{icon} V2 [{snapshot.name}]({get_vault_url(snapshot.address, snapshot.chain)}) "
            f"{set_name} changed on {snapshot.chain.name}\n" + "\n".join(lines),
            PROTOCOL,
        )
    )


# ----------------------------------------------------------------------------
# Diff logic
# ----------------------------------------------------------------------------


def _diff_pending(snapshot: V2GovernanceSnapshot) -> None:
    addr = snapshot.address.lower()

    current_keys: set[str] = set()
    for pc in snapshot.pending_configs:
        current_keys.add(pc.data_hash)
        operation_label = _operation_label(snapshot, pc)
        _write(_pending_function_key(snapshot, pc.data_hash), _operation_function_name(pc, operation_label))
        cache_key = morpho_key(addr, pc.data_hash, PENDING_TYPE)
        last = _read_int(cache_key)
        # Already alerted at this validAt, or marked executed.
        if last == pc.valid_at or last == EXECUTED:
            continue
        _alert_pending_new(snapshot, pc, operation_label)
        _write(cache_key, pc.valid_at)

    # Detect resolved entries: anything in last-run's index that isn't in the
    # current pending list.
    index_key = morpho_key(addr, "pending_keys", PENDING_INDEX_TYPE)
    previous_index = _read_str(index_key)
    previous_keys = {h for h in previous_index.split(",") if h} if previous_index else set()
    resolved = previous_keys - current_keys
    for data_hash in resolved:
        cache_key = morpho_key(addr, data_hash, PENDING_TYPE)
        last = _read_int(cache_key)
        if last <= 0:
            # Already marked executed/revoked.
            continue
        _alert_pending_resolved(snapshot, data_hash, last, _read_str(_pending_function_key(snapshot, data_hash)))
        _write(cache_key, EXECUTED if last <= int(datetime.now().timestamp()) else REVOKED)

    _write(index_key, ",".join(sorted(current_keys)))


def _diff_single_role(snapshot: V2GovernanceSnapshot, role: str, current: str) -> None:
    cache_key = morpho_key(snapshot.address.lower(), role, ROLE_TYPE)
    last = _read_str(cache_key)
    cur_lc = current.lower()
    if last and last != cur_lc:
        _alert_role_change(snapshot, role, last, current)
    _write(cache_key, cur_lc)


def _diff_set(snapshot: V2GovernanceSnapshot, set_name: str, current: List[str]) -> None:
    cache_key = morpho_key(snapshot.address.lower(), set_name, SET_TYPE)
    last_str = _read_str(cache_key)
    last_set = {a for a in last_str.split(",") if a} if last_str else set()
    current_set = {addr.lower() for addr in current}
    added = current_set - last_set
    removed = last_set - current_set
    # Only alert if we have a baseline — first run seeds the cache silently.
    if last_str and (added or removed):
        added_cs: set[str] = {str(Web3.to_checksum_address(a)) for a in added}
        removed_cs: set[str] = {str(Web3.to_checksum_address(a)) for a in removed}
        _alert_set_diff(snapshot, set_name, added_cs, removed_cs)
    _write(cache_key, ",".join(sorted(current_set)))


def diff_and_alert(snapshot: V2GovernanceSnapshot) -> None:
    """Diff a vault's snapshot against persisted state and emit Telegram alerts."""
    _diff_pending(snapshot)
    _diff_single_role(snapshot, "owner", snapshot.owner)
    _diff_single_role(snapshot, "curator", snapshot.curator)
    _diff_set(snapshot, "sentinels", snapshot.sentinels)
    _diff_set(snapshot, "allocators", snapshot.allocators)
    _diff_set(snapshot, "adapters", snapshot.adapters)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main() -> None:
    """Pull governance snapshots from GraphQL and alert on diffs vs. the cache."""
    logger.info("Checking Morpho V2 governance...")
    snapshots_by_chain = fetch_governance_snapshots()
    if not any(snapshots_by_chain.values()):
        logger.info("No matching V2 vaults found; nothing to monitor yet.")
        return

    failures: List[str] = []
    for chain, vaults in snapshots_by_chain.items():
        if not vaults:
            continue
        for vault in vaults:
            try:
                diff_and_alert(vault)
            except Exception as e:
                logger.exception("Failed to process governance for %s on %s", vault.address, chain.name)
                failures.append(f"{vault.name} on {chain.name}: {type(e).__name__}: {e}")

    if failures:
        raise MorphoV2MonitoringError("Failed Morpho Vault V2 governance checks: " + "; ".join(failures))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
