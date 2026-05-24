# APYUSD Monitoring

Monitors the `apxUSD` rate oracle proxy at `0xa2ef2e7bf32248083e514a737259f3785ea8d37d`.

## What is monitored

- The implementation behind the proxy is `ApxUSDRateOracle`.
- `setRate(uint256 newRate)` is the privileged write function.
- `rate()` is the read function used by this monitor.
- The rate uses `1e18` precision:
  - `1e18` = `1.0`
  - `1.5e18` = `1.5`

## Alert condition

`apyusd/main.py` reads `rate()` once per run, compares it against the cached previous value, and alerts when the value differs by at least `10%` by default, in either direction.

The script stores the previous observed rate in the shared cache file. On the first run it initializes the cache and does not alert.

Configure the threshold with:

```bash
APYUSD_RATE_DELTA_ALERT_THRESHOLD=0.1
```

## Usage

```bash
uv run apyusd/main.py
```
