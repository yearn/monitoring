# Timelock Monitoring

Monitors all timelock contract types (TimelockController, Aave, Compound, Lido, Maple) and sends Telegram alerts to protocol-specific channels.

## How It Works

1. Queries the Envio GraphQL indexer (`ENVIO_GRAPHQL_URL`) for new `TimelockEvent` events across all monitored timelocks (all types).
2. Groups events by `operationId` so batch operations (`scheduleBatch`) are sent as a single alert.
3. Routes each alert to the correct Telegram channel based on the protocol mapping.
4. Stores the latest processed `blockTimestamp` in the SQLite state store (namespace `cache-id.txt`, key `TIMELOCK_LAST_TS`) to avoid duplicate alerts between runs.

The script runs hourly via the [monitoring runner](../../automation/jobs.yaml).

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
📋 Timelock: Aave Timelock
🔗 Chain: Mainnet
📌 Type: Aave
📝 Event: ProposalQueued
✅ Votes For: 12345
❌ Votes Against: 6789
🆔 Proposal ID: 42
🔗 Tx: https://etherscan.io/tx/0x...
```

**Lido:**
```
⏰ TIMELOCK: New Operation Scheduled
🅿️ Protocol: LIDO
📋 Timelock: Lido DAO
🔗 Chain: Mainnet
📌 Type: Lido
📝 Event: StartVote
👤 Creator: 0x1234...
📄 Metadata: ipfs://...
🆔 Vote ID: 123
🔗 Tx: https://etherscan.io/tx/0x...
```

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
