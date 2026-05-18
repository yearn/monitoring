# 3Jane USD3/sUSD3 Monitoring

## What it monitors

3Jane is a credit-based money market on Ethereum (modified Morpho Blue fork) with unsecured lending. USD3 is the senior tranche ERC-4626 vault backed by USDC deposits. sUSD3 is the junior (first-loss) tranche created by staking USD3.

- **PPS (Price Per Share):** `convertToAssets(1e6)` on USD3 and sUSD3 vs cached prior run. Alerts on any decrease — indicates loan markdowns or defaults (critical since loans are unsecured).
- **TVL (Total Value Locked):** `totalAssets()` on both vaults vs cached prior run. Alerts when absolute change is **≥15%**.
- **Junior Buffer Ratio:** sUSD3 TVL as a percentage of USD3 TVL. Alerts when sUSD3 buffer drops below **15%** of USD3 TVL — thin first-loss coverage puts senior tranche at risk.
- **Vault Shutdown:** `isShutdown()` on both vaults. Alert-once when either vault enters emergency shutdown.
- **Debt Cap:** `ProtocolConfig.getDebtCap()` vs cached prior. Alerts on any change — signals governance scaling the protocol up or down.

## Key Contracts

| Contract | Address | Purpose |
|----------|---------|---------|
| USD3 Vault | [`0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc`](https://etherscan.io/address/0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc) | Senior tranche ERC-4626 vault |
| sUSD3 Vault | [`0xf689555121e529Ff0463e191F9Bd9d1E496164a7`](https://etherscan.io/address/0xf689555121e529Ff0463e191F9Bd9d1E496164a7) | Junior (first-loss) tranche |
| ProtocolConfig | [`0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E`](https://etherscan.io/address/0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E) | Debt cap governance |

## Alert Thresholds

| Metric | Threshold | Severity |
|--------|-----------|----------|
| PPS decrease | Any decrease vs cached prior (USD3 or sUSD3) | HIGH |
| TVL change | ≥15% absolute change vs prior run | HIGH |
| Junior buffer ratio | sUSD3 < 15% of USD3 TVL | MEDIUM |
| Vault shutdown | `isShutdown()` transitions to true (alert-once) | HIGH |
| Debt cap change | Any change to `getDebtCap()` | MEDIUM |
| Monitoring run failure | Uncaught exception in `main()` | LOW |

## Governance

[Internal timelock monitoring](../timelock/README.md) for CallScheduled events on the [3Jane TimelockController](https://etherscan.io/address/0x1dccd4628d48a50c1a7adea3848bcc869f08f8c2) on Mainnet.

## Running

```bash
uv run 3jane/main.py
```

## Frequency

Runs hourly via [GitHub Actions](../.github/workflows/hourly.yml).
