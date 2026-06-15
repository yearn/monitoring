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
a reverse proxy in front of it. The proxy enforces TLS, authentication, rate
limits, and request/response timeouts — the stdlib server does none of these and
uses one thread per connection, so an unshielded slowloris/connection flood
would wedge it.

### Caddy (Hetzner VPS)

[`Caddyfile`](./Caddyfile) is a ready-to-use config: it terminates TLS
(auto-provisioned by Caddy), exposes `GET /healthz` publicly for uptime checks,
and gates everything else behind an `Authorization: Bearer <token>` header.

```sh
# 1. DNS: point an A record (e.g. alerts.example.com) at the VPS public IP.

# 2. Install Caddy (official apt repo) and drop in the config:
sudo cp /srv/monitoring/deploy/Caddyfile /etc/caddy/Caddyfile

# 3. Provide domain + tokens + ACME email to the caddy service:
sudo systemctl edit caddy        # add, then save:
#   [Service]
#   Environment=ALERTS_API_DOMAIN=alerts.example.com
#   Environment=ALERTS_API_TOKENS=<token1>|<token2>     # pipe-separated, one per consumer
#   Environment=ACME_EMAIL=you@example.com
sudo systemctl restart caddy

# 4. Verify:
curl https://alerts.example.com/healthz                                   # public -> {"status":"ok"}
curl https://alerts.example.com/v1/alerts?limit=5                         # 401 (no token)
curl -H "Authorization: Bearer <token>" https://alerts.example.com/v1/alerts?limit=5
```

### Generating and storing API tokens

`/v1/*` is gated by a bearer token. Any value in the pipe-separated
`ALERTS_API_TOKENS` list is accepted, so each consumer gets its own token and
you revoke one by dropping it from the list.

```sh
openssl rand -hex 32     # generate one token per consumer (run once each)
```

Use hex (`openssl rand -hex`) — it contains only `[0-9a-f]`, so it needs no
escaping inside Caddy's matcher regex. ~32 bytes (256 bits) is plenty.

**Where to store them:**

- **Source of truth**: your team password manager / secrets vault, one entry per
  consumer labelled with who holds it (the proxy can't tell tokens apart, so this
  label is how you know which to revoke). Never commit a token to git or write it
  into `deploy/Caddyfile`.
- **On the box**: the pipe-joined list lives only in the caddy systemd override
  (`sudo systemctl edit caddy` → `Environment=ALERTS_API_TOKENS=...`), which is
  root-owned. It is visible via `systemctl show caddy` to root, which is fine for
  an admin-only host. For stricter isolation, put it in a `0600 root:root`
  `EnvironmentFile=` instead.
- **To each consumer**: hand over only their single token, over a secure channel.

**Add / rotate / revoke** — edit the list, then restart Caddy:

```sh
sudo systemctl edit caddy        # add or remove a token in ALERTS_API_TOKENS
sudo systemctl daemon-reload
sudo systemctl restart caddy
```

A `restart` (not `reload`) is required because the tokens live in Caddy's process
environment, which is only read at start — `reload` just re-reads the Caddyfile
from disk. The restart is sub-second; this is a read API and clients retry, so
the blip is harmless. Removing a token invalidates it immediately; the others
keep working.

### Firewall (Hetzner)

Hetzner includes baseline L3/L4 DDoS protection at its network edge for free, so
volumetric floods are largely absorbed before reaching the box. Layer the rest:

- **Hetzner Cloud Firewall** (console): allow inbound `22` (SSH), `80` + `443`
  (Caddy/ACME) only. Never open `8923` — the API must stay localhost-only so the
  only path in is through Caddy's auth.
- On-box `ufw` as defense-in-depth: `ufw allow 22,80,443/tcp && ufw enable`.
- **Rate limiting**: enable the commented `rate_limit` blocks in the Caddyfile
  (needs the `caddy-ratelimit` plugin — see the file header), and/or add
  `fail2ban` on `/var/log/caddy/alerts.log` to ban IPs that repeatedly 401 or
  flood.
- Optional: front it with Cloudflare (proxied DNS) for L7 WAF + IP hiding if you
  expect hostile traffic; then restrict the firewall to Cloudflare IP ranges and
  rate-limit on `{http.request.header.CF-Connecting-IP}` instead of
  `{remote_host}`.
