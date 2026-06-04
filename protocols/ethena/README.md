# Ethena

## Overview

Ethena is a synthetic dollar protocol built on Ethereum that provides a crypto-native solution for money, USDe, alongside a globally accessible dollar savings asset, sUSDe.

## Monitoring

The script [`ethena/ethena.py`](ethena.py) runs daily via GitHub Actions to sanity-check that **USDe remains fully backed** and that the public data feeds are fresh and internally consistent. Telegram messages are sent if some values are out of the expected range.

### Data Sources - Chaos Labs

1. **Attestation**
   `GET https://api.chaoslabs.xyz/v1/attestation/ethena`

2. **Attestation Freshness**
   If attestation is older than 1 day, skip the check.

3. **Attestation Consistency**
   - Check if USDe is fully backed
   - Check if only approved assets are used
   - Check if delta neutral strategy is maintained
   - Check if signature is valid

4. **Attestation Metrics**
   - Backing Ratio: `backingAssetsUsdValue / totalSupply`
   - Reserve Buffer: `backingAssetsAndReserveFundUsdValue - totalSupply`
   - Last Update: `timestamp`

### Data Sources - LlamaRisk

> NOTE: LlamaRisk data is not reliable, so we use Chaos Labs data instead.

#### Off-Chain

Data used is provided by Ethena on [transparency page](https://app.ethena.fi/dashboards/transparency) and LlamaRisk:

1. **Ethena Transparency**
   • Collateral: `GET /positions/current/collateral?latest=true`
   • Supply  : `GET /solvency/token-supply?symbol=USDe`
2. **LlamaRisk Dashboard**
   `GET https://api.llamarisk.com/protocols/ethena/overview/all/?format=json`

> NOTE: Ethena data is not available when running on Github Actions, so we use LlamaRisk data only.

#### On-Chain

1. **USDe Supply**
   `totalSupply` for USDe token
2. **sUSDe Supply**
   `totalSupply` for sUSDe token

#### What We Monitor

1. **Collateral Ratio**
   `totalBackingAssetsInUsd + reserveFund / totalUsdeSupply`
   • Warn if ratio < **1.01**

2. **Dual-Source Consistency**
   • Ethena vs LlamaRisk supply — alert if they differ by > 0.1%
   • Ethena vs LlamaRisk collateral — alert if they differ by > 0.1%

3. **Data Freshness**
   • If collateral or chain data is older than 12h from either API triggers a stale-data warning. Also, if reserve data is older than 12 h, send a warning.

4. **On-Chain Supply**
   • Ethena vs LlamaRisk supply for USDe and sUSDe — alert if they differ by > 0.5%
   • If chain data is old, use on-chain data for validating backings
