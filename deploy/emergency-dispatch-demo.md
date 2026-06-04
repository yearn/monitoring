# Emergency Dispatch System - Demo Guide

## How it works

```
monitoring-scripts-py                    liquidity-monitoring
+------------------------+              +---------------------------+
| Protocol monitor       |   GitHub     | emergency_withdraw.yml    |
| detects issue          | ----------> | receives dispatch event   |
| (HIGH or CRITICAL)     |   API        |                           |
+------------------------+              +---------------------------+
                                                    |
                                         +----------+----------+
                                         |                     |
                                    HIGH alert           CRITICAL alert
                                         |                     |
                                   Opens PR with        Direct commit to
                                   forced_caps=0        main + immediate
                                   for review           reallocation
```

## Flow

1. A protocol monitor in `monitoring-scripts-py` detects an issue
2. It fires `send_alert(Alert(AlertSeverity.HIGH/CRITICAL, message, protocol))`
3. The alert hook calls `dispatch_emergency_withdrawal()`, which sends a `repository_dispatch` event to `liquidity-monitoring`
4. The `emergency_withdraw.yml` workflow picks it up and:
   - Looks up the protocol in `emergency_config.json` to find which vaults/markets to act on
   - Zeros the `forced_cap` and `forced_percentage` for those markets in `forced_caps.json`

## HIGH vs CRITICAL

| | HIGH | CRITICAL |
|---|---|---|
| **Action** | Opens a PR with the cap changes | Commits directly to main |
| **Reallocation** | Runs after PR is merged | Runs immediately |
| **Use case** | Degraded state, needs human review | System failure, act now |
| **Examples** | UR breach, low liquidity, peg deviation | Not fully backed, exchange rate drop, total loss |

## Protocols with dispatch enabled

| Protocol | Telegram channel | Example alerts |
|---|---|---|
| infinifi | infinifi | Backing < 0.999, reserves < $15M |
| cap | cap | Withdrawable liquidity < $50M |
| ethena | ethena | USDe not fully backed |
| ethplus | rtoken | Coverage below threshold, StRSR rate drop |
| origin | pegs | Wrapped OETH redeem value drop, backing ratio drop |
| usdai | usdai | _(hook registered)_ |

## Safety mechanisms

- **60-minute cooldown** per protocol (prevents duplicate dispatches from repeated alerts)
- **DEBUG mode** skips dispatch (same as Telegram)
- **DISPATCHABLE_PROTOCOLS** whitelist (only configured protocols can trigger)
- **`PAT_DISPATCH`** fine-grained token required (scoped to liquidity-monitoring repo)

## Manual trigger script

To manually trigger an emergency withdrawal dispatch:

### CRITICAL (direct commit + immediate reallocation)

```bash
gh api repos/tapired/liquidity-monitoring/dispatches \
  -X POST \
  -f event_type=emergency_withdrawal \
  -f 'client_payload[protocol]=usdai' \
  -f 'client_payload[severity]=CRITICAL'
```

### HIGH (opens PR for review)

```bash
gh api repos/tapired/liquidity-monitoring/dispatches \
  -X POST \
  -f event_type=emergency_withdrawal \
  -f 'client_payload[protocol]=usdai' \
  -f 'client_payload[severity]=HIGH'
```

Replace `usdai` with any protocol from the list above.

A **204 (no output)** response means the dispatch was sent successfully. Check the [Actions tab](https://github.com/tapired/liquidity-monitoring/actions) to see the workflow run.
