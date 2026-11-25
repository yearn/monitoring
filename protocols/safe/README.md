# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes through the
[Safe Queue Service](https://docs.safe.global/core-api/safe-queue-service-overview)
(`https://api.safe.global/queue`). It supports Ethereum mainnet, Base, and Katana.
[The watchlist](addresses.py) defines which Safes are polled on those networks.

Each queued transaction alerts once, keyed by its `safeTxHash` per chain and Safe. A tx queued
later at a lower pending nonce, or a replacement proposed at a nonce that already alerted, gets its
own alert. Transactions below the Safe's onchain nonce never alert: a competing tx at that nonce
already executed. Alerted hashes are pruned once the onchain nonce passes them.

An alert is marked handled after Telegram accepts it, so a skipped delivery stays eligible on
the next poll. The first successful queue poll seeds routine proposals covered by the legacy
nonce cutoff. Yearn proposals from unexpected proposers stay eligible for a critical alert, even
if their nonce is covered by the legacy cache. Pendle proposals are reevaluated under the new
filter on that first poll, which can resend a previously delivered alert.

Each Safe costs one Queue Service request per run (`limit=100`, highest nonce first). A further
page is fetched only when the first is full and its lowest nonce is still at or above the onchain
nonce, up to 5 pages.

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

If one Safe cannot be checked (for example the API rejects the request), the monitor sends an
internal error alert and continues with the remaining Safes.

The **Diff** link opens a Wavey Gist, published with `WAVEY_GIST_API_KEY`, with the verified
source diff of both implementations. Any compiler, linked-library or constructor-argument changes
are listed below it. Proxies upgraded to the same implementation pair share one gist. An alert
waits up to 60 seconds for its gists. A comparison that is not ready by then, or whose source is
unavailable, keeps the explorer link.
