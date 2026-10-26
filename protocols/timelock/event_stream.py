"""The ``TimelockEvent`` stream followed by the continuous event subscriber."""

from protocols.timelock.timelock_alerts import TIMELOCK_LIST
from utils.subscriber.reconcile import build_scope
from utils.subscriber.stream import EventStream, StreamLabel, StreamScope

# The scheduled monitor's selection plus logIndex and blockHash. Both transports use
# it, so a row fingerprints identically whichever one fetched it, and a reorg that
# replaces the block changes the fingerprint through blockHash.
TIMELOCK_FIELDS = """
        id
        timelockAddress
        timelockType
        eventName
        chainId
        blockNumber
        blockHash
        blockTimestamp
        logIndex
        transactionHash
        operationId
        index
        target
        value
        data
        predecessor
        delay
        signature
        creator
        metadata
        votesFor
        votesAgainst
"""

# Database file and table names predate named streams; keeping them lets an
# existing pilot inbox continue with its checkpoints and history.
TIMELOCK_STREAM = EventStream(
    name="timelock",
    noun="timelock",
    entity="TimelockEvent",
    address_field="timelockAddress",
    address_label="Timelock",
    fields=TIMELOCK_FIELDS,
    db_filename="timelock_subscriber.db",
    table_prefix="timelock_sub",
    operation_field="operationId",
)


def timelock_scope() -> StreamScope:
    """Return every monitored timelock, keyed by chain, with its notice label."""
    return StreamScope(
        addresses=build_scope(TIMELOCK_LIST),
        labels={(t.address.lower(), t.chain_id): StreamLabel(t.label, t.protocol) for t in TIMELOCK_LIST},
    )
