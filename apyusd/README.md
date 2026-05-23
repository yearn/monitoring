# APYUSD Monitoring

Monitors the `apxUSD` rate oracle proxy at `0xa2ef2e7bf32248083e514a737259f3785ea8d37d`.

## What is monitored

- The implementation behind the proxy is `ApxUSDRateOracle`.
- `setRate(uint256 newRate)` is the privileged write function.
- `RateUpdated(uint256 oldRate, uint256 newRate)` is the event emitted on successful updates.
- The rate uses `1e18` precision:
  - `1e18` = `1.0`
  - `1.5e18` = `1.5`

## Alert condition

`apyusd/main.py` watches `RateUpdated` events on the proxy and alerts when `newRate` differs from `oldRate` by at least `10%` by default, in either direction.

The script stores the last processed block in the shared cache file. On the first run it initializes the cursor at the current block and does not backfill historical events.

Configure the threshold with:

```bash
APYUSD_RATE_DELTA_ALERT_THRESHOLD=0.1
```

## Usage

```bash
uv run apyusd/main.py
```
