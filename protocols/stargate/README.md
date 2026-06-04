# Stargate

**Monitoring - DISABLED** because we don't have any active strategies.

## Withdraw amount

Github bot that triggers every hour to check the amount of withdroom room. Telegram message is sent if the withdroom below the amount deposited by the strategy.
Setup for:

- Polygon:
  - yearn-V3-Stargate-DAI
  - yearn-V3-Stargate-USDT
  - yearn-V3-Stargate-USDC
- Arbitrum:
  - yearn-V3-Stargate-USDT
  - yearn-V3-Stargate-USDC.E

[Python script code](https://github.com/tapired/monitoring-scripts-py/blob/main/stargate/main.py)

This bot is suspended because Stargate strategies are not active or have minimal amounts.

## Governance

Stargate contracts are controlled by a Safe Multisig (3/6). Github actions run hourly and send telegram message when there is a queued transaction in Safe Multisig.

These multisigs have the ability to add or remove bridge assets, configure bridge parameters and STG emissions, adjust pool parameters such as deposit fees when minting liquidity on Stargate pools, and execute emergency functions in bridge contracts.

    Mainnet Safe Multisig: 0x65bb797c2B9830d891D87288F029ed8dACc19705
    Polygon Safe Multisig: 0x47290DE56E71DC6f46C26e50776fe86cc8b21656
    Optimism Safe Multisig: 0x392AC17A9028515a3bFA6CCe51F8b70306C6bd43
    Arbitrum Safe Multisig: 0x9CD50907aeb5D16F29Bddf7e1aBb10018Ee8717d
