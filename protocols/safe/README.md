# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes. It supports Ethereum mainnet, Base,
and Katana. [The watchlist](addresses.py) defines which Safes are polled on those networks.

Alerts cover queued transactions and surface proxy upgrades, including upgrades inside multisend
and timelock calls. Yearn Safe alerts are escalated when the proposer is outside the configured list
of known proposers.

The **Diff** link opens the explorer by default. To publish a pending upgrade's verified source
comparison on Wavey Gist, add its `chain_id:old_implementation:new_implementation` to the
comma-separated `WAVEY_PUBLIC_UPGRADE_DIFFS` allowlist and configure `WAVEY_GIST_API_KEY`.
Anyone with the gist URL can open it. Successful publications are reused for the same
implementation pair. If the gist is unavailable within the alert's four-second diff budget,
the alert uses the explorer link.
