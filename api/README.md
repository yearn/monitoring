# Monitoring Alerts API

Read-only HTTP API for persisted monitoring alerts, monitored protocols, and
monitoring card metadata.

Run locally:

```sh
CACHE_DIR=/tmp/monitoring-cache uv run python -m api
```

See [`deploy/alerts-api.md`](../deploy/alerts-api.md) for endpoints, response
shapes, pagination, and production setup.
