# Monitoring Alerts API

Read-only HTTP API for persisted monitoring alerts and the list of monitored
protocols.

Run locally:

```sh
CACHE_DIR=/tmp/monitoring-cache uv run python -m api
```

Production runs as `monitoring-api.service` and binds to `127.0.0.1:8923`.
Public auth and rate limiting should be handled by the reverse proxy.

## Health

```sh
curl http://127.0.0.1:8923/healthz
```

```json
{"status":"ok"}
```

## Protocols

Returns enabled protocol objects from `automation/jobs.yaml`, grouped with the
tasks that monitor each protocol.

```sh
curl http://127.0.0.1:8923/v1/protocols
```

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
        }
      ]
    }
  ],
  "count": 1
}
```

## Alerts

```sh
curl 'http://127.0.0.1:8923/v1/alerts?limit=10'
curl 'http://127.0.0.1:8923/v1/alerts?source=protocol&protocol=aave'
curl 'http://127.0.0.1:8923/v1/alerts?from=2026-06-11T00:00:00Z&to=2026-06-12T00:00:00Z'
```

Query parameters:

- `limit`: default `100`, max `500`.
- `cursor`: previous response `next_cursor`, for older rows.
- `from`: inclusive timestamp with timezone.
- `to`: exclusive timestamp with timezone.
- `since`: alias for `from`.
- `protocol`: exact protocol filter.
- `severity`: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`.
- `source`: `protocol`, `ops_error`, `crash`, or `automation_digest`.

Response:

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

Fetch the next page:

```sh
curl 'http://127.0.0.1:8923/v1/alerts?cursor=5021&limit=100'
```

Fetch one alert:

```sh
curl http://127.0.0.1:8923/v1/alerts/5021
```
