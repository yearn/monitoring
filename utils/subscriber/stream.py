"""Named event streams the continuous subscriber can follow.

A stream is one Envio entity of event occurrences (``TimelockEvent``,
``MorphoGovernanceEvent``, ...) plus where its inbox lives. Every stream shares
the transport, inbox, reconciliation, and report code, and keeps its own SQLite
file, checkpoints, and provenance, so one stream's backlog or failure never
holds back another. Streams are defined next to the protocol they serve.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StreamLabel:
    """Human label for one monitored contract, shown in pilot notices."""

    label: str
    protocol: str


@dataclass(frozen=True)
class EventStream:
    """How to query, store, and describe one Envio event entity.

    Every entity must expose ``id`` (``<chainId>_<blockNumber>_<logIndex>``),
    ``chainId``, ``blockNumber``, ``blockHash``, ``blockTimestamp``, ``logIndex``,
    ``transactionHash``, and ``eventName`` besides its own fields.
    """

    # Short identifier for the CLI, logs, and the notice label ("<name>-subscriber").
    name: str
    # Noun used in notice and report text ("timelock", "Morpho governance").
    noun: str
    # Envio GraphQL entity, e.g. "TimelockEvent".
    entity: str
    # Entity field holding the monitored contract address.
    address_field: str
    # Notice label for that contract ("Timelock", "Vault").
    address_label: str
    # GraphQL selection set for the entity (both transports use it, so rows fingerprint identically).
    fields: str
    db_filename: str
    # SQLite tables are ``<table_prefix>_events``, ``<table_prefix>_changes``, ...
    table_prefix: str
    # Optional entity field stored as the inbox ``operation_id`` and shown in notices.
    operation_field: str | None = None
    # Extra notice lines for one event row payload (e.g. decoded arguments).
    describe: Callable[[dict[str, Any]], list[str]] | None = field(default=None, compare=False)

    def table(self, name: str) -> str:
        """Return this stream's SQLite table called ``name`` (events, changes, checkpoints, log, status)."""
        return f"{self.table_prefix}_{name}"


@dataclass(frozen=True)
class StreamScope:
    """The contracts one stream monitors, and how notices name them."""

    # chain id -> lowercase monitored addresses
    addresses: dict[int, set[str]]
    # (lowercase address, chain id) -> label
    labels: Mapping[tuple[str, int], StreamLabel]
