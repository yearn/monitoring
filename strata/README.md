# Strata Monitoring

Monitors Strata srUSDe/sUSDe risk and governance signals on Ethereum.
Reference report: https://curation.yearn.fi/report/strata-srusde/

## Scope

### srUSDe Vault Monitoring
- srUSDe: `0x3d7d6fdf07EE548B939A80edbc9B2256d0cdc003`
- Monitor `convertToAssets(1e18)`; alert if exchange rate decreases.

### StrataCDO Monitoring
- StrataCDO: `0x908B3921aaE4fC17191D382BB61020f2Ee6C0e20`
- Monitor senior coverage ratio; alert if ratio is below `105%`.
- Watch junior side draining via `jrUSDe.totalAssets()` rapid drop.

### Strategy Monitoring
- sUSDeStrategy: `0xdbf4FB6C310C1C85D0b41B5DbCA06096F2E7099F`
- Monitor `sUSDe` balance held by strategy.
- Alert if strategy balance drops significantly relative to total deposits.

### Governance Monitoring
- Admin Multisig: `0xA27cA9292268ee0f0258B749f1D5740c9Bb68B50`
- 48h Timelock: `0xb2A3CF69C97AFD4dE7882E5fEE120e4efC77B706`
- 24h Timelock: `0x4f2682b78F37910704fB1AFF29358A1da07E022d`
- Monitor queued Safe transactions via `safe/main.py`.
- Monitor timelock `CallScheduled/CallExecuted/Cancelled` via shared `timelock/timelock_alerts.py`.

### Ethena Dependency Monitoring
- sUSDe vault anomalies:
  - monitor `convertToAssets(1e18)` monotonicity
  - monitor cooldown period changes

## Frequency
- Timelock scheduled calls: hourly (`hourly.yml`, shared timelock monitor)
- Proxy upgrade events: hourly via safe/timelock queue monitoring
- srUSDe exchange rate: daily (`daily.yml`)
- Senior coverage ratio: daily (`daily.yml`)
- Strategy sUSDe balance: daily (`daily.yml`)
- Protocol TVL changes: daily (`daily.yml`)
