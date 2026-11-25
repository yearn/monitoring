# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes through the
[Safe Queue Service](https://docs.safe.global/core-api/safe-queue-service-overview)
(`https://api.safe.global/queue`). It supports Ethereum mainnet, Base, and Katana.
[The watchlist](addresses.py) defines which Safes are polled on those networks.

Each queued transaction alerts once, keyed by its `safeTxHash` per chain and Safe. A tx queued
later at a lower pending nonce, or a replacement proposed at a nonce that already alerted, gets its
own alert. The Queue Service's `/queue` endpoint returns only txs that can still execute (not
executed, nonce at or above the Safe's current nonce), so dead slots never alert. Alerted hashes
are pruned once they leave the queue; a tx that leaves and comes back alerts again.

An alert is marked handled after Telegram accepts it, so a skipped delivery stays eligible on
the next poll. A tx whose protocol has no Telegram destination configured is left queued before
any explanation, simulation or stored alert, so it costs nothing per poll and alerts once a
destination is set. The legacy nonce cache is not read: it records only a nonce, so it cannot tell
which proposal at that nonce was delivered. The first poll after rollout therefore alerts every
queued tx once, which can repeat alerts the old monitor already sent.

One Queue Service request per run lists every watched Safe (`safes=address:chainId`, repeated;
`limit=100`, lowest nonce first). A further page is fetched only while the response has a `next`
link, up to 5 pages; a listing cut off at that cap alerts what it has but prunes nothing.

Alerts cover queued transactions and surface proxy upgrades, including upgrades inside multisend
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
  wrappers. Calldata the rule
  cannot fully inspect alerts rather than being skipped.

If the service rejects the batched request (for example a Safe it cannot resolve), the monitor
queries each Safe on its own, so only the bad one goes unchecked; this costs one request per Safe
until the watchlist is fixed. A Safe that still fails gets an internal error alert and the rest
continue. When retries run out (an outage), the run fails and the crash alert goes to the internal
errors channel on every run until the service is back.

The **Diff** link opens a Wavey Gist, published with `WAVEY_GIST_API_KEY`, with the verified
source diff of both implementations. Any compiler, linked-library or constructor-argument changes
are listed below it. Proxies upgraded to the same implementation pair share one gist. An alert
waits up to 60 seconds for its gists. A comparison that is not ready by then, or whose source is
unavailable, keeps the explorer link.
