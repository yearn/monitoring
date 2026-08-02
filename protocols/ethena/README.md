# Ethena

## Overview

Ethena is a synthetic dollar protocol built on Ethereum that provides a crypto-native solution for money, USDe, alongside a globally accessible dollar savings asset, sUSDe.

## Monitoring

The script [`ethena/ethena.py`](ethena.py) runs daily via our VPS automation to sanity-check that **USDe remains fully backed**. A Telegram message is sent if the backing ratio drops below the expected range.

The backing check (`ethena_backing_check`) computes `(collateral + reserve fund) / supply`, alerts **CRITICAL** if `< 1` and **HIGH** if `< 1.005` (`COLLATERAL_RATIO_TRIGGER`). USDe targets ~1:1 collateral backing with a separate reserve fund as the buffer, so the collateral-only figure hovers right around 1.0; including the reserve fund gives the true solvency ratio and avoids false positives on fractional collateral dips. Alerts are prefixed with `[Ethena API]` to make the source explicit.

### Data Source — Ethena Transparency API

Uses Ethena's own transparency API (`app.ethena.fi`). This API was previously blocked for GitHub Actions IPs, so a Chaos Labs / Oracle Security Proof-of-Reserve endpoint was used instead. That endpoint has since been decommissioned (returns HTTP 503), and Chainlink's USDe Proof of Reserves (Ethena's [PoR launch](https://ethena.fi/blog/usde-proof-of-reserves-launch) with Chainlink, Chaos Labs, LlamaRisk and Harris & Trotter) is not published as a public on-chain feed we can query. Since monitoring now runs on our VPS, Ethena's transparency API is reachable.

1. **Supply**: `GET /api/solvency/token-supply?symbol=USDe`
2. **Collateral**: `GET /api/positions/current/collateral?latest=true` (`totalBackingAssetsInUsd`)
3. **Reserve fund**: `GET /api/solvency/reserve-fund` — latest point of the `queryIndex[0].yields` time series.

> **On the `latest=true` collateral figure:** it returns Ethena's *net backing* number, which tracks supply ~1:1 (ratio ≈ 1.00, ≈ 1.015 with reserve). The same endpoint *without* `latest=true` returns a detailed per-exchange breakdown whose total is *gross collateral*, ~2.7% higher — but that breakdown is a stale snapshot (items lag several hours). We use the fresh net figure plus the reserve fund as the buffer.

> **Removed:** a second independent check against LlamaRisk's transparency API (`api.llamarisk.com/protocols/ethena/...`) previously ran alongside this one. LlamaRisk decommissioned that endpoint (now HTTP 404; the host only serves `aave-v4` routes), so the check was removed.
