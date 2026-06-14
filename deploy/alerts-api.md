# Alerts API

The alerts API exposes persisted monitoring alerts from the local SQLite
database at `$CACHE_DIR/monitoring.db`. It is read-only.

Production runs it as `monitoring-api.service`, bound to localhost:

```sh
sudo systemctl enable --now monitoring-api
curl http://127.0.0.1:8923/healthz
```

For local testing:

```sh
CACHE_DIR=/tmp/monitoring-cache uv run python -m api
```

## Endpoints

### `GET /healthz`

Returns:

```json
{"status":"ok"}
```

### `GET /v1/alerts`

Returns alert rows ordered newest first.

Common examples:

```sh
curl 'http://127.0.0.1:8923/v1/alerts?limit=10'
curl 'http://127.0.0.1:8923/v1/alerts?source=protocol&limit=50'
curl 'http://127.0.0.1:8923/v1/alerts?protocol=aave&severity=HIGH'
curl 'http://127.0.0.1:8923/v1/alerts?from=2026-06-11T00:00:00Z&to=2026-06-12T00:00:00Z'
```

Query parameters:

- `limit`: rows to return, default `100`, max `500`.
- `cursor`: pagination cursor from the previous response.
- `from`: inclusive timestamp with timezone.
- `to`: exclusive timestamp with timezone.
- `since`: alias for `from`.
- `protocol`: exact protocol filter, for example `aave`.
- `severity`: one of `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`.
- `source`: alert source, commonly `protocol`, `ops_error`, `crash`, or `automation_digest`.

Example response:

```json
{
  "data": [
    {
      "id": 5021,
      "created_at": "2026-06-11T10:23:45.123456Z",
      "source": "protocol",
      "protocol": "aave",
      "channel": "aave",
      "severity": "LOW",
      "message": "message text",
      "plain_text": false,
      "silent": true,
      "delivery_status": "delivered",
      "delivered_at": "2026-06-11T10:23:45.456789Z",
      "delivery_error": null,
      "metadata": {}
    }
  ],
  "next_cursor": "5021",
  "limit": 100
}
```

For the next page, pass `cursor=<next_cursor>`:

```sh
curl 'http://127.0.0.1:8923/v1/alerts?cursor=5021&limit=100'
```

### `GET /v1/alerts/{id}`

Returns one alert:

```sh
curl http://127.0.0.1:8923/v1/alerts/5021
```

Missing alerts return `404`.

### `GET /v1/protocols`

Returns enabled protocol objects from `automation/jobs.yaml`, with the tasks
that monitor each protocol.

```sh
curl http://127.0.0.1:8923/v1/protocols
```

Example response:

```json
{
  "data": [
    {
      "name": "aave",
      "tasks": [
        {
          "name": "aave",
          "script": "protocols/aave/main.py",
          "args": {},
          "profile": "hourly",
          "cron": "5 * * * *"
        },
        {
          "name": "aave-proposals",
          "script": "protocols/aave/proposals.py",
          "args": {},
          "profile": "hourly",
          "cron": "5 * * * *"
        }
      ]
    }
  ],
  "count": 1
}
```

## Delivery Status

An alert row means a monitor generated an alert. Telegram delivery is tracked
separately:

- `generated`: row inserted before Telegram delivery completed.
- `delivered`: Telegram API call succeeded.
- `failed`: Telegram API call failed.
- `skipped_debug`: `LOG_LEVEL=DEBUG` skipped Telegram delivery.
- `skipped_missing_credentials`: Telegram credentials were missing.
- `not_attempted`: no Telegram attempt was made.

## Public Access

Do not expose the Python service directly. Keep it bound to `127.0.0.1` and put
a reverse proxy in front of it. The proxy should enforce authentication, rate
limits, and request/response timeouts.
