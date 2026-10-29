# Durable emergency delivery

`utils.dispatch` queues HIGH/CRITICAL monitoring alerts before sending them to
independently configured liquidity-monitoring and vaults-mommy destinations.
`automation/jobs.yaml` drains retries every minute, even without a new alert.

## Destinations and operations


Set `VAULTS_MOMMY_WEBHOOK_SECRET` independently from `LIQUIDITY_WEBHOOK_SECRET`.
Its default URL is `http://127.0.0.1:8081/webhook/emergency`; override with
`VAULTS_MOMMY_WEBHOOK_URL`. Leaving either secret unset disables only that destination.
Receiver execution/position activation remains a separate operator decision under
[vaults-mommy #27/#35](https://github.com/johnnyonline/vaults-mommy/issues/27).
This sender neither signs transactions nor approves an exit.

Before HTTP, eligible deliveries for both destinations are committed to
`$CACHE_DIR/emergency_dispatch.sqlite3` (`EMERGENCY_DISPATCH_DB` overrides it).
SQLite uses WAL, FULL synchronization, transactions and bounded lock waits.
Back up the database and its WAL with the other monitoring state; do not delete it to
clear a failure. It stores payloads, endpoint URLs, lease/attempt state and independent
success timestamps, never webhook secrets. New code starts a new cooldown history;
legacy `dispatch_last_<protocol>` cache values are retained but are not consulted.
The first alert after upgrading can therefore be delivered again.

Liquidity receives the existing `event_type`/`client_payload` envelope unchanged.
Vaults-mommy additionally receives top-level `event_id` inside the signed body.
The ID and exact UTF-8 payload bytes remain stable across retries and process restarts.
An escalation is a new logical event/ID. HIGH at t=0 followed by CRITICAL at t=20 minutes
is delivered to both independently. Neither HIGH success nor HIGH pending/terminal work
suppresses CRITICAL. An unavailable destination cannot mark another as delivered or
start its cooldown. Duplicate same-severity pending work is retained, not multiplied.

The `emergency_delivery` automation profile runs `utils/dispatch_retry.py` every minute,
so retries drain even without new alerts. Timeout/connection failures, HTTP 408, 429
and 5xx retry with bounded backoff; numeric Retry-After is honored up to an hour.
3xx redirects are not followed. Other 4xx outcomes remain terminal and visible in logs
for correction. Repeated alerts do not silently replace terminal deliveries.
Leases expire after a crash, allowing at-least-once redelivery with the same event ID.
If an HTTP acknowledgement cannot be committed, delivery remains uncertain and retries.

After correcting a terminal error, explicitly retry its logged delivery ID:

```sh
uv run python -m utils.dispatch_retry --retry-terminal <delivery_id>
```

This preserves ID and payload. It also explicitly adopts the destination's current URL,
if the endpoint was corrected. An endpoint change alone never reroutes previously queued
signed alerts automatically. Secret rotation uses the current secret on the same body.
Missing/removed credentials hold existing pending deliveries; scheduled logs identify them.
DEBUG skips enqueue and sending while retaining queued state. The script uses the existing
crash-alert wrapper; disk/schema failures are operational errors, never delivery success.

The vaults-mommy #29 receiver integration must continue accepting legacy manual/no-ID envelopes. Without a
stable ID, an authentic delayed/replayed alert can reopen a resolved incident and request
execution; ordinary readiness, execution-gate and loss checks still apply. No timestamp or
resolution freshness gate is invented. Do not turn on automatic CRITICAL execution until
the sender escalation fix and receiver acceptance tests pass together.
