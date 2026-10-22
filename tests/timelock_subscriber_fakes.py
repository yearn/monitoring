"""In-process stand-ins for Envio's HTTP GraphQL and graphql-transport-ws endpoints."""

import asyncio
import json
import re
import threading
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.typing import Subprotocol

from protocols.timelock.subscriber.envio import EnvioQueryError

TIMELOCK = "0x88ba032be87d5ef1fbe87336b7090767f367bf73"
OTHER_TIMELOCK = "0xd8236031d8279d82e615af2bfab5fc0127a329ab"


def make_event(
    chain_id: int, block: int, log_index: int = 0, ts: int = 1_700_000_000, address: str = TIMELOCK, **extra: Any
) -> dict[str, Any]:
    """Return a TimelockEvent row shaped like Envio's response."""
    row: dict[str, Any] = {
        "id": f"{chain_id}_{block}_{log_index}",
        "timelockAddress": address,
        "timelockType": "TimelockController",
        "eventName": "CallScheduled",
        "chainId": chain_id,
        "blockNumber": block,
        "blockHash": "0x" + f"{block:064x}",
        "blockTimestamp": ts,
        "logIndex": log_index,
        "transactionHash": "0x" + f"{block:064x}",
        "operationId": "0x" + f"{block * 1000 + log_index:064x}",
        "index": 0,
        "target": "0x" + "ab" * 20,
        "value": "0",
        "data": "0x",
        "predecessor": None,
        "delay": 86400,
        "signature": None,
        "creator": None,
        "metadata": None,
        "votesFor": None,
        "votesAgainst": None,
    }
    row.update(extra)
    return row


def _int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


class FakeEnvio:
    """Serves ``chain_metadata`` and ``TimelockEvent`` range pages from in-memory rows."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.heads: dict[int, int] = {}
        self.lock = threading.Lock()
        self.queries: list[dict[str, Any]] = []
        # chain_id -> remaining failures for range queries on that chain
        self.fail_chain: dict[int, int] = {}
        self.fail_metadata = 0
        # Rebound to the WebSocket server's event loop by FakeWSServer.start().
        self.changed = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def add(self, *rows: dict[str, Any], advance_head: bool = True) -> None:
        with self.lock:
            for row in rows:
                self.rows[row["id"]] = row
                if advance_head:
                    self.heads[row["chainId"]] = max(self.heads.get(row["chainId"], 0), row["blockNumber"])
        self._notify()

    def remove(self, event_id: str) -> None:
        with self.lock:
            self.rows.pop(event_id, None)
        self._notify()

    def set_head(self, chain_id: int, block: int) -> None:
        with self.lock:
            self.heads[chain_id] = block

    def _notify(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.changed.set)

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if "chain_metadata" in query:
                if self.fail_metadata:
                    self.fail_metadata -= 1
                    raise EnvioQueryError("metadata down")
                return {
                    "data": {
                        "chain_metadata": [
                            {"chain_id": c, "latest_processed_block": h, "block_height": h + 2}
                            for c, h in self.heads.items()
                        ]
                    }
                }
            chain_id = _int(r"chainId: \{ _eq: (\d+) \}", query)
            assert chain_id is not None
            if self.fail_chain.get(chain_id):
                self.fail_chain[chain_id] -= 1
                raise EnvioQueryError(f"chain {chain_id} down")
            lo = _int(r"_gt: (-?\d+), _lte", query)
            hi = _int(r"_lte: (\d+)", query)
            since = _int(r"blockTimestamp: \{ _gte: (\d+) \}", query)
            limit = _int(r"limit: (\d+)", query)
            offset = _int(r"offset: (\d+)", query)
            assert lo is not None and hi is not None and limit is not None and offset is not None
            self.queries.append({"chain_id": chain_id, "from": lo, "to": hi, "offset": offset, "since": since})
            addresses = set(variables["addresses"])
            matched = sorted(
                (
                    r
                    for r in self.rows.values()
                    if r["chainId"] == chain_id
                    and r["timelockAddress"] in addresses
                    and lo < r["blockNumber"] <= hi
                    and (since is None or r["blockTimestamp"] >= since)
                ),
                key=lambda r: (r["blockNumber"], r["logIndex"], r["id"]),
            )
            return {"data": {"TimelockEvent": [dict(r) for r in matched[offset : offset + limit]]}}

    def snapshot(self, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = sorted(
                self.rows.values(), key=lambda r: (r["blockTimestamp"], r["blockNumber"], r["logIndex"]), reverse=True
            )
            return [dict(r) for r in rows[:limit]]


class FakeWSServer:
    """graphql-transport-ws server streaming FakeEnvio live-query snapshots."""

    def __init__(self, envio: FakeEnvio, limit: int = 50, ping_first: bool = True) -> None:
        self.envio = envio
        self.limit = limit
        self.ping_first = ping_first
        self.server: Server | None = None
        self.connections: set[ServerConnection] = set()
        self.subscriptions = 0
        self.accepting = True

    @property
    def url(self) -> str:
        assert self.server is not None
        host, port = list(self.server.sockets)[0].getsockname()[:2]
        return f"ws://{host}:{port}/v1/graphql"

    async def start(self) -> None:
        self.envio.loop = asyncio.get_running_loop()
        self.envio.changed = asyncio.Event()
        self.server = await serve(self._handle, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def drop_connections(self) -> None:
        for conn in list(self.connections):
            await conn.close(code=1011, reason="test disconnect")

    async def _handle(self, conn: ServerConnection) -> None:
        if not self.accepting:
            await conn.close(code=1013, reason="try later")
            return
        self.connections.add(conn)
        try:
            init = json.loads(await conn.recv())
            assert init["type"] == "connection_init"
            # Envio's Hasura sends a keepalive ping before the ack.
            await conn.send(json.dumps({"type": "ping", "payload": {"message": "keepalive"}}))
            await conn.send(json.dumps({"type": "connection_ack"}))
            sub = json.loads(await conn.recv())
            while sub["type"] == "pong":
                sub = json.loads(await conn.recv())
            assert sub["type"] == "subscribe"
            self.subscriptions += 1
            if self.ping_first:
                await conn.send(json.dumps({"type": "ping"}))
            sent = None
            while True:
                self.envio.changed.clear()
                snapshot = self.envio.snapshot(self.limit)
                if snapshot != sent:
                    await conn.send(
                        json.dumps({"id": sub["id"], "type": "next", "payload": {"data": {"TimelockEvent": snapshot}}})
                    )
                    sent = snapshot
                waiter = asyncio.create_task(self.envio.changed.wait())
                reader = asyncio.create_task(conn.recv())
                done, pending = await asyncio.wait({waiter, reader}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if reader in done:
                    reader.result()  # pong or close; raises ConnectionClosed on close
        except Exception:  # noqa: BLE001 - connection closed by either side
            pass
        finally:
            self.connections.discard(conn)
