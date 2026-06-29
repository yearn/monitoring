# Ethena

## Overview

Ethena is a synthetic dollar protocol built on Ethereum that provides a crypto-native solution for money, USDe, alongside a globally accessible dollar savings asset, sUSDe.

## Monitoring

The script [`ethena/ethena.py`](ethena.py) runs daily via our VPS automation to sanity-check that **USDe remains fully backed** and that the public data feeds are fresh and internally consistent. Telegram messages are sent if some values are out of the expected range.

### Data Source - Ethena Transparency API

The primary backing check uses Ethena's own transparency API (`app.ethena.fi`). This API was previously blocked for GitHub Actions IPs, so a Chaos Labs / Oracle Security Proof-of-Reserve endpoint was used instead. That endpoint has since been decommissioned (returns HTTP 503), and Chainlink's USDe Proof of Reserves (Ethena's [PoR launch](https://ethena.fi/blog/usde-proof-of-reserves-launch) with Chainlink, Chaos Labs, LlamaRisk and Harris & Trotter) is not published as a public on-chain feed we can query. Since monitoring now runs on our VPS, Ethena's transparency API is reachable and is used directly.

1. **Supply**: `GET /api/solvency/token-supply?symbol=USDe`
2. **Collateral**: `GET /api/positions/current/collateral?latest=true`
3. **Backing Ratio**: `totalBackingAssetsInUsd / supply` — alert CRITICAL if `< 1`. USDe targets ~1:1 collateral backing with a separate reserve fund as the buffer, so the collateral-only ratio sits just above 1.0 in normal operation.

### Data Sources - LlamaRisk

> NOTE: LlamaRisk data is not reliable, so it is currently disabled (`llama_risk_check`).

#### Off-Chain

Data used is provided by Ethena on [transparency page](https://app.ethena.fi/dashboards/transparency) and LlamaRisk:

1. **Ethena Transparency**
   • Collateral: `GET /positions/current/collateral?latest=true`
   • Supply  : `GET /solvency/token-supply?symbol=USDe`
2. **LlamaRisk Dashboard**
   `GET https://api.llamarisk.com/protocols/ethena/overview/all/?format=json`

> NOTE: This LlamaRisk cross-check section is currently disabled (`llama_risk_check`). The note that Ethena data was unavailable applied to the old GitHub Actions setup; on our VPS the Ethena transparency API is reachable and is the primary source (see above).

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
