"""Catch Safe txs that executed onchain without a queued-tx alert.

The queue monitor only sees txs posted to the Safe Queue Service while they wait for
execution. A tx signed offchain and executed directly, one that replaces an alerted
tx at the same nonce, or one proposed and executed between two polls never reaches
that alert. Every execution bumps the Safe's onchain nonce, so each run compares the
nonces executed since the last run with the txs that were alerted:

- A nonce with no alerted (or rule-skipped) tx executed without an alert.
- A nonce with alerted txs is confirmed only when the Queue Service reports one of
  those exact ``safeTxHash`` values as executed. Otherwise a different tx took that
  nonce. The service may lag behind the chain, so an unconfirmed nonce waits one run
  before it alerts.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from web3 import Web3

from utils.cache import get_last_value_for_key_from_file, nonces_filename, write_last_value_to_file
from utils.chains import Chain, safe_network_to_chain_id
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("safe")

# A watched Safe: ``(network_name, safe_address)``.
SafeKey = tuple[str, str]
# A queued tx's dedup id: ``(nonce, lowercased safeTxHash)``.
TxId = tuple[int, str]

SAFE_NONCE_ABI = [
    {"inputs": [], "name": "nonce", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}
]
CACHE_KEY_NONCE_CHECK_PREFIX = "safe_nonce_check"


@dataclass(frozen=True)
class NonceCheckState:
    """How far a Safe's executed nonces have been checked.

    Attributes:
        next_nonce: Every nonce below it is checked, except those in ``awaiting``.
        awaiting: Executed nonces whose alerted tx the Queue Service has not reported as
            executed yet. They alert if still unconfirmed on the next run.
    """

    next_nonce: int
    awaiting: frozenset[int] = frozenset()

    def is_settled(self, nonce: int) -> bool:
        """Return whether ``nonce`` is checked, so its handled entries are no longer needed."""
        return nonce < self.next_nonce and nonce not in self.awaiting


@dataclass
class NonceCheck:
    """One Safe's executed nonces to settle this run.

    Attributes:
        safe: The Safe being checked.
        protocol: Protocol key that picks the alert channel.
        current_nonce: The Safe's onchain nonce read this run.
        previously_awaiting: Nonces that were already unconfirmed on the last run.
        unalerted: Executed nonces with no alerted or rule-skipped tx.
        alerted_hashes: Executed nonces mapped to the alerted ``safeTxHash`` values at them.
    """

    safe: SafeKey
    protocol: str
    current_nonce: int
    previously_awaiting: frozenset[int]
    unalerted: set[int] = field(default_factory=set)
    alerted_hashes: dict[int, set[str]] = field(default_factory=dict)

    def resolve(self, executed_hashes: set[str]) -> tuple[list[int], frozenset[int]]:
        """Split the executed nonces into ones to alert now and ones to wait on.

        Args:
            executed_hashes: Lowercased ``safeTxHash`` values the Queue Service reports executed.

        Returns:
            The nonces that executed without an alert, and the nonces to recheck next run.
        """
        unconfirmed = {nonce for nonce, hashes in self.alerted_hashes.items() if not hashes & executed_hashes}
        overdue = unconfirmed & self.previously_awaiting
        return sorted(self.unalerted | overdue), frozenset(unconfirmed - overdue)


def _state_cache_key(safe: SafeKey) -> str:
    network_name, safe_address = safe
    return f"{CACHE_KEY_NONCE_CHECK_PREFIX}+{network_name}+{safe_address.lower()}"


def load_nonce_check_state(safe: SafeKey) -> NonceCheckState | None:
    """Read a Safe's check progress, stored as ``next_nonce`` or ``next_nonce;n1,n2``.

    Returns:
        The stored state, or ``None`` before the first successful onchain nonce read.
    """
    raw = get_last_value_for_key_from_file(nonces_filename, _state_cache_key(safe))
    if not raw:
        return None
    next_nonce, _, awaiting = str(raw).partition(";")
    if not next_nonce.isdigit():
        return None
    return NonceCheckState(int(next_nonce), frozenset(int(n) for n in awaiting.split(",") if n.isdigit()))


def save_nonce_check_state(safe: SafeKey, state: NonceCheckState) -> None:
    """Persist a Safe's check progress."""
    awaiting = ",".join(str(n) for n in sorted(state.awaiting))
    value = f"{state.next_nonce};{awaiting}" if awaiting else str(state.next_nonce)
    write_last_value_to_file(nonces_filename, _state_cache_key(safe), value)


def read_onchain_nonces(safes: list[SafeKey]) -> dict[SafeKey, int]:
    """Read each Safe's ``nonce()`` with one batched RPC request per chain.

    A chain whose read fails is left out and logged. Its Safes keep their stored
    progress, so the nonces they execute meanwhile are checked once a read succeeds.
    """
    by_chain: dict[int, list[SafeKey]] = defaultdict(list)
    for safe in safes:
        by_chain[safe_network_to_chain_id(safe[0])].append(safe)

    nonces: dict[SafeKey, int] = {}
    for chain_id, chain_safes in by_chain.items():
        try:
            client = ChainManager.get_client(Chain.from_chain_id(chain_id))
            with client.batch_requests() as batch:
                for _, safe_address in chain_safes:
                    contract = client.eth.contract(address=Web3.to_checksum_address(safe_address), abi=SAFE_NONCE_ABI)
                    batch.add(contract.functions.nonce())
                results = client.execute_batch(batch)
            nonces.update({safe: int(value) for safe, value in zip(chain_safes, results, strict=True)})
        except Exception as e:  # noqa: BLE001 - a chain's RPC failure only delays its check
            logger.warning("Could not read Safe nonces on chain %s; checking them next run: %s", chain_id, e)
    return nonces


def plan_nonce_check(
    safe: SafeKey, protocol: str, state: NonceCheckState, current_nonce: int, handled: set[TxId]
) -> NonceCheck:
    """Collect the executed nonces to settle: the newly executed ones and those still awaited.

    Args:
        safe: The Safe being checked.
        protocol: Protocol key that picks the alert channel.
        state: The Safe's stored progress.
        current_nonce: The Safe's onchain nonce read this run.
        handled: The Safe's alerted or rule-skipped txs.
    """
    hashes_by_nonce: dict[int, set[str]] = defaultdict(set)
    for nonce, safe_tx_hash in handled:
        hashes_by_nonce[nonce].add(safe_tx_hash)

    check = NonceCheck(safe, protocol, current_nonce, state.awaiting)
    for nonce in set(range(state.next_nonce, current_nonce)) | state.awaiting:
        if nonce in hashes_by_nonce:
            # An entry without a usable hash (``""``) never matches, so it alerts after the wait.
            check.alerted_hashes[nonce] = hashes_by_nonce[nonce]
        else:
            check.unalerted.add(nonce)
    return check
