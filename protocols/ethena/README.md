# Ethena

## Overview

Ethena is a synthetic dollar protocol built on Ethereum that provides a crypto-native solution for money, USDe, alongside a globally accessible dollar savings asset, sUSDe.

## Monitoring

The script [`ethena/ethena.py`](ethena.py) runs daily via our VPS automation to sanity-check that **USDe remains fully backed** and that the public data feeds are fresh and internally consistent. Telegram messages are sent if some values are out of the expected range.

Two **independent** backing checks run each cycle — one against Ethena's own transparency API, one against LlamaRisk. They run in isolation (a failure or false positive in one provider never suppresses the other), and **every alert is prefixed with the provider that triggered it** (`[Ethena API]` or `[LlamaRisk]`) so it is obvious which source fired.

Both checks compute the same ratio: `(collateral + reserve fund) / supply`, alert **CRITICAL** if `< 1` and **HIGH** if `< 1.005` (`COLLATERAL_RATIO_TRIGGER`). USDe targets ~1:1 collateral backing with a separate reserve fund as the buffer, so the collateral-only figure hovers right around 1.0; including the reserve fund gives the true solvency ratio and avoids false positives on fractional collateral dips.

> **The two checks measure different collateral lenses — they are not a like-for-like cross-check.** The Ethena check queries `/positions/current/collateral?latest=true`, whose `totalBackingAssetsInUsd` is a *net backing* figure that tracks supply ~1:1 (ratio ≈ 1.00, ≈ 1.015 with reserve). LlamaRisk (and the same Ethena endpoint *without* `latest=true`) report *gross collateral*, ~2.7% higher (ratio ≈ 1.027, ≈ 1.042 with reserve). They agree asset-by-asset to ~0.04%, so the ~2.7% ratio difference between the two checks is expected and definitional, **not** a data-staleness or backing problem. Each is a valid independent lower-bound on backing; they are intentionally kept separate rather than reconciled into one ratio.

### Check 1 — Ethena Transparency API (`ethena_backing_check`)

Uses Ethena's own transparency API (`app.ethena.fi`). This API was previously blocked for GitHub Actions IPs, so a Chaos Labs / Oracle Security Proof-of-Reserve endpoint was used instead. That endpoint has since been decommissioned (returns HTTP 503), and Chainlink's USDe Proof of Reserves (Ethena's [PoR launch](https://ethena.fi/blog/usde-proof-of-reserves-launch) with Chainlink, Chaos Labs, LlamaRisk and Harris & Trotter) is not published as a public on-chain feed we can query. Since monitoring now runs on our VPS, Ethena's transparency API is reachable.

1. **Supply**: `GET /api/solvency/token-supply?symbol=USDe`
2. **Collateral**: `GET /api/positions/current/collateral?latest=true` (`totalBackingAssetsInUsd`)
3. **Reserve fund**: `GET /api/solvency/reserve-fund` — latest point of the `queryIndex[0].yields` time series.

### Check 2 — LlamaRisk (`llama_risk_check`)

Uses the LlamaRisk transparency dashboard as a fully independent second opinion:

`GET https://api.llamarisk.com/protocols/ethena/overview/all/?format=json`

- **Backing ratio**: `(collateral_value + reserve_fund) / total_usde_supply`, same CRITICAL/HIGH thresholds as Check 1.
- **On-chain cross-validation**: LlamaRisk's USDe and sUSDe supply are compared against on-chain `totalSupply()`; a MEDIUM alert fires if they differ by more than 0.5%. Skipped when LlamaRisk chain data is older than 2h (it would be out of sync with chain state).
- **Data freshness**: LOW alerts if LlamaRisk collateral or reserve data is older than 12h.

> NOTE: LlamaRisk data has historically lagged/diverged from Ethena's; it is treated as a secondary cross-check, which is why the two checks are independent and separately labelled rather than merged into one ratio.

#### On-Chain feeds used by Check 2

1. **USDe Supply** — `totalSupply` for the [USDe token](https://etherscan.io/address/0x4c9EDD5852cd905f086C759E8383e09bff1E68B3)
2. **sUSDe Supply** — `totalSupply` for the [sUSDe token](https://etherscan.io/address/0x9D39A5DE30e57443BfF2A8307A4256c8797A3497)
