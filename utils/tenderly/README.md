# Tenderly Alerts Monitoring

Monitors Tenderly alerts to ensure they haven't been changed unexpectedly.

## Overview

This script verifies that Tenderly alerts match a stored snapshot (`alerts.json`). It runs daily via the [monitoring runner](../../automation/jobs.yaml) and will fail if alerts are added, removed, or modified without updating the snapshot.

## Files

- `tenderly.py` - Main monitoring script
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
