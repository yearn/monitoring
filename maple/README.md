# Maple Finance syrupUSDC Monitoring

## What it monitors

- **PPS (Price Per Share):** `convertToAssets(1e6)` on the syrupUSDC pool vs the value cached from the last run. Alerts on any decrease (HIGH); cache updates whenever PPS changes.
- **TVL (Total Value Locked):** `totalAssets()` vs cached prior run. Alerts when absolute change is **≥15%** (HIGH).
- **Unrealized Losses (on-chain):** Batched `unrealizedLosses()` on FixedTermLoanManager and OpenTermLoanManager. Alerts on any non-zero total (HIGH).
- **Unrealized Losses vs Pool Size (subgraph):** Maple GraphQL `poolV2S` per syrupUSDC and syrupUSDT. Alerts when unrealized losses are **≥0.5%** of that pool's `totalAssets` (HIGH).
- **Strategy AUM:** Logs `assetsUnderManagement()` on Aave and Sky strategies (visibility). Same figures define “liquid funds” for the withdrawal-queue ratio below.
- **Withdrawal Queue vs TVL:** Pending withdrawal exit value (`totalShares` → `convertToExitAssets`) vs pool TVL. Alerts when pending **>** **1%** of TVL (LOW). Aave + Sky AUM logged for context.
- **Pool Liquidity:** USDC `balanceOf` the pool vs pending withdrawal exit value (`totalShares` → `convertToExitAssets`). Alerts when pending withdrawals **>** pool cash, i.e. the delegate cannot satisfy the queue from idle cash (MEDIUM). Queue depth is fetched only when alerting and included as context.
- **Loan Collateral Risk:** GraphQL collateral merged across both pools; USD-weighted risk from `ASSET_RISK_SCORES` in [`maple/collateral.py`](./collateral.py). Alerts when weighted average is **>** **1.5** (MEDIUM), or when a collateral symbol is missing from the map (MEDIUM; unknowns use default score 5 for weighting).
- **Collateralization Ratio:** [`syrupGlobals`](https://docs.maple.finance/integrate/technical-resources/collateral-and-yield-disclosure) combined ratio (OC loans only; strategies excluded). Alerts when `collateralRatio` **<** **140%** (MEDIUM).
- **Pool Delegate Cover:** USDC `balanceOf` on PoolDelegateCover vs cached prior. Alerts if balance hits **$0** after a non-zero cached value, or on any decrease vs that cached prior (MEDIUM).
- **Stablecoin Peg (DeFiLlama):** `syrupUSDC` and `syrupUSDT` prices monitored via [`stables/main.py`](../stables/main.py) (runs every 10 min). Depeg alert below **$0.97** (CRITICAL); fetch failure alerts LOW.

## Key Contracts

| Contract | Address | Purpose |
|----------|---------|---------|
| syrupUSDC Pool | [`0x80ac24aA929eaF5013f6436cdA2a7ba190f5Cc0b`](https://etherscan.io/address/0x80ac24aA929eaF5013f6436cdA2a7ba190f5Cc0b) | ERC-4626 vault |
| FixedTermLoanManager | [`0x4A1c3F0D9aD0b3f9dA085bEBfc22dEA54263371b`](https://etherscan.io/address/0x4A1c3F0D9aD0b3f9dA085bEBfc22dEA54263371b) | Loan health |
| OpenTermLoanManager | [`0x6ACEb4cAbA81Fa6a8065059f3A944fb066A10fAc`](https://etherscan.io/address/0x6ACEb4cAbA81Fa6a8065059f3A944fb066A10fAc) | Loan health |
| AaveStrategy | [`0x560B3A85Af1cEF113BB60105d0Cf21e1d05F91d4`](https://etherscan.io/address/0x560B3A85Af1cEF113BB60105d0Cf21e1d05F91d4) | DeFi allocation |
| SkyStrategy | [`0x859C9980931fa0A63765fD8EF2e29918Af5b038C`](https://etherscan.io/address/0x859C9980931fa0A63765fD8EF2e29918Af5b038C) | DeFi allocation |
| WithdrawalManagerQueue | [`0x1bc47a0Dd0FdaB96E9eF982fdf1F34DC6207cfE3`](https://etherscan.io/address/0x1bc47a0Dd0FdaB96E9eF982fdf1F34DC6207cfE3) | Withdrawal processing |
| PoolDelegateCover | [`0x9e62FE15d0E99cE2b30CE0D256e9Ab7b6893AfF5`](https://etherscan.io/address/0x9e62FE15d0E99cE2b30CE0D256e9Ab7b6893AfF5) | Delegate skin-in-the-game |

## Alert Thresholds

Severities match `AlertSeverity` in code (`utils.alert`): **CRITICAL** / **HIGH** (notifying morpho curation automation), **MEDIUM** / **LOW** (sending telegram messages).

| Metric | Threshold | Severity |
|--------|-----------|----------|
| PPS decrease | Any decrease vs cached prior (`convertToAssets(1e6)`) | HIGH |
| TVL change | ≥15% absolute change vs prior run (`totalAssets`) | HIGH |
| Unrealized losses (on-chain) | Any non-zero on FixedTerm + OpenTerm loan managers | HIGH |
| Unrealized losses vs pool | ≥0.5% of `totalAssets` per pool (subgraph; syrupUSDC + syrupUSDT) | HIGH |
| Withdrawal queue vs TVL | Pending withdrawal exit value **>** 1% of pool TVL (`totalAssets`) | LOW |
| Pending withdrawals vs cash | Pending withdrawal exit value **>** pool cash | MEDIUM |
| Collateral risk score | Weighted average **>** 1.5 (USD-weighted over collateral) | MEDIUM |
| Unknown collateral asset | Collateral asset not in `ASSET_RISK_SCORES` | MEDIUM |
| Collateralization ratio | `syrupGlobals.collateralRatio` **<** 140% (combined Syrup pools; OC loans only) | MEDIUM |
| Delegate cover | USDC balance → $0 from cached non-zero, or any decrease vs cached prior | MEDIUM |
| Stablecoin peg (DeFiLlama) | `syrupUSDC` / `syrupUSDT` price **<** $0.97 — see [`stables/main.py`](../stables/main.py) | CRITICAL |
| DeFiLlama price fetch | Request fails — see [`stables/main.py`](../stables/main.py) | LOW |
| Monitoring run failure | Uncaught exception in `main()` | LOW |

## Collateral Risk Scores

Scores are defined in [`maple/collateral.py`](./collateral.py#L35). Unknown assets default to risk score 5 (Unknown) and trigger an alert for review.

## Governance Monitoring

- **DAO Multisig** ([`0xd6d4Bcde6c816F17889f1Dd3000aF0261B03a196`](https://etherscan.io/address/0xd6d4Bcde6c816F17889f1Dd3000aF0261B03a196)): Added to [safe monitoring](../safe/README.md). Alerts on queued multisig transactions.
- **Governor Timelock** ([`0x2eFFf88747EB5a3FF00d4d8d0f0800E306C0426b`](https://etherscan.io/address/0x2eFFf88747EB5a3FF00d4d8d0f0800E306C0426b)): Added to [cross-protocol timelock monitoring](../timelock/README.md). Alerts on `ProposalScheduled` events.

## Running

```bash
uv run maple/main.py
```

## Frequency

Runs hourly via [GitHub Actions](../.github/workflows/hourly.yml).

## Risk Report

Full risk assessment: [maple-syrupusdc report](https://github.com/tapired/risk-score/blob/master/reports/report/maple-syrupusdc.md)
