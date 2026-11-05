"""Envio GraphQL queries for the event subscriber: chain heads, ranges, and the live subscription."""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from eth_utils import to_checksum_address

from utils.http_client import request_with_retry
from utils.subscriber.stream import EventStream

CHAIN_METADATA_QUERY = "{ chain_metadata { chain_id latest_processed_block block_height } }"


class EnvioQueryError(Exception):
    """The Envio endpoint was unreachable or answered with GraphQL errors."""


@dataclass(frozen=True)
class ChainHead:
    """Indexer progress for one chain, as reported by ``chain_metadata``."""

    chain_id: int
    latest_processed_block: int
    block_height: int

    @property
    def lag_blocks(self) -> int:
        """Blocks between the chain head Envio knows about and what it has processed."""
        return max(0, self.block_height - self.latest_processed_block)


# (query, variables) -> response payload. Injected so tests can stand in for Envio.
GraphQLPost = Callable[[str, dict[str, Any]], dict[str, Any]]


def http_poster(url: str, timeout: int = 30) -> GraphQLPost:
    """Return a GraphQL POST function bound to ``url`` with retries on transient errors.

    Requests go through one persistent ``requests.Session`` per calling thread, so
    every stream's reconcile cycle reuses pooled TCP/TLS connections without
    sharing a session across threads.
    """
    local = threading.local()

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        try:
            response = request_with_retry(
                "post",
                url,
                timeout=timeout,
                log_errors=False,
                session=session,
                json={"query": query, "variables": variables},
            )
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EnvioQueryError(f"GraphQL request failed: {exc}") from exc

    return post


def entity_rows(payload: Any, field: str) -> list[dict[str, Any]]:
    """Return ``payload["data"][field]``, failing closed on anything but a list of rows.

    Reconciliation treats a returned page as the complete content of its block
    range: rows missing from it are marked removed and the checkpoint moves past
    it. So only an explicit list (``[]`` included) may count as a page. A missing
    or null ``data``, a missing or null ``field``, or a non-object row is a broken
    response, not an empty range, and raises before anything is changed.

    Raises:
        EnvioQueryError: GraphQL ``errors``, or a response without the expected list.
    """
    if not isinstance(payload, dict):
        raise EnvioQueryError(f"GraphQL response is not an object: {str(payload)[:200]}")
    if payload.get("errors"):
        raise EnvioQueryError(f"GraphQL errors: {json.dumps(payload['errors'])[:500]}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise EnvioQueryError(f"GraphQL response has no data object: {json.dumps(payload)[:200]}")
    rows = data.get(field)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise EnvioQueryError(f"GraphQL response has no {field} list: {json.dumps(data)[:200]}")
    return rows


def address_variants(addresses: set[str]) -> list[str]:
    """Return lowercase and checksummed forms; some Envio deployments store either."""
    return sorted({form for a in addresses for form in (a.lower(), to_checksum_address(a))})


def fetch_chain_heads(post: GraphQLPost) -> dict[int, ChainHead]:
    """Fetch ``chain_metadata`` and key it by chain id."""
    rows = entity_rows(post(CHAIN_METADATA_QUERY, {}), "chain_metadata")
    heads: dict[int, ChainHead] = {}
    for row in rows:
        if row.get("latest_processed_block") is None:
            continue
        chain_id = int(row["chain_id"])
        heads[chain_id] = ChainHead(
            chain_id=chain_id,
            latest_processed_block=int(row["latest_processed_block"]),
            block_height=int(row.get("block_height") or row["latest_processed_block"]),
        )
    return heads


class SharedChainHeads:
    """``fetch_chain_heads`` shared by every stream's reconcile cycle.

    Streams reconcile on the same cadence, so a read younger than ``max_age``
    seconds is reused instead of querying ``chain_metadata`` once per stream. A
    slightly old head only means a cycle stops a few blocks short; the next one
    catches up.
    """

    def __init__(self, post: GraphQLPost, max_age: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.post = post
        self.max_age = max_age
        self.clock = clock
        self._lock = threading.Lock()
        self._heads: dict[int, ChainHead] | None = None
        self._fetched_at = 0.0

    def __call__(self) -> dict[int, ChainHead]:
        """Return chain heads, fetching them when the cached read is too old.

        Raises:
            EnvioQueryError: The fetch failed; the next call retries.
        """
        with self._lock:
            if self._heads is None or self.clock() - self._fetched_at > self.max_age:
                self._heads = fetch_chain_heads(self.post)
                self._fetched_at = self.clock()
            return self._heads


def range_query(
    stream: EventStream, chain_id: int, from_block: int, to_block: int, limit: int, offset: int, since_ts: int | None
) -> str:
    """Build a page query for one chain's events in the block range ``(from_block, to_block]``.

    Numbers are inlined rather than passed as ``Int!`` variables: Hasura rejects an
    ``Int`` variable for a ``numeric``/``bigint`` column, and these are ints we
    generate. The order (block, logIndex, id) is total within a chain and the range
    never exceeds the indexer's processed head, so offset pagination is stable.
    """
    since = f", blockTimestamp: {{ _gte: {int(since_ts)} }}" if since_ts is not None else ""
    return f"""
    query {stream.entity}Range($addresses: [String!]!) {{
      {stream.entity}(
        where: {{
          chainId: {{ _eq: {int(chain_id)} }}
          {stream.address_field}: {{ _in: $addresses }}
          blockNumber: {{ _gt: {int(from_block)}, _lte: {int(to_block)} }}{since}
        }}
        order_by: [{{ blockNumber: asc }}, {{ logIndex: asc }}, {{ id: asc }}]
        limit: {int(limit)}
        offset: {int(offset)}
      ) {{{stream.fields}      }}
    }}
    """


def fetch_range(
    post: GraphQLPost,
    stream: EventStream,
    chain_id: int,
    addresses: set[str],
    from_block: int,
    to_block: int,
    page_size: int,
    max_pages: int,
    since_ts: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch every event in ``(from_block, to_block]`` for one chain, page by page.

    Returns:
        The rows in (block, logIndex) order and the number of pages fetched.

    Raises:
        EnvioQueryError: A page failed, or the range needs more than ``max_pages``
            pages. The caller must then not advance its checkpoint for this range.
    """
    variables = {"addresses": address_variants(addresses)}
    rows: list[dict[str, Any]] = []
    for page in range(max_pages):
        query = range_query(stream, chain_id, from_block, to_block, page_size, page * page_size, since_ts)
        batch = entity_rows(post(query, variables), stream.entity)
        rows.extend(batch)
        if len(batch) < page_size:
            return rows, page + 1
    raise EnvioQueryError(
        f"chain {chain_id} range ({from_block}, {to_block}] exceeds {max_pages} pages of {page_size}; "
        "lower --max-block-span"
    )


def live_subscription(stream: EventStream, limit: int, chain_ids: list[int]) -> str:
    """Live query over the newest monitored events across chains.

    Hasura re-sends the full snapshot whenever the result changes, so every message
    repeats rows already seen; the inbox deduplicates them. Anything that falls out
    of the window between snapshots is recovered by HTTP reconciliation. Chain ids
    are inlined for the same reason as in ``range_query``.
    """
    chains = ", ".join(str(int(c)) for c in sorted(chain_ids))
    return f"""
    subscription {stream.entity}Live($addresses: [String!]!) {{
      {stream.entity}(
        where: {{ {stream.address_field}: {{ _in: $addresses }}, chainId: {{ _in: [{chains}] }} }}
        order_by: [{{ blockTimestamp: desc }}, {{ blockNumber: desc }}, {{ logIndex: desc }}]
        limit: {int(limit)}
      ) {{{stream.fields}      }}
    }}
    """
