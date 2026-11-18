# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes. It supports Ethereum mainnet, Base,
and Katana. [The watchlist](addresses.py) defines which Safes are polled on those networks.

Alerts cover queued transactions and surface proxy upgrades, including upgrades inside multisend
and timelock calls. Yearn Safe alerts are escalated when the proposer is outside the configured list
of known proposers.

The **Diff** link opens a Wavey Gist with the verified source diff and deployment metadata of
both implementations, published with `WAVEY_GIST_API_KEY`. Proxies upgraded to the same
implementation pair share one gist. An alert waits up to 60 seconds for its gists. A comparison
that is not ready by then, or whose source is unavailable, keeps the explorer link.
