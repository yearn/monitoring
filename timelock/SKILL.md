---
name: add-timelock-monitoring
description: Add a new timelock contract to monitoring. Use when onboarding a new protocol's timelock or adding a new timelock address for an existing protocol.
allowed-tools: Read Write Edit Grep Glob Bash(git:*) Bash(gh:*) Bash(cast:*)
---

# Adding Timelock Monitoring

Adding a new timelock requires changes in **two repositories**. The Envio indexer must be deployed before the monitoring script can consume events.

## Step 1: Determine the Timelock Type

Before making changes, identify the timelock's event signature. Fetch the ABI from Etherscan:

```bash
cast abi <timelock_address> --etherscan
```

Compare the scheduling event against existing contract types in the [Envio config.yaml](https://github.com/chain-events/yearn-indexing-test/blob/main/config.yaml):

| Contract Type | Event Signature |
|---|---|
| `TimelockController` | `CallScheduled(bytes32 indexed id, uint256 indexed index, address target, uint256 value, bytes data, bytes32 predecessor, uint256 delay)` |
| `AaveTimelock` | `ProposalQueued(uint256 indexed proposalId, uint128 votesFor, uint128 votesAgainst)` |
| `CompoundTimelock` | `QueueTransaction(bytes32 indexed txHash, address indexed target, uint value, string signature, bytes data, uint eta)` |
| `PufferTimelock` | `TransactionQueued(bytes32 indexed txHash, address indexed target, bytes callData, uint256 indexed operationId, uint256 lockedUntil)` |
| `LidoTimelock` | `StartVote(uint256 indexed voteId, address indexed creator, string metadata)` |
| `MapleTimelock` | `ProposalScheduled(uint256 indexed proposalId, (bytes32,bool,uint32,uint32,uint32) proposal)` |

**If the event signature matches an existing type** — reuse that contract type (skip to Step 2a).
**If the event signature is different** — create a new contract type (go to Step 2b).

The event signature must match exactly because the Envio indexer uses it to generate ABI topic filters. Different parameters or types mean different topics.

## Step 2a: Reuse Existing Contract Type (Envio Indexer)

**Repository**: [chain-events/yearn-indexing-test](https://github.com/chain-events/yearn-indexing-test)

Add the address under the matching contract type in `config.yaml`:

```yaml
- name: TimelockController  # or whichever type matches
  address:
    - "0xexisting_address" # Existing Timelock
    - "0xnew_address"      # New Timelock Label
```

No handler changes needed. Skip to Step 3.

## Step 2b: New Contract Type (Envio Indexer)

**Repository**: [chain-events/yearn-indexing-test](https://github.com/chain-events/yearn-indexing-test)

### 2b.1 — Add contract definition in `config.yaml`

Add a new contract entry under `contracts:` with the event signature:

```yaml
- name: NewProtocolTimelock
  handler: src/EventHandlers.ts
  events:
    - event: "EventName(param1 type, param2 type, ...)"
```

Add the address under the correct network:

```yaml
- name: NewProtocolTimelock
  address:
    - "0xaddress" # Label
```

### 2b.2 — Add handler in `src/EventHandlers.ts`

Import the new contract type and add a handler that maps to the unified `TimelockEvent` entity:

```typescript
NewProtocolTimelock.EventName.handler(async ({ event, context }) => {
  const entity: TimelockEvent = {
    id: `${event.chainId}_${event.block.number}_${event.logIndex}`,
    timelockAddress: event.srcAddress,
    timelockType: "NewProtocol",
    eventName: "EventName",
    chainId: event.chainId,
    blockNumber: event.block.number,
    blockTimestamp: event.block.timestamp,
    blockHash: event.block.hash,
    transactionHash: event.transaction.hash,
    transactionFrom: event.transaction.from,
    logIndex: event.logIndex,
    operationId: /* map proposal/operation ID, use .toString() for numeric IDs */,
    target: /* map if available, else undefined */,
    value: /* map if available, else undefined */,
    data: /* map if available, else undefined */,
    delay: /* map delay/eta/lockedUntil if available, else undefined */,
    predecessor: undefined,
    index: undefined,
    signature: undefined,
    creator: undefined,
    metadata: undefined,
    votesFor: undefined,
    votesAgainst: undefined,
  };
  context.TimelockEvent.set(entity);
});
```

Key decisions when mapping fields:
- **`delay`**: Is it relative (seconds from now) or absolute (unix timestamp)? This affects Step 3.
- **`operationId`**: Use `.toString()` for numeric IDs (like Aave, Lido, Maple). Use raw value for bytes32 hashes (like Compound, Puffer).

### 2b.3 — Deploy the indexer

The indexer must be deployed and indexing events before the monitoring script can query them.

## Step 3: Add Monitoring Consumer

**Repository**: [yearn/monitoring-scripts-py](https://github.com/yearn/monitoring-scripts-py)

### 3.1 — Add `TimelockConfig` entry in `timelock/timelock_alerts.py`

```python
TimelockConfig("0xlowercase_address", chain_id, "PROTOCOL", "Human Label"),
```

- Address **must be lowercase**
- Protocol name is used for Telegram routing (`TELEGRAM_CHAT_ID_{PROTOCOL}`)

### 3.2 — Handle delay format (only for new contract types)

In `_format_delay_info()`, check if the new type uses absolute timestamps. If so, add it to the group:

```python
if timelock_type in ("Compound", "Puffer", "Maple", "NewProtocol"):
```

### 3.3 — Handle alert message (only for new contract types)

In `build_alert_message()`, add a branch for the new type:

```python
elif timelock_type == "NewProtocol":
    lines.append(f"🆔 Proposal: {first.get('operationId') or ''}")
```

If the type includes `target`/`data` fields, reuse the existing `_build_call_info()` helper by adding it to the group:

```python
elif timelock_type in ("TimelockController", "Compound", "Puffer", "NewProtocol"):
```

### 3.4 — Update documentation

- Add the new timelock to the table in `timelock/README.md`
- Update the `timelockType` list in the schema fields section
- If using a new protocol, add `TELEGRAM_CHAT_ID_{PROTOCOL}` to the monitoring env file (`/etc/monitoring/.env` on the VPS) and document it in `.env.example`

## Deployment Order

1. **Merge and deploy** the Envio indexer PR first
2. **Verify** events are being indexed via the GraphQL endpoint
3. **Then merge** the monitoring-scripts-py PR

## Verification

After both are deployed, run manually to verify:

```bash
uv run timelock/timelock_alerts.py --no-cache --since-seconds 604800 --log-level DEBUG
```

This looks back 7 days to catch any recent events from the new timelock.
