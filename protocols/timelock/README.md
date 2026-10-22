# Timelock Monitoring

Monitors all timelock contract types (TimelockController, Aave, Compound, Lido, Maple) and sends Telegram alerts to protocol-specific channels.

## How It Works

1. Queries the Envio GraphQL indexer (`ENVIO_GRAPHQL_URL`) for new `TimelockEvent` events across all monitored timelocks (all types).
2. Groups events by `operationId` so batch operations (`scheduleBatch`) are sent as a single alert.
3. Routes each alert to the correct Telegram channel based on the protocol mapping.
4. Stores the latest processed `blockTimestamp` in the SQLite state store (namespace `cache-id.txt`, key `TIMELOCK_LAST_TS`) to avoid duplicate alerts between runs.

The script runs hourly via the [monitoring runner](../../automation/jobs.yaml).

## Continuous Subscriber Pilot

`event_subscriber.py` is a continuously running worker that observes the same `TimelockEvent` rows as `timelock_alerts.py` with lower latency ([#438](https://github.com/yearn/monitoring/issues/438), the first step of [#437](https://github.com/yearn/monitoring/issues/437)). It is an observation pilot: **it sends no alerts and changes nothing about current delivery**. The hourly task remains the only sender to protocol groups. Optionally it posts diagnostic notices to the internal errors chat (see [Pilot notices](#pilot-notices)). It runs as the `monitoring-timelock-subscriber` systemd unit; operations are in the [runbook](../../deploy/runbook.md#timelock-event-subscriber-pilot).

It runs two transports side by side and records which one saw each event first:

- **WebSocket**: a `graphql-transport-ws` live query (`utils/graphql_ws.py`) over the newest 50 monitored events, filtered by monitored chains and addresses. Hasura resends the whole snapshot on every change, so most rows are repeats; the inbox deduplicates them. On disconnect the worker reconnects with capped exponential backoff (1s to 60s).
- **HTTP reconciliation**, every 60s and also while the WebSocket is connected: for each chain, read `chain_metadata.latest_processed_block`, then fetch `(checkpoint - overlap, head]` in chunks of at most `MAX_BLOCK_SPAN` blocks, paginated in pages of 200 ordered by `(blockNumber, logIndex, id)`. This is the completeness guarantee.

State lives in `$CACHE_DIR/timelock_subscriber.db` (SQLite, WAL), separate from the hourly monitor's state. The schema is versioned (`PRAGMA user_version`); a database from an earlier revision is migrated in place on open, keeping observations, changes, and checkpoints. A row stored before the event selection grew (e.g. before `blockHash`) is upgraded silently on its next observation, not recorded as a change.

- **Per-chain checkpoints** (`timelock_sub_checkpoints`): the highest block fully fetched over HTTP, never moved backwards. They are independent per chain and do not use `TIMELOCK_LAST_TS`, so a failing or lagging chain never holds back the others. A chain's first run starts at the indexed head unless `TIMELOCK_SUBSCRIBER_START_LOOKBACK_SECONDS` is set.
- **Event provenance** (`timelock_sub_events`): one row per Envio event id (`<chainId>_<blockNumber>_<logIndex>`) with its block hash, a content fingerprint, the first transport and time it was seen, first-seen times and observation counts per transport.
- **Changes** (`timelock_sub_changes`): `changed` when a known id comes back with different content, `removed` when a fully fetched range no longer returns it, and `restored` when it reappears. The previous payload is kept.
- **Worker log** (`timelock_sub_log`): WebSocket connects and disconnects, each reconcile with its duration and per-chain counts, and snapshot ingestion times.

Edge cases:

- **Duplicate snapshots** and the overlapping re-scan only increment observation counters.
- **Equal timestamps**: ranges are bounded by block number per chain, not by a `blockTimestamp > last` cursor, so rows sharing a block or a timestamp are never skipped.
- **Late indexing and reorgs**: each cycle re-reads about 15-20 minutes of blocks below the checkpoint (`REORG_OVERLAP_BLOCKS`). The fingerprint includes `blockHash`, so a replaced block shows as a changed row at the same id (with the old payload kept), and a dropped log shows as a removed id.
- **Restarts** resume from the stored checkpoints. A backlog drains in bounded chunks, at most 20 chunks per chain per cycle.

Supervision: a heartbeat every 30s stores worker status, flags any chain whose indexed head has not advanced for 15 minutes, and pings the systemd watchdog only while reconciliation keeps completing. `event_subscriber.py health` exits 1 on a stale heartbeat, failed reconcile, WebSocket outage, or stalled chain.

`event_subscriber.py report --hours N` prints the coverage/latency report used to choose the production transport:

- WebSocket coverage of HTTP-confirmed events, plus WebSocket-only and HTTP-only counts.
- Delay from block timestamp to first sighting per transport (p50/p95/max), and how far the WebSocket leads HTTP. Envio exposes no per-row index time, so the faster transport's delay is an upper bound on indexing delay.
- WebSocket outages with the events that fell inside them, which transport recovered them, and how long it took.
- Reconcile timing and errors, absorbed duplicates, and changed/removed rows.
- A recommendation: `both` when the WebSocket saw at least 99% of confirmed events and was faster at p95, `http` otherwise, `undecided` with fewer than 5 events. Envio [fully supports hosted WebSockets only on Dedicated plans](https://docs.envio.dev/docs/HyperIndex/websockets), which the final decision must weigh.

### Pilot notices

With `TIMELOCK_SUBSCRIBER_NOTIFY=errors` (or `run --notify errors`), the worker posts plain-text diagnostics to the internal errors chat (`TELEGRAM_TOPIC_ID_ERRORS` or `TELEGRAM_CHAT_ID_ERRORS`), labelled `[timelock-subscriber]` and stored in alert history under `yearn-internal`. They are for comparing the pilot with the hourly alerts, not alerts themselves. Notices stay off unless an errors chat is configured: `send_error_message` would otherwise fall back to a protocol group.

| Notice | When |
|---|---|
| 🛰️ New timelock event | First sighting of each event: timelock, chain, event id, full timelock address and tx links, which transport saw it first, and the delay since its block. More than 10 new events in one batch post one summary per chain instead. Only events from blocks after the notice baseline are announced (see below). |
| ⚠️ WS miss | HTTP reconciliation found events first although the WebSocket was connected. |
| 🔀 Events changed | Indexed rows changed, were removed, or came back (a reorg or indexer rewrite). |
| 🔌 / ✅ WebSocket | Down for more than 5 minutes, then reconnected. |
| 🐢 / ✅ Envio head | A chain's indexed head stalled for `TIMELOCK_SUBSCRIBER_STALL_SECONDS`, then advanced again. |
| ❌ / ✅ Reconciliation | HTTP reconciliation failed 3 cycles in a row, then recovered. |
| 📊 Daily summary | Every 24h: the report's coverage, delays, outages, changes, and recommendation. The schedule survives restarts. |

The **notice baseline** is the time notices were first enabled on this inbox, stored in `timelock_sub_status`. Events from earlier blocks are history and never announced: the first WebSocket snapshot of a fresh pilot (the newest 50 historical events), a backfill, or enabling notices on an existing pilot. Events from later blocks are announced even when first seen after a restart, so events indexed while the worker was down still get a notice.

Notices go through a bounded background queue, never through the ingestion path, so a slow or failing Telegram call cannot delay event intake. Notices queued while a send is in flight are combined into one message; a failed send is logged and dropped.

## Stale Ready Operations

`stale_operations.py` runs daily. An OpenZeppelin TimelockController operation never expires: once its delay passes, it stays executable until someone executes or cancels it. A forgotten operation can therefore land weeks later, and `timelock_alerts.py` reported it only once, when it was scheduled.

1. Reads the `CallScheduled` events Envio indexed for the monitored timelocks over the last `TIMELOCK_STALE_LOOKBACK_DAYS` (default 180), and groups them by operation.
2. Reads each operation's state on-chain with `getTimestamp(id)`, batched per chain. Envio indexes only `CallScheduled`, so it cannot tell whether an operation ran. 0 means unset or cancelled, 1 executed, and anything else is the time the operation became ready.
3. Sends one silent alert per operation that has been ready for more than `TIMELOCK_STALE_DAYS` (default 7), grouped per protocol channel, with each call's target and decoded function. The cache key `TIMELOCK_STALE_<chain>_<timelock>_<operationId>` keeps it to one alert; it has no colons because the file cache backend stores rows as `key:value`. Alerts are split into messages under Telegram's 4096-character limit, and an operation is cached only after the message carrying it is sent. A batch lists up to its first 10 calls that fit the message budget, with an omitted-call count and a link to the schedule transaction for the complete batch. Yearn alerts are also mirrored to the internal Yearn chat, as in `timelock_alerts.py`.

Only `TimelockController` timelocks are checked. Compound-style queues expire after a grace period, and governor proposals have their own lifecycle.

Alerts show the timelock, full operation ID in inline code for copying into `getTimestamp(id)` or other state queries, schedule transaction link, readiness date, and call details.

## GraphQL Schema

The script queries the unified `TimelockEvent` type from the Envio indexer. The query fetches all timelock types (TimelockController, Aave, Compound, Lido, Maple) for monitored addresses.

### Query Structure

```graphql
query GetTimelockEvents($limit: Int!, $sinceTs: Int!, $addresses: [String!]!) {
  TimelockEvent(
    where: {
      timelockAddress: { _in: $addresses }
      blockTimestamp: { _gt: $sinceTs }
    }
    order_by: { blockTimestamp: asc, blockNumber: asc, logIndex: asc }
    limit: $limit
  ) {
    id
    timelockAddress
    timelockType
    eventName
    chainId
    blockNumber
    blockTimestamp
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
  }
}
```

### Schema Fields

The `TimelockEvent` type includes fields that vary by timelock type:

**Common fields (all types):**
- **`id`** - Unique identifier: `${chainId}_${blockNumber}_${logIndex}`
- **`timelockAddress`** - Address of the timelock contract
- **`timelockType`** - Type discriminator: `"TimelockController"`, `"Aave"`, `"Compound"`, `"Lido"`, or `"Maple"`
- **`eventName`** - Original event name (e.g., `"CallScheduled"`, `"ProposalQueued"`, `"QueueTransaction"`, etc.)
- **`chainId`** - Chain ID (1 for Mainnet, 8453 for Base, etc.)
- **`blockNumber`** - Block number where the event was emitted
- **`blockTimestamp`** - Unix timestamp of the block
- **`transactionHash`** - Transaction hash
- **`operationId`** - Unified identifier for the queued operation

**Type-specific fields:**
- **TimelockController**: `target`, `value`, `data`, `delay` (relative seconds), `predecessor`, `index`
- **Aave**: `votesFor`, `votesAgainst`, `operationId` (proposalId)
- **Compound**: `target`, `value`, `data`, `delay` (absolute timestamp/eta), `signature`, `operationId` (txHash)
- **Lido**: `creator`, `metadata`, `operationId` (voteId)
- **Maple**: `delay` (absolute timestamp/delayedUntil), `operationId` (proposalId)

## Monitored Timelocks

The list of monitored timelocks (address, chain, protocol, label) is `TIMELOCK_LIST` in [`timelock_alerts.py`](./timelock_alerts.py).

## How to Add a New Timelock

Follow [`SKILL.md`](./SKILL.md). It covers the Envio indexer change, the `TimelockConfig` entry, and the deployment order.

## Alert Format

The alert format varies by timelock type:

**TimelockController/Compound:**
```
⏰ TIMELOCK: New Operation Scheduled
🅿️ Protocol: LRT
📋 Timelock: EtherFi Timelock
🔗 Chain: Mainnet
📌 Type: TimelockController
📝 Event: CallScheduled
⏳ Delay: 2d
🆔 Operation ID: 0x5f3a...
🎯 Target: 0x1234...
📝 Function: 0xabcdef12
🔗 Tx: https://etherscan.io/tx/0x...
```

**Aave:**
```
⏰ TIMELOCK: New Operation Scheduled
🅿️ Protocol: AAVE
📋 Aave Governance V3: 0x9AEE0B04504CeF83A65AC3f0e838D0593BCb2BC7
🔗 Chain: Mainnet
✅ Votes For: 12345
❌ Votes Against: 6789
🆔 Proposal: 42
🔗 Governance: [Aave Governance](https://app.aave.com/governance/v3/proposal/?proposalId=42)
🔗 Tx: https://etherscan.io/tx/0x...
```

**Lido:**
```
🗳️ *New Governance Vote*
🅿️ Protocol: LIDO
📋 Lido Aragon Voting: [0x2e59a20f205bb85a89c53f1936454680651e618e](https://etherscan.io/address/0x2e59a20f205bb85a89c53f1936454680651e618e)
🔗 Chain: Mainnet
🆔 Vote: 206
🔗 Governance: [Lido Vote #206](https://dao.lido.fi/vote/206)
👤 Creator: [0xf73a1260d222f447210581ddf212d915c09a3249](https://etherscan.io/address/0xf73a1260d222f447210581ddf212d915c09a3249)
📄 Metadata: Omnibus vote: activate 0x02 CSM
🔗 Tx: [0x4950850f9e617242ec701d73e61861797cf8002711dfa14f93102b6a29f177bc](https://etherscan.io/tx/0x4950850f9e617242ec701d73e61861797cf8002711dfa14f93102b6a29f177bc)
```

Lido `StartVote` events announce a new governance vote. Metadata is escaped as literal text and shortened to a preview with a truncation marker to fit Telegram's message limit. The vote ID and governance link remain visible and are omitted together if the ID is missing.

TimelockController and Compound alerts show the full operation ID in inline code, in the same `Operation ID` format as the stale-operation alert, so you can match the two and copy the ID into TimelockController's `getTimestamp(id)` or `cancel(id)`. For Compound it is the `queuedTransactions` hash.

For batch operations (`scheduleBatch`), all calls are included in a single message with `--- Call N ---` separators. Calls are numbered from 1 (the on-chain batch index + 1) so they match the numbering in the AI report's call flow.

## Usage

```bash
uv run protocols/timelock/timelock_alerts.py
```

Optional flags:

- `--limit` — max events to fetch per run (default: `100`)
- `--since-seconds` — fallback lookback window when no cache exists (default: `43200` / 12h)
- `--no-cache` — disable caching, always use `--since-seconds` lookback
- `--protocol` — filter to a specific protocol, case-insensitive (e.g. `--protocol MAPLE`)
- `--log-level` — set log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `WARNING`)

## Caching

The script stores the latest processed `blockTimestamp` via `utils/cache.py` (namespace `cache-id.txt`, key `TIMELOCK_LAST_TS`). This value is universal across chains (unlike block numbers) so a single cache entry covers all monitored timelocks. On the first run (or with `--no-cache`), it falls back to querying events from the last 12 hours.
