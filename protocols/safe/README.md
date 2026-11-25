# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes through the
[Safe Queue Service](https://docs.safe.global/core-api/safe-queue-service-overview)
(`https://api.safe.global/queue`). It supports Ethereum mainnet, Base, and Katana.
[The watchlist](addresses.py) defines which Safes are polled on those networks.

## How a run works

Each run does three things, in order. The goal is that no Safe tx executes without an
alert; a duplicate alert in an edge case is acceptable, a missed one is not.

### 1. Fetch every queue with one request

One Queue Service request lists every watched Safe
(`GET /api/v1/multisig-transactions/queue?safes=address:chainId&…`, `limit=100`, lowest nonce
first). The endpoint returns only txs that can still execute: not executed, nonce at or above the
Safe's current nonce. Dead slots, where a competing tx at the same nonce already executed, never
come back. A further page is fetched only while the response has a `next` link, up to 5 pages.
If the shared listing exceeds that budget, the monitor switches to individual Safe listings.
Each Safe then has its own five-page budget and keeps its highest 500 proposals if it exceeds it.

When retries run out (an outage), the run fails and the crash alert goes to the internal errors
channel on every run until the service is back. When the service rejects the request (for example
a Safe it cannot resolve), the monitor queries each Safe on its own, so only the bad one goes
unchecked and gets an internal error alert. Each individual listing usually costs one request.

### 2. Alert each queued tx once

A queued tx is identified by its `safeTxHash` per chain and Safe, so a tx queued later at a lower
pending nonce, or a replacement proposed at a nonce that already alerted, gets its own alert.

A tx is recorded as handled when Telegram accepts its alert, or when the protocol's rule (below)
skips it. A failed or skipped delivery stays eligible on the next poll. A tx whose protocol has no
Telegram destination is left queued before any explanation, simulation or stored alert, so it
costs nothing per poll and alerts once a destination is set.

The legacy nonce cache is not read: it records only a nonce, so it cannot tell which proposal at
that nonce was delivered. The first poll after rollout therefore alerts every queued tx once,
which can repeat alerts the old monitor already sent.

### 3. Check that every executed tx was alerted

The queue only shows txs posted to the Safe service while they wait. A tx signed offchain and
executed directly, one executed in place of an alerted tx at the same nonce, or one proposed and
executed between two polls never reaches step 2. Every execution bumps the Safe's onchain nonce,
so the monitor reads each Safe's `nonce()` (one batched RPC request per chain) and checks every
nonce executed since the last run:

- **No tx at that nonce was handled:** it sends a loud "executed without a queued alert" to the
  protocol's channel, listing the nonces and linking the Safe's history.
- **A handled tx exists at that nonce:** that is not enough, because a signer could execute a
  different tx in its place. The monitor asks the Queue Service which of those exact hashes
  executed (`GET /api/v1/multisig-transactions/batch?safeTxHash=…`, up to 100 hashes per request,
  shared by all Safes). A match settles the nonce. Without a match the nonce waits one run, since
  the service may not have recorded the execution yet, and then alerts.

This check costs no Safe API request when no handled tx executed since the last run. It costs one
request when at least one did. A Safe's first nonce read only records the nonce. If the RPC read
fails, the Safe keeps its progress and the missed nonces are checked on the next successful read.
Executed alerts compress consecutive nonces into ranges and split large lists into separate
messages. Each message's nonces are settled after Telegram accepts it. A failed delivery leaves
those nonces for the next run while the other Safes continue. If the hash lookup fails, the next
run checks again.

Handled entries are kept until their tx has left the queue and step 3 has settled their nonce.

### Cost

About 2,160 Safe API requests per 30-day window: one per run, plus one hash lookup on runs after
an alerted tx executed. The Safe's nonce comes from RPC and does not count against the Safe API
quota.

## What an alert contains

Queued-tx alerts surface proxy upgrades, including upgrades inside multisend
and timelock calls. The Queue Service does not decode calldata, so the monitor decodes multisend
batches itself, and only for a DELEGATECALL into a known Safe multisend utility. For the v1.5.0
utilities, a zero inner destination resolves to the Safe for enrichment and simulation.

Yearn Safe alerts are escalated when the proposer is outside the configured list of known
proposers. A known address counts when it is the `proposer`, or the delegate in `proposedBy`
when `proposedVia` is `DELEGATE`. A nested-owner signer in `proposedBy` does not count, and an
`UNKNOWN`, missing or unrecognized `proposedVia` trusts neither address.

[Per-protocol rules](filters.py) narrow some Safes:

- **Euler** alerts only on txs to the vault our strategies use.
- **Pendle** alerts only on privileged calls: proxy upgrades (direct or through a ProxyAdmin),
  `changeProxyAdmin`, `transferOwnership` and `diamondCut`. Calls are read from decoded calldata,
  including multisend batches, Pendle `aggregate` / `aggregateWithScopedAccess` calls and timelock
  wrappers. Calldata the rule cannot fully inspect alerts rather than being skipped.

A tx a rule skipped still counts as handled for step 3. The executed-without-alert check ignores
these rules: a tx that never reached the queue has no calldata to check, so it always alerts.

The **Diff** link opens a Wavey Gist, published with `WAVEY_GIST_API_KEY`, with the verified
source diff of both implementations. Uncached source lookups try Sourcify first, then Etherscan
if needed. When the two implementations come from different providers, one is refetched from
the other's provider, because the providers lay out file paths and compiler versions differently.
If that fails, the gist says the providers differ. The gist names the source provider for each
implementation and lists compiler, linked-library or constructor-argument changes. Refetching
source preserves any constructor arguments already verified for the deployment. Proxies upgraded
to the same implementation pair share one gist. An alert waits up to 60 seconds for its gists. A comparison that is not
ready by then, or whose source is unavailable, keeps the explorer link.
