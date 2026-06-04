# Maker DAO

## Governance

### Proposals Script

`proposals.py` monitors new executive proposals from the [Sky governance portal](https://vote.sky.money/executive) using the public API at `https://vote.sky.money/api/executive`. Sends Telegram alerts for new proposals and caches the last seen proposal timestamp to avoid duplicate messages.

Run: `python maker/proposals.py`

### Tenderly Alerts

Tenderly alert for function [`plot()` called in DSPause](https://dashboard.tenderly.co/yearn/sam/alerts/rules/6c4b81c9-3130-4b3c-9356-15c9abc7b918) that schedules the plan (tx) that can be executed after a delay. A plan can be executed by anyone. A similar concept to Timelock controller. The minimum delay is set to [16 hours](https://etherscan.io/address/0xbe286431454714f511008713973d3b053a2d38f3#readContract#F2). Scheduled tx (spell) will only be executed Monday through Friday between 14:00 and 21:00 UTC.

[PauseProxy](https://etherscan.io/address/0xbe8e3e3618f7474f8cb1d074a26affef007e98fb) controls [PSM USDC](https://etherscan.io/address/0x89B78CfA322F6C5dE0aBcEecab66Aee45393cC5A#readContract#F9), it can update the DAI/USDC rate by calling the function `file()`. Its [owner is DSPause contract](https://etherscan.io/address/0xbe8e3e3618f7474f8cb1d074a26affef007e98fb#readContract#F1), any update is covered with Tenderly alert specified above.

Upgradeable [sUSDS](https://etherscan.io/address/0xa3931d71877c0e7a3148cb7eb4463524fec27fbd) contract owner is also [set](https://etherscan.io/tx/0x4d4a0396ac55bd2113fe630efe9db0330043508e19ed93d8bb3973a5dda3727e#eventlog) to DSPause contract. This is the some for upgradeable token [USDS](https://etherscan.io/address/0xdC035D45d973E3EC169d2276DDab16f1e407384F), tx owner [set](https://etherscan.io/tx/0x3c9a2a60285c972bf103d29ffe97503b25c5dbcb130f2bd862749a69ec21098c#eventlog).

To get the proposal data from the received alert:

1. see the tx from alert on [Tenderly](https://dashboard.tenderly.co/yearn/sam/tx/mainnet/0x4d84206c92894f4c4b5865d9c85f42105e5655d4a9415f799867a67ea3686c39)
2. get [DssSpell address](https://dashboard.tenderly.co/yearn/sam/tx/mainnet/0x4d84206c92894f4c4b5865d9c85f42105e5655d4a9415f799867a67ea3686c39?trace=0.1)
3. check proposals on [Maker DAO voting site](https://vote.makerdao.com/executive), after rebranding use [SKY DAO voting site](https://vote.sky.money/executive)
4. [match proposal with spell address](https://vote.makerdao.com/executive/template-executive-vote-lite-psm-usdc-a-phase-2-setup-august-22-2024) with DssSpell address from tx

[Governance DSPause module docs](https://docs.makerdao.com/smart-contract-modules/governance-module/pause-detailed-documentation).

## PSM Balance

Tenderly alert for [PSM balance](https://dashboard.tenderly.co/yearn/sam/alerts/rules/4033c908-e924-43c1-a12c-5be712c039d8) of USDC. If the balance is less than 2B USDC, telegram alert is sent Maker DAO channel. On 5th March 2026, the balance was 4.46B USDC, [PSM contract](https://etherscan.io/address/0x37305B1cD40574E4C5Ce33f8e8306Be057fD7341#readContract).
