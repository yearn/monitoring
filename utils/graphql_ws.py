"""Minimal GraphQL-over-WebSocket client (``graphql-transport-ws`` protocol).

Envio's hosted Hasura negotiates ``graphql-transport-ws`` (the protocol of the
``graphql-ws`` npm client the Envio docs use). This module speaks just enough of
it to run one subscription per connection: ``connection_init`` → ack →
``subscribe`` → a stream of ``next`` payloads, answering server pings.

Reconnects are left to the caller, which needs to record each disconnect and
pick its own backoff. See https://github.com/enisdenjo/graphql-ws/blob/master/PROTOCOL.md.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Subprotocol

SUBPROTOCOL = "graphql-transport-ws"


class GraphQLWSError(Exception):
    """The server rejected the connection or the subscription."""


def http_to_ws_url(url: str) -> str:
    """Map a GraphQL HTTP endpoint to its WebSocket endpoint (https → wss, http → ws)."""
    if url.startswith("https://"):
        return "wss://" + url.removeprefix("https://")
    if url.startswith("http://"):
        return "ws://" + url.removeprefix("http://")
    return url


async def _await_ack(ws: ClientConnection) -> None:
    """Wait for ``connection_ack``, answering pings sent before it.

    Envio's Hasura sends a keepalive ``ping`` before the ack.
    """
    while True:
        message = json.loads(await ws.recv())
        kind = message.get("type")
        if kind == "connection_ack":
            return
        if kind == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif kind != "pong":
            raise GraphQLWSError(f"expected connection_ack, got {message!r}")


async def subscribe(
    url: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    ack_timeout: float = 10.0,
    idle_timeout: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Open a connection, run one subscription, and yield each ``next`` payload.

    Args:
        url: ``ws://`` or ``wss://`` GraphQL endpoint.
        query: GraphQL subscription document.
        variables: Subscription variables.
        ack_timeout: Seconds to wait for ``connection_ack``.
        idle_timeout: Raise ``TimeoutError`` when no message (data or ping) arrives
            for this long, so a silently dead connection is noticed. None waits forever.

    Yields:
        The ``payload`` of every ``next`` message (``{"data": ...}``; may carry ``errors``).

    Raises:
        GraphQLWSError: The server sent ``error``, or closed the handshake unexpectedly.
        TimeoutError: The ack or the next message did not arrive in time.
        websockets.exceptions.ConnectionClosed: The connection dropped.
    """
    async with connect(url, subprotocols=[Subprotocol(SUBPROTOCOL)], open_timeout=ack_timeout) as ws:
        if ws.subprotocol != SUBPROTOCOL:
            raise GraphQLWSError(f"server negotiated subprotocol {ws.subprotocol!r}, expected {SUBPROTOCOL!r}")
        await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
        await asyncio.wait_for(_await_ack(ws), ack_timeout)

        await ws.send(
            json.dumps({"id": "1", "type": "subscribe", "payload": {"query": query, "variables": variables or {}}})
        )
        while True:
            raw = await asyncio.wait_for(ws.recv(), idle_timeout)
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif kind == "next":
                yield message.get("payload") or {}
            elif kind == "error":
                raise GraphQLWSError(f"subscription error: {message.get('payload')!r}")
            elif kind == "complete":
                return
            # "pong" and unknown types are ignored.
