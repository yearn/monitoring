# Tenderly Monitoring

Monitors Tenderly usage spikes and unexpected changes to SAM alert configurations.

## Hourly Usage Check

`usage.py` runs in the hourly profile in [automation/jobs.yaml](../../automation/jobs.yaml).
It checks organization-wide Tenderly Unit (TU) consumption using `TENDERLY_API_KEY`
and `TENDERLY_ACCOUNT` (default: `yearn`). It makes one read-only usage API request per run.

```bash
uv run utils/tenderly/usage.py --dry-run
```

The monitor compares the last UTC hour that ended at least five minutes ago with
the **busiest hour in the preceding 24 hours**. It alerts only when consumption is
both **at least 3× that peak** and **at least 100,000 TUs above it**. This tolerates
normal hourly and daily cron bursts. Weekly jobs or unusually expensive legitimate
runs can still trigger an alert; a spike alone does not establish a leaked key.

Partial hours are excluded. Missing, stale, malformed, or duplicate hourly data
fails the check and is reported through the normal crash-alert path. Whole-valued
float timestamps are accepted and normalized to integer hourly timestamps. The monitor
uses the dashboard's undocumented usage endpoint with `timespan=1w` to obtain
hourly buckets; a Tenderly API change may require updating the parser.

Spike messages go to `TELEGRAM_CHAT_ID_YEARN_MAINTENANCE`, falling back to the Yearn
channel. Alert history is stored under `yearn-internal`. On first startup the
monitor checkpoints the latest settled hour before requesting data. It then
processes pending hours chronologically, advancing the checkpoint after a normal
hour or a successfully delivered spike. API failures, missing data, and skipped
or failed sends leave that hour pending for the next hourly run, even after the
clock moves to a later hour. Catch-up uses one weekly history response per run.

If a pending hour or its 24-hour baseline falls outside Tenderly's available
history, the monitor reports an error and retains the checkpoint. Investigate
the missing coverage before manually resetting the account's cursor in the
`tenderly_usage` monitor-state namespace; hours are never silently skipped.

`--dry-run` evaluates only the latest settled hour, without reading or writing
monitor state or sending messages. Failures exit nonzero and are logged locally
instead of sending a crash alert.

This is an organization-wide check: a small leak may be hidden by normal traffic,
and sustained high usage becomes part of the next hour's baseline. It detects
sudden changes, rather than enforcing a spending budget.

## Overview

This script verifies that Tenderly alerts match a stored snapshot (`alerts.json`). It runs daily via the [monitoring runner](../../automation/jobs.yaml) and will fail if alerts are added, removed, or modified without updating the snapshot.

## Files

- `usage.py` - Hourly organization-wide TU spike check
- `tenderly.py` - Daily SAM alert configuration check
- `alerts.json` - Stored snapshot of alerts (backup for recovery)

## Usage

### Daily Verification (Default)

```bash
uv run utils/tenderly/tenderly.py
```

Compares current alerts from Tenderly API with stored snapshot. Fails if they don't match.

### Update Snapshot

After making intentional changes to alerts in Tenderly:

```bash
uv run utils/tenderly/tenderly.py --update
```

Fetches current alerts and saves them to `alerts.json`.

## How It Works

1. **Hash Comparison**: Generates SHA256 hash of entire JSON response (sorted by alert ID)
2. **Count Verification**: Ensures alert count matches stored snapshot
3. **Failure on Mismatch**: Raises exception with both hashes if alerts changed

## Recovery

If alerts are accidentally deleted or modified, use `alerts.json` to recreate them. The file contains the complete alert configuration including:
- Alert expressions
- Delivery channels
- Enabled status
- All metadata

## CI/CD Integration

Runs automatically daily via the [monitoring runner](../../automation/jobs.yaml). The run will fail if alerts change unexpectedly, alerting you to investigate.
