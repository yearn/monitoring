# Silo - ⚠️ DISABLED ⚠️

## Bad Debt

Github actions run hourly and send telegram message if there are positions with `riskFactor > 1` which can lead to bad debt.
[Python script code](https://github.com/tapired/monitoring-scripts-py/blob/main/silo/main.py)

## Governance

- [Internal timelock monitoring](../timelock/README.md) to send Telegram message for scheduled transactions in Timelock controller. Min delay is set to [2 days](https://etherscan.io/address/0xe1F03b7B0eBf84e9B9f62a1dB40f1Efb8FaA7d22#readContract#F5).
- Github actions run hourly and send telegram message when there are queued transactions in Safe Multisig (3/6).

Important contracts such as SiloRepository and SiloRouter are owned by the Safe Multisigs listed below, and we monitor the pending transactions of these multisigs every hour. In the future, SILO will transition to on-chain voting with the Timelock contract and veSILO. Details can be found [here](https://gov.silo.finance/t/silo-finance-2024-roadmap/451).

Currently, there is a [Timelock contract](0xe1F03b7B0eBf84e9B9f62a1dB40f1Efb8FaA7d22) deployed. However, it does not own any of the important contracts as the transition to complete on-chain governance is not yet complete.

    Mainnet Safe Multisig: 0xE8e8041cB5E3158A0829A19E014CA1cf91098554
    Optimism Safe Multisig: 0x468CD12aa9e9fe4301DB146B0f7037831B52382d
    Arbitrum Safe Multisig: 0x865A1DA42d512d8854c7b0599c962F67F5A5A9d9
