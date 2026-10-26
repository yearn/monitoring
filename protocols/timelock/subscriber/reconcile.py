"""Bounded, paginated HTTP reconciliation with independent per-chain checkpoints.

Each cycle reads ``chain_metadata`` once, then for every monitored chain fetches
``(checkpoint - overlap, head]`` in chunks of at most ``max_block_span`` blocks,
each chunk paginated. Block numbers, not timestamps, bound the ranges: two events
in one block (or in different blocks with the same timestamp) can never be skipped
by a strict ``_gt`` cursor, and one chain's progress never gates another's.

The overlap re-reads recent blocks every cycle. That catches rows Envio indexed
late, rows a reorg replaced (same id, new fingerprint), and rows a reorg dropped
(missing from a fully fetched range). The checkpoint advances only past chunks
that were fetched completely.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from protocols.timelock.subscriber.envio import ChainHead, EnvioQueryError, GraphQLPost, fetch_chain_heads, fetch_range
from protocols.timelock.subscriber.inbox import SOURCE_HTTP, EventInbox
from utils.logger import get_logger

_logger = get_logger("timelock.subscriber.reconcile")

# Blocks re-read below the checkpoint on every cycle, about 15-20 minutes per
# chain: deeper than any reorg seen on these chains, and long enough to cover an
# indexer that commits a block's rows slightly after advancing its head.
REORG_OVERLAP_BLOCKS: dict[int, int] = {
    1: 64,
    137: 512,
    8453: 600,
    42161: 4800,
    747474: 1200,
}
DEFAULT_REORG_OVERLAP_BLOCKS = 1000

# Largest block range fetched in one chunk (roughly a week of blocks per chain).
# Timelock events are rare, so a chunk is normally one page; a backlog after a long
# outage drains in several bounded chunks instead of one unbounded scan.
MAX_BLOCK_SPAN: dict[int, int] = {
    1: 50_000,
    137: 300_000,
    8453: 300_000,
    42161: 2_400_000,
    747474: 600_000,
}
DEFAULT_MAX_BLOCK_SPAN = 500_000


@dataclass(frozen=True)
class ReconcileConfig:
    """Paging and backlog bounds for one reconcile cycle."""

    page_size: int = 200
    max_pages: int = 50
    # Chunks per chain per cycle; the rest of a backlog waits for the next cycle so
    # the worker keeps heart-beating during a long catch-up.
    max_chunks_per_cycle: int = 20
    # On a chain's first reconcile (no checkpoint yet), backfill this many seconds
    # of history. 0 starts at the indexer's current head.
    start_lookback_seconds: int = 0
    overlap_blocks: dict[int, int] = field(default_factory=lambda: dict(REORG_OVERLAP_BLOCKS))
    max_block_span: dict[int, int] = field(default_factory=lambda: dict(MAX_BLOCK_SPAN))


@dataclass
class ChainReconcileResult:
    """What one chain's reconcile did."""

    chain_id: int
    from_block: int = 0
    to_block: int = 0
    head: int = 0
    lag_blocks: int = 0
    pages: int = 0
    rows: int = 0
    new: int = 0
    duplicate: int = 0
    changed: int = 0
    restored: int = 0
    removed: int = 0
    caught_up: bool = False
    initialized: bool = False
    error: str | None = None
    new_ids: list[str] = field(default_factory=list)


@dataclass
class ReconcileReport:
    """One reconcile cycle across every monitored chain."""

    started_at: float
    duration_ms: float = 0.0
    error: str | None = None
    chains: list[ChainReconcileResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when chain heads were read and no chain failed."""
        return self.error is None and all(c.error is None for c in self.chains)


def build_scope(timelocks: Iterable[Any]) -> dict[int, set[str]]:
    """Map chain id to the lowercase timelock addresses monitored on it."""
    scope: dict[int, set[str]] = {}
    for timelock in timelocks:
        scope.setdefault(int(timelock.chain_id), set()).add(str(timelock.address).lower())
    return scope


def in_scope(event: dict[str, Any], scope: dict[int, set[str]]) -> bool:
    """True when the row's (chain, timelock) pair is monitored.

    The same address is monitored on some chains only, so a query filtered by
    address and chain separately can return pairs outside the scope.
    """
    return str(event.get("timelockAddress", "")).lower() in scope.get(int(event.get("chainId", -1)), set())


def reconcile_chain(
    post: GraphQLPost,
    inbox: EventInbox,
    chain_id: int,
    addresses: set[str],
    head: ChainHead,
    config: ReconcileConfig,
    now: Callable[[], float] = time.time,
) -> ChainReconcileResult:
    """Fetch one chain's unreconciled range and advance its checkpoint chunk by chunk.

    Raises:
        EnvioQueryError: A page failed. Chunks completed before it stay committed.
    """
    result = ChainReconcileResult(chain_id=chain_id, head=head.latest_processed_block, lag_blocks=head.lag_blocks)
    head_block = head.latest_processed_block
    checkpoint = inbox.get_checkpoint(chain_id)

    if checkpoint is None:
        result.initialized = True
        result.from_block = result.to_block = head_block
        if config.start_lookback_seconds > 0:
            since_ts = int(now()) - config.start_lookback_seconds
            rows, pages = fetch_range(
                post, chain_id, addresses, -1, head_block, config.page_size, config.max_pages, since_ts=since_ts
            )
            _fold(inbox, rows, result, pages, now())
        inbox.set_checkpoint(chain_id, head_block, head_block, now())
        result.caught_up = True
        return result

    if head_block < checkpoint.block_number:
        # Indexer rewound (resync) or reports a stale head; nothing new is final yet.
        _logger.warning("chain %s head %s is behind checkpoint %s", chain_id, head_block, checkpoint.block_number)
        inbox.set_checkpoint(chain_id, checkpoint.block_number, head_block, now())
        result.from_block = result.to_block = checkpoint.block_number
        return result

    overlap = config.overlap_blocks.get(chain_id, DEFAULT_REORG_OVERLAP_BLOCKS)
    span = config.max_block_span.get(chain_id, DEFAULT_MAX_BLOCK_SPAN)
    start = max(-1, checkpoint.block_number - overlap)
    result.from_block = start
    cursor = checkpoint.block_number
    lower = start
    for _ in range(config.max_chunks_per_cycle):
        upper = min(head_block, cursor + span)
        rows, pages = fetch_range(post, chain_id, addresses, lower, upper, config.page_size, config.max_pages)
        observed_at = now()
        seen = _fold(inbox, rows, result, pages, observed_at)
        removed = inbox.mark_missing(chain_id, lower + 1, upper, seen, SOURCE_HTTP, observed_at)
        if removed:
            _logger.warning("chain %s: %s events no longer indexed (reorg?): %s", chain_id, len(removed), removed)
        result.removed += len(removed)
        inbox.set_checkpoint(chain_id, upper, head_block, observed_at)
        result.to_block = upper
        cursor = lower = upper
        if upper >= head_block:
            result.caught_up = True
            break
    return result


def _fold(
    inbox: EventInbox, rows: list[dict[str, Any]], result: ChainReconcileResult, pages: int, at: float
) -> set[str]:
    """Observe fetched rows and add their counts to ``result``; return the ids seen."""
    observed = inbox.observe(rows, SOURCE_HTTP, at)
    result.pages += pages
    result.rows += len(rows)
    result.new += observed.new
    result.duplicate += observed.duplicate
    result.changed += observed.changed
    result.restored += observed.restored
    result.new_ids.extend(observed.new_ids)
    return {str(row["id"]) for row in rows}


def reconcile_all(
    post: GraphQLPost,
    inbox: EventInbox,
    scope: dict[int, set[str]],
    config: ReconcileConfig,
    now: Callable[[], float] = time.time,
) -> ReconcileReport:
    """Run one reconcile cycle over every monitored chain and log its timing.

    A failure on one chain is recorded and the others still run, so each chain's
    checkpoint advances on its own.
    """
    report = ReconcileReport(started_at=now())
    clock = time.perf_counter()
    try:
        heads = fetch_chain_heads(post)
    except EnvioQueryError as exc:
        report.error = str(exc)
        heads = {}

    if report.error is None:
        for chain_id in sorted(scope):
            head = heads.get(chain_id)
            if head is None:
                report.chains.append(ChainReconcileResult(chain_id=chain_id, error="no chain_metadata row"))
                continue
            try:
                report.chains.append(reconcile_chain(post, inbox, chain_id, scope[chain_id], head, config, now))
            except EnvioQueryError as exc:
                report.chains.append(
                    ChainReconcileResult(chain_id=chain_id, head=head.latest_processed_block, error=str(exc))
                )

    report.duration_ms = (time.perf_counter() - clock) * 1000
    inbox.log(
        "reconcile",
        now(),
        report.duration_ms,
        error=report.error,
        chains=[
            {
                "chain_id": c.chain_id,
                "from": c.from_block,
                "to": c.to_block,
                "head": c.head,
                "lag_blocks": c.lag_blocks,
                "pages": c.pages,
                "rows": c.rows,
                "new": c.new,
                "changed": c.changed,
                "restored": c.restored,
                "removed": c.removed,
                "caught_up": c.caught_up,
                "error": c.error,
            }
            for c in report.chains
        ],
    )
    return report
