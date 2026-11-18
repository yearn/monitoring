# Safe Multisig Monitoring

The monitor polls configured Safe queues every 20 minutes. It supports Ethereum mainnet, Base,
and Katana. [The watchlist](addresses.py) defines which Safes are polled on those networks.

Alerts cover queued transactions and surface proxy upgrades, including upgrades inside multisend
and timelock calls. Yearn Safe alerts are escalated when the proposer is outside the configured
list of known proposers.
