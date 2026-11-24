# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes through the
[Safe Queue Service](https://docs.safe.global/core-api/safe-queue-service-overview)
(`https://api.safe.global/queue`). It supports Ethereum mainnet, Base, and Katana.
[The watchlist](addresses.py) defines which Safes are polled on those networks.

Alerts cover queued transactions and surface proxy upgrades, including upgrades inside multisend
and timelock calls. The Queue Service does not decode calldata, so the monitor decodes multisend
batches itself, and only for a DELEGATECALL into a known Safe multisend utility.

Yearn Safe alerts are escalated when the proposer is outside the configured list of known
proposers. A known address counts when it is the `proposer`, or the delegate in `proposedBy`
when `proposedVia` is `DELEGATE`. A nested-owner signer in `proposedBy` does not count.

[Per-protocol rules](filters.py) narrow some Safes:

- **Euler** alerts only on txs to the vault our strategies use.
- **Pendle** alerts only on privileged calls: proxy upgrades (direct or through a ProxyAdmin),
  `changeProxyAdmin`, `transferOwnership` and `diamondCut`. Calls are read from decoded calldata,
  including multisend batches, Pendle `aggregate` calls and timelock wrappers. Calldata the rule
  cannot fully inspect alerts rather than being skipped.

If one Safe cannot be checked (for example the API rejects the request), the monitor sends an
internal error alert and continues with the remaining Safes.

The **Diff** link opens a Wavey Gist, published with `WAVEY_GIST_API_KEY`, with the verified
source diff of both implementations. Any compiler, linked-library or constructor-argument changes
are listed below it. Proxies upgraded to the same implementation pair share one gist. An alert
waits up to 60 seconds for its gists. A comparison that is not ready by then, or whose source is
unavailable, keeps the explorer link.
