# Emergency Dispatch System - Demo Guide

## How it works

```
monitoring-scripts-py                    liquidity-monitoring
+------------------------+              +---------------------------+
| Protocol monitor       |   Webhook    | /webhook/emergency        |
| detects issue          | ----------> | receives signed payload   |
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
3. The alert hook calls `dispatch_emergency_withdrawal()`, which sends a signed JSON webhook to `liquidity-monitoring`
4. The webhook handler picks it up and:
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

The authoritative list is `DISPATCHABLE_PROTOCOLS` in [`utils/dispatch.py`](../utils/dispatch.py). Only HIGH and CRITICAL alerts whose `protocol` is in that set trigger a dispatch.

## Safety mechanisms

- **60-minute cooldown** per protocol (prevents duplicate dispatches from repeated alerts)
- **DEBUG mode** skips dispatch (same as Telegram)
- **DISPATCHABLE_PROTOCOLS** whitelist (only configured protocols can trigger)
- **`LIQUIDITY_WEBHOOK_SECRET`** required for `X-Hub-Signature-256` HMAC verification

## Webhook configuration

By default, the monitoring dispatcher sends to:

```text
http://127.0.0.1:8080/webhook/emergency
```

Set `LIQUIDITY_WEBHOOK_SECRET` to the shared webhook secret. The dispatcher serializes the JSON body once, signs those exact bytes with HMAC-SHA256, and sends:

```text
X-Hub-Signature-256: sha256=<hmac>
Content-Type: application/json
```

Override the endpoint with `LIQUIDITY_WEBHOOK_URL` only when the webhook is not running on the default local address.

## Manual trigger script

To manually trigger an emergency withdrawal webhook:

### CRITICAL (direct commit + immediate reallocation)

```bash
body='{"event_type":"emergency_withdrawal","client_payload":{"protocol":"usdai","severity":"CRITICAL","message":"manual trigger"}}'
sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$LIQUIDITY_WEBHOOK_SECRET" -hex | awk '{print $2}')"
curl -sS -X POST http://127.0.0.1:8080/webhook/emergency \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$sig" \
  --data-binary "$body"
```

### HIGH (opens PR for review)

```bash
body='{"event_type":"emergency_withdrawal","client_payload":{"protocol":"usdai","severity":"HIGH","message":"manual trigger"}}'
sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$LIQUIDITY_WEBHOOK_SECRET" -hex | awk '{print $2}')"
curl -sS -X POST http://127.0.0.1:8080/webhook/emergency \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$sig" \
  --data-binary "$body"
```

Replace `usdai` with any protocol from `DISPATCHABLE_PROTOCOLS`.
