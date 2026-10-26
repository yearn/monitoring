"""Envio GraphQL queries for the timelock subscriber: chain heads, ranges, and the live subscription."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from eth_utils import to_checksum_address

from utils.http_client import request_with_retry

# The hourly monitor's selection plus logIndex and blockHash. Both transports use
# it, so a row fingerprints identically whichever one fetched it, and a reorg that
# replaces the block changes the fingerprint through blockHash.
EVENT_FIELDS = """
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
    """Return a GraphQL POST function bound to ``url`` with retries on transient errors."""

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = request_with_retry(
                "post", url, timeout=timeout, log_errors=False, json={"query": query, "variables": variables}
            )
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EnvioQueryError(f"GraphQL request failed: {exc}") from exc

    return post


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("errors"):
        raise EnvioQueryError(f"GraphQL errors: {json.dumps(payload['errors'])[:500]}")
    return payload.get("data") or {}


def address_variants(addresses: set[str]) -> list[str]:
    """Return lowercase and checksummed forms; some Envio deployments store either."""
    return sorted({form for a in addresses for form in (a.lower(), to_checksum_address(a))})


def fetch_chain_heads(post: GraphQLPost) -> dict[int, ChainHead]:
    """Fetch ``chain_metadata`` and key it by chain id."""
    rows = _data(post(CHAIN_METADATA_QUERY, {})).get("chain_metadata") or []
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


def range_query(chain_id: int, from_block: int, to_block: int, limit: int, offset: int, since_ts: int | None) -> str:
    """Build a page query for one chain's events in the block range ``(from_block, to_block]``.

    Numbers are inlined rather than passed as ``Int!`` variables: Hasura rejects an
    ``Int`` variable for a ``numeric``/``bigint`` column, and these are ints we
    generate. The order (block, logIndex, id) is total within a chain and the range
    never exceeds the indexer's processed head, so offset pagination is stable.
    """
    since = f", blockTimestamp: {{ _gte: {int(since_ts)} }}" if since_ts is not None else ""
    return f"""
    query TimelockEventRange($addresses: [String!]!) {{
      TimelockEvent(
        where: {{
          chainId: {{ _eq: {int(chain_id)} }}
          timelockAddress: {{ _in: $addresses }}
          blockNumber: {{ _gt: {int(from_block)}, _lte: {int(to_block)} }}{since}
        }}
        order_by: [{{ blockNumber: asc }}, {{ logIndex: asc }}, {{ id: asc }}]
        limit: {int(limit)}
        offset: {int(offset)}
      ) {{{EVENT_FIELDS}      }}
    }}
    """


def fetch_range(
    post: GraphQLPost,
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
        query = range_query(chain_id, from_block, to_block, page_size, page * page_size, since_ts)
        batch = _data(post(query, variables)).get("TimelockEvent") or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows, page + 1
    raise EnvioQueryError(
        f"chain {chain_id} range ({from_block}, {to_block}] exceeds {max_pages} pages of {page_size}; "
        "lower --max-block-span"
    )


def live_subscription(limit: int, chain_ids: list[int]) -> str:
    """Live query over the newest monitored events across chains.

    Hasura re-sends the full snapshot whenever the result changes, so every message
    repeats rows already seen; the inbox deduplicates them. Anything that falls out
    of the window between snapshots is recovered by HTTP reconciliation. Chain ids
    are inlined for the same reason as in ``range_query``.
    """
    chains = ", ".join(str(int(c)) for c in sorted(chain_ids))
    return f"""
    subscription TimelockEventLive($addresses: [String!]!) {{
      TimelockEvent(
        where: {{ timelockAddress: {{ _in: $addresses }}, chainId: {{ _in: [{chains}] }} }}
        order_by: [{{ blockTimestamp: desc }}, {{ blockNumber: desc }}, {{ logIndex: desc }}]
        limit: {int(limit)}
      ) {{{EVENT_FIELDS}      }}
    }}
    """
