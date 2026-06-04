# Aave V3 Monitoring

## Bad Debt

[Github actions bot](https://github.com/tapired/monitoring-scripts-py/blob/main/.github/workflows/bad-debt.yml) for bad debt in the protocol. Uses [Risk DAO](https://bad-debt.riskdao.org/) data. Sends telegram message if the bad debt ratio is above `0.1%`.

## Utilization

Github actions run hourly and send telegram message if there is a market with utilization above `96%`. [Python script code](https://github.com/tapired/monitoring-scripts-py/blob/main/aave/main.py).

## Governance

[Internal timelock monitoring](../timelock/README.md) for queueing proposal to Aave Governance contract.
Proposal can be executed immediately because the cooldown period is [set to 0](https://etherscan.io/address/0x9aee0b04504cef83a65ac3f0e838d0593bcb2bc7#readProxyContract#F3). After the proposal is executed, the payload is queued to Payload Controller which has min execution delay [set to 1 day](https://etherscan.io/address/0xdabad81af85554e9ae636395611c58f7ec1aaec5#readProxyContract#F6).
Every payload that is executed on any network is the first queue to proposal on the mainnet. Monitoring just the proposal queue on the mainnet enables to get notification for future updates on all networks.

Additionally, Github actions bot runs every hour and fetches queued proposals using The Graph data: https://github.com/tapired/monitoring-scripts-py/blob/main/aave/proposals.py

Monitoring Safe multisigs of Protocol emergency Guardian and Governance emergency Guardian. Link with [address](https://app.aave.com/governance/v3/proposal/?proposalId=184) and [explanation](https://governance.aave.com/t/arfc-renewal-of-aave-guardian-2024/17523).

## External Monitoring

ChaosLabs has a public Telegram group with alerts for Aave V3: https://community.chaoslabs.xyz/aave/risk/alerts
