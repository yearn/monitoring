# SQLite-backed Alerts API Implementation Plan

Branch: `alerts-api-implementation-plan`

This document is the development handoff for adding a persistent alerts API to the
monitoring scripts. It is intentionally written as an implementation plan, not only
an architecture note, so separate contributors can pick up individual phases.

## Context

An external party requested API access to monitoring alerts. Today alerts are mostly
fire-and-forget Telegram messages. There is no alert history, no query surface, and
no HTTP service. Current monitor state is stored in small text files under
`CACHE_DIR`, primarily via `utils/cache.py`.

The monitoring service already runs on a single VPS through systemd and supercronic.
The data is explicitly disposable: no backups are required, and losing the database
is acceptable. Expected volume is small: dozens of alerts per day and hundreds of
state/cache keys.

## Goals

- Persist every generated alert in a local SQLite database.
- Expose a read-only HTTP API for querying alert history.
- Keep the monitoring cron service isolated from API failures.
- Keep alert capture best-effort: database problems must never block Telegram sends.
- Keep the first rollout small: alerts API first, cache migration later.
- Support timestamp range queries from day one.
- Support cursor pagination from day one.
- Use no new runtime dependencies.

## Non-goals

- No high-availability database.
- No backups or durable storage guarantees.
- No write API for external users.
- No dashboard UI.
- No cache migration in the first API PR.
- No in-process public auth if the service binds to localhost behind the operator's
  reverse proxy. Auth and rate limiting belong at the reverse proxy.

## Final Decisions

- Storage: SQLite database at `$CACHE_DIR/monitoring.db`.
- Journal mode: WAL.
- Services:
  - `monitoring.service`: existing cron runner, writes alert events.
  - `monitoring-api.service`: new HTTP API, reads alert events.
- API binding: `127.0.0.1:8923` by default.
- HTTP implementation: stdlib `http.server.ThreadingHTTPServer`.
- API version path: `/v1`.
- Pagination: `cursor` is the last seen `id`; next page uses `id < cursor`.
- Timestamp filters:
  - `from`: inclusive start timestamp.
  - `to`: exclusive end timestamp.
  - omitted `to` means "until now".
- Alert rows represent "alert generated". Telegram delivery status is tracked
  separately in the same row.
- Cache migration is a later phase and uses a separate `monitor_state` table.

## Architecture

```
protocol scripts
    |
    v
utils.alert.send_alert(...) or utils.telegram.send_telegram_message(...)
    |
    | best-effort insert/update
    v
$CACHE_DIR/monitoring.db  <---- read-only queries ----  monitoring-api.service
    ^
    |
future phase: utils.cache key/value state
```

The API and scheduler must be separate systemd units. If the API is overloaded or
crashes, the cron runner should continue to start jobs. The only shared resource is
the SQLite database file under `/srv/cache` on the VPS.

## Data Semantics

An `alert_events` row means a monitor generated an alert-like message. It does not
guarantee Telegram delivery. Delivery is tracked with `delivery_status`,
`delivered_at`, and `delivery_error`.

Expected statuses:

- `generated`: row inserted before the send path completed.
- `delivered`: Telegram API call succeeded.
- `failed`: Telegram API call failed and raised `TelegramError`.
- `skipped_debug`: `LOG_LEVEL=DEBUG` skipped Telegram delivery.
- `skipped_missing_credentials`: Telegram credentials were missing.
- `not_attempted`: no Telegram attempt was made for this source.

`protocol` should mean the logical origin of the alert, not necessarily the Telegram
routing key. `channel` should store the Telegram routing key/chat selection used for
delivery. For most monitors these are the same. For alerts using `Alert.channel`,
`protocol` remains the real protocol and `channel` stores the routing destination.

`source` separates alert classes:

- `protocol`: normal protocol monitor alert.
- `ops_error`: operational error routed through `send_error_message`.
- `crash`: unhandled script crash captured by `run_with_alert`.
- `automation_digest`: profile-level failure digest from `automation/runner.py`.

## Database Schema

Use a new `utils/store.py` module for SQLite access.

Do not import `utils.cache` from `utils.store`. Phase 2 will make `utils.cache`
depend on `utils.store`, so importing cache helpers from store creates a future
cycle. Add a tiny path module instead:

- `utils/paths.py`
  - `CACHE_DIR = os.getenv("CACHE_DIR", "")`
  - `cache_path(filename: str) -> str`
- `utils/cache.py`
  - import and re-export `CACHE_DIR` and `cache_path` from `utils.paths`
- `utils/store.py`
  - import `cache_path` from `utils.paths`

Alerts table:

```sql
CREATE TABLE IF NOT EXISTS alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL,
    protocol        TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT '',
    severity        TEXT,
    message         TEXT NOT NULL,
    plain_text      INTEGER NOT NULL DEFAULT 0,
    silent          INTEGER NOT NULL DEFAULT 0,
    delivery_status TEXT NOT NULL DEFAULT 'generated',
    delivered_at    TEXT,
    delivery_error  TEXT,
    dedupe_key      TEXT,
    fingerprint     TEXT,
    metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_events_created_at
    ON alert_events(created_at);

CREATE INDEX IF NOT EXISTS idx_alert_events_protocol_created_id
    ON alert_events(protocol, created_at, id);

CREATE INDEX IF NOT EXISTS idx_alert_events_severity_created_id
    ON alert_events(severity, created_at, id);

CREATE INDEX IF NOT EXISTS idx_alert_events_source_created_id
    ON alert_events(source, created_at, id);
```

Notes:

- Store `created_at` and `delivered_at` as normalized UTC ISO strings ending in `Z`,
  for example `2026-06-11T10:23:45.123456Z`.
- Keep query-specific structured fields in `metadata_json` until there is a real
  need for columns. Do not add vague columns like `value` in v1.
- `dedupe_key` and `fingerprint` are optional future hooks; they are not used by
  Phase 1.
- Use parameterized SQL only.

SQLite connection settings:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

Implementation details:

- Use short-lived connections for scripts.
- API requests should open a short-lived read connection per request.
- Set `row_factory = sqlite3.Row`.
- Guard schema setup with a module-level `_initialized` flag, but still make
  `_ensure_schema(conn)` idempotent.
- Capture failures must be swallowed by callers, not hidden inside every store
  function. This keeps store functions testable.

## Store API

Add typed helpers in `utils/store.py`.

Required types:

- `AlertEvent`: `TypedDict` or dataclass used by query/get helpers.
- `NewAlertEvent`: optional dataclass for insert input if it makes call sites
  cleaner.

Required functions:

```python
def db_path() -> str:
    """Return the SQLite database path under CACHE_DIR."""


def record_alert(
    *,
    message: str,
    protocol: str,
    channel: str = "",
    severity: str | None = None,
    source: str = "protocol",
    plain_text: bool = False,
    silent: bool = False,
    delivery_status: str = "generated",
    metadata: dict[str, object] | None = None,
    dedupe_key: str | None = None,
    fingerprint: str | None = None,
) -> int:
    """Insert an alert event and return its id."""


def update_alert_delivery(
    alert_id: int,
    *,
    status: str,
    delivered_at: str | None = None,
    error: str | None = None,
) -> None:
    """Update Telegram delivery fields for an alert event."""


def get_alert(alert_id: int) -> AlertEvent | None:
    """Return one alert event by id."""


def query_alerts(
    *,
    protocol: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    cursor: int | None = None,
    limit: int = 100,
) -> list[AlertEvent]:
    """Return alert events ordered by id descending."""


def prune_alerts(older_than_days: int) -> int:
    """Delete old alert events and return the number of deleted rows."""
```

Limit rules:

- Default `limit`: 100.
- Maximum `limit`: 500 for the public API.
- Store helper may clamp to 1000 internally for tests/admin callers, but the API
  should enforce 500.

Ordering and pagination SQL shape:

```sql
WHERE (:from_ts IS NULL OR created_at >= :from_ts)
  AND (:to_ts IS NULL OR created_at < :to_ts)
  AND (:cursor IS NULL OR id < :cursor)
ORDER BY id DESC
LIMIT :limit
```

The actual implementation can build dynamic SQL instead of using nullable
parameters, but behavior should match this.

## Alert Capture Wiring

### `utils/telegram.py`

Extend `send_telegram_message` with keyword-only capture metadata so existing
positional callers are untouched:

```python
def send_telegram_message(
    message: str,
    protocol: str,
    disable_notification: bool = False,
    plain_text: bool = False,
    *,
    severity: str | None = None,
    source: str = "protocol",
    origin_protocol: str | None = None,
    channel: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    ...
```

Capture behavior:

1. Truncate messages exactly as today before recording, so the API mirrors what
   Telegram would receive.
2. Insert an alert row before returning for debug/missing-credential cases.
3. Insert an alert row before attempting `_post_message`.
4. After successful `_post_message`, update `delivery_status = 'delivered'`.
5. If `_post_message` raises, update `delivery_status = 'failed'` and preserve
   existing exception behavior.
6. If capture insert/update fails, log at debug with `exc_info=True` and continue.

Add local helpers:

```python
def _record_alert_safe(...) -> int | None:
    """Best-effort alert insert; never raises."""


def _update_alert_delivery_safe(alert_id: int | None, ...) -> None:
    """Best-effort delivery update; never raises."""
```

Special routing:

- In test-chat mode, record the logical protocol as `origin_protocol or protocol`.
  The test chat is a delivery route, not the logical protocol.
- If topic routing is used, `channel` can remain the routing key, not the numeric
  chat id. Do not store Telegram bot tokens or chat ids in the alert DB.
- For missing credentials, record `delivery_status = 'skipped_missing_credentials'`.
- For `LOG_LEVEL=DEBUG`, record `delivery_status = 'skipped_debug'`.

### `utils/alert.py`

`send_alert` already has structured severity. Preserve it:

```python
send_telegram_message(
    message,
    alert.channel or alert.protocol,
    silent,
    plain_text,
    severity=alert.severity.value,
    source="protocol",
    origin_protocol=alert.protocol,
    channel=alert.channel or alert.protocol,
)
```

The recorded message may include the existing emoji prefix. That is acceptable
because `severity` is also stored separately.

### `utils/telegram.send_error_message`

Add a keyword-only source:

```python
def send_error_message(
    message: str,
    protocol: str,
    disable_notification: bool = True,
    *,
    source: str = "ops_error",
) -> None:
    ...
```

When sending to the central errors channel, preserve the origin:

```python
send_telegram_message(
    f"[{protocol}] {message}",
    ERROR_CHANNEL,
    disable_notification,
    plain_text=True,
    source=source,
    origin_protocol=protocol,
    channel=ERROR_CHANNEL,
)
```

### `utils/runner.py`

When an unhandled script crash is caught, call:

```python
send_error_message("\n".join(lines), protocol, source="crash")
```

### `automation/runner.py`

In `_send_failure_digest`, pass:

```python
send_telegram_message(
    message,
    protocol=TELEGRAM_PROTOCOL,
    plain_text=False,
    source="automation_digest",
    origin_protocol=TELEGRAM_PROTOCOL,
    channel=TELEGRAM_PROTOCOL,
)
```

## HTTP API Contract

Add a top-level `api` package:

- `api/__init__.py`
- `api/__main__.py`
- `api/server.py`

Update `pyproject.toml` package discovery to include `api*`.

Default environment:

- `MONITORING_API_HOST=127.0.0.1`
- `MONITORING_API_PORT=8923`
- `CACHE_DIR=/srv/cache` from the systemd unit

Endpoints:

### `GET /healthz`

Response:

```json
{"status":"ok"}
```

### `GET /v1/alerts`

Query parameters:

- `from`: optional inclusive UTC timestamp.
- `to`: optional exclusive UTC timestamp. If omitted, query goes until now.
- `cursor`: optional integer id from the previous response's `next_cursor`.
- `limit`: optional integer, default 100, max 500.
- `protocol`: optional exact protocol filter.
- `severity`: optional one of `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`.
- `source`: optional source filter, for example `protocol` to exclude ops noise.
- `since`: optional alias for `from` for compatibility only. Prefer `from`.

Example:

```http
GET /v1/alerts?from=2026-06-11T00:00:00Z&to=2026-06-12T00:00:00Z&limit=100
```

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

`next_cursor` should be the smallest `id` returned by the page when there may be
more rows. Returning it unconditionally when `len(data) == limit` is acceptable.
The next request passes `cursor=<next_cursor>` to fetch older alerts.

### `GET /v1/alerts/{id}`

Response: one alert object.

Missing row: `404`.

### Errors

Use JSON errors:

```json
{"error":"bad_request","message":"invalid severity"}
```

Status codes:

- `400`: invalid query parameter.
- `404`: unknown path or alert not found.
- `405`: non-GET method.
- `500`: unexpected server error.

Timestamp parsing rules:

- Accept `Z` suffix and explicit offsets.
- Normalize internally to UTC `Z`.
- Reject naive timestamps without timezone.
- Reject `to <= from`.

## API Server Implementation Notes

Use `BaseHTTPRequestHandler` with small routing helpers. Keep request handling
thin and testable:

- `parse_alert_query(query: str) -> AlertQuery`
- `alert_to_json(row: AlertEvent) -> dict[str, object]`
- `write_json(handler, status, payload) -> None`
- `write_error(handler, status, code, message) -> None`

Do not log request headers. They may contain reverse-proxy auth credentials.

Set response headers:

- `Content-Type: application/json`
- `Cache-Control: no-store`

The API is read-only, but the systemd unit still needs write access to
`CACHE_DIR`. SQLite WAL can create or update `monitoring.db-wal` and
`monitoring.db-shm`, even for readers.

## Deploy and Systemd

Add `deploy/systemd/monitoring-api.service`.

It should mirror the hardening style of `deploy/systemd/monitoring.service`:

- `User=__MONITOR_USER__`
- `Group=__MONITOR_USER__`
- `WorkingDirectory=__REPO_DIR__`
- `Environment=REPO_ROOT=__REPO_DIR__`
- `Environment=PATH=__REPO_DIR__/.venv/bin:/usr/local/bin:/usr/bin:/bin`
- `Environment=LOG_LEVEL=INFO`
- `Environment=PYTHONUNBUFFERED=1`
- `Environment=CACHE_DIR=__CACHE_DIR__`
- `Environment=MONITORING_API_HOST=127.0.0.1`
- `Environment=MONITORING_API_PORT=8923`
- `EnvironmentFile=__ETC_DIR__/.env`
- `ExecStart=__REPO_DIR__/.venv/bin/python -m api`
- `Restart=on-failure`
- `ReadWritePaths=__CACHE_DIR__`
- network address family restrictions matching the current unit

Update `deploy/install.sh`:

- Render and install the new unit with the same substitutions as the main unit.
- Run `systemctl daemon-reload`.
- Do not automatically expose the API publicly.

Update `deploy/runbook.md`:

- Document `systemctl enable --now monitoring-api`.
- Document `systemctl status monitoring-api`.
- Document `journalctl -u monitoring-api -f`.
- Document a local health check:
  `curl http://127.0.0.1:8923/healthz`.
- Document reverse-proxy forwarding to `127.0.0.1:8923`.
- Explicitly state that public auth/rate limiting belongs at the reverse proxy.

Update `deploy/systemd/monitoring.service`:

- Replace the stale "There is no HTTP surface and nothing to expose" comment with
  a note that the HTTP API, when enabled, lives in `monitoring-api.service`.

Reverse proxy requirements:

- Bind Python API to localhost only.
- Require bearer token or basic auth at the proxy.
- Add rate limiting.
- Add a max response/request timeout.
- Do not expose `/srv/cache` or the SQLite file directly.

## Retention

Add `utils/prune_alerts.py`.

Behavior:

- Read `ALERTS_RETENTION_DAYS`, default `30`.
- Call `utils.store.prune_alerts(days)`.
- Log deleted count.
- Optionally run `PRAGMA wal_checkpoint(TRUNCATE)` after pruning to keep WAL size
  bounded.
- Wrap entrypoint with `run_with_alert(main, "automation")`.

Add to the daily profile in `automation/jobs.yaml`:

```yaml
- { name: "prune-alerts", script: utils/prune_alerts.py }
```

Retention can ship after the API. It does not need to block Phase 1 or Phase 2.

## Phase 2: Cache Migration to SQLite

This phase is intentionally separate from the alerts API.

Add a `monitor_state` table in `utils/store.py`:

```sql
CREATE TABLE IF NOT EXISTS monitor_state (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
```

Add helpers:

```python
def state_get(namespace: str, key: str) -> str | None:
    """Return a stored monitor state value."""


def state_set(namespace: str, key: str, value: str) -> None:
    """Upsert a monitor state value."""
```

Rewire `utils/cache.py` while keeping public signatures stable:

- Keep `CACHE_DIR`, `cache_path`, `cache_filename`, `nonces_filename`,
  `morpho_filename`, and `morpho_key`.
- `namespace = os.path.basename(filename)`.
- `get_last_value_for_key_from_file(filename, key)`:
  - If SQLite has `(namespace, key)`, return it.
  - Else read the legacy text file if it exists.
  - If found in the text file, write through to SQLite and return it.
  - Else return `0`.
- `write_last_value_to_file(filename, key, value)`:
  - Write `str(value)` to SQLite.
  - Optional transition-only dual-write to text file behind
    `CACHE_DUAL_WRITE_LEGACY=1`.

Rationale:

- Lazy read-through avoids one noisy first tick after cutover.
- Namespace by basename preserves hourly/daily isolation:
  - `cache-id.txt`
  - `cache-id-daily.txt`
  - `nonces.txt`
- No protocol script changes should be needed.

Leave protocol-specific JSON caches alone unless they use `utils.cache`.
For example, `protocols/yearn/check_stuck_triggers.py` has its own JSON cache
and should not be migrated in this phase.

Optional rollback switch:

- Add `CACHE_BACKEND=file|sqlite`, default `sqlite` only after Phase 2 is ready.
- `file` path uses the current implementation.
- This is useful if the cache migration ships independently from the API.

## Rollout Plan

### PR 1: SQLite store and best-effort alert capture

Files:

- `utils/paths.py`
- `utils/store.py`
- `utils/cache.py` path import/re-export only
- `utils/telegram.py`
- `utils/alert.py`
- `utils/runner.py`
- `automation/runner.py`
- tests

Tasks:

- [ ] Add `utils/paths.py`.
- [ ] Add `utils/store.py` with alerts schema and helpers.
- [ ] Add safe capture helpers in `utils/telegram.py`.
- [ ] Capture raw `send_telegram_message` calls.
- [ ] Capture structured `send_alert` severity and origin protocol.
- [ ] Capture ops errors with `source="ops_error"`.
- [ ] Capture crash alerts with `source="crash"`.
- [ ] Capture automation digests with `source="automation_digest"`.
- [ ] Add unit tests for store schema, insert, update, get, query, and prune.
- [ ] Add capture tests proving DB failures are swallowed.
- [ ] Run formatting, lint, type checks, and tests.

Acceptance criteria:

- Existing alert behavior is unchanged if SQLite capture fails.
- Existing direct positional calls to `send_telegram_message` still work.
- Running any monitor locally creates `monitoring.db` under `CACHE_DIR`.
- Alerts are queryable directly through store helpers.

Deployment:

- Code-only. Existing `monitoring.service` auto-sync can land it.
- No systemd restart is required unless environment changes are made.

### PR 2: HTTP API service and deploy docs

Files:

- `api/__init__.py`
- `api/__main__.py`
- `api/server.py`
- `pyproject.toml`
- `deploy/systemd/monitoring-api.service`
- `deploy/install.sh`
- `deploy/runbook.md`
- `deploy/systemd/monitoring.service`
- tests

Tasks:

- [ ] Add `api` package.
- [ ] Implement `/healthz`.
- [ ] Implement `/v1/alerts`.
- [ ] Implement `/v1/alerts/{id}`.
- [ ] Implement timestamp parsing and validation.
- [ ] Implement cursor pagination.
- [ ] Implement JSON error responses.
- [ ] Add package discovery for `api*`.
- [ ] Add API systemd unit.
- [ ] Update install script to render/install the API unit.
- [ ] Update runbook with enable/start/status/logs/proxy instructions.
- [ ] Update stale "no HTTP surface" comment in `monitoring.service`.
- [ ] Add API tests.
- [ ] Run formatting, lint, type checks, and tests.

Acceptance criteria:

- `python -m api` starts locally.
- `curl http://127.0.0.1:8923/healthz` returns `{"status":"ok"}`.
- `/v1/alerts` supports `from`, `to`, `cursor`, `limit`, `protocol`,
  `severity`, and `source`.
- Bad severity returns `400`.
- Unknown alert id returns `404`.
- The API process can be stopped or crashed without stopping `monitoring.service`.

Deployment:

- Operator installs/enables the new unit.
- Operator configures reverse proxy auth and rate limits.

### PR 3: Retention

Files:

- `utils/prune_alerts.py`
- `automation/jobs.yaml`
- tests

Tasks:

- [ ] Add prune script.
- [ ] Add daily automation task.
- [ ] Add tests for retention day parsing and prune call.
- [ ] Run formatting, lint, type checks, and tests.

Acceptance criteria:

- Old rows are deleted based on `ALERTS_RETENTION_DAYS`.
- The daily profile includes the prune task.
- Prune failures alert through the existing crash wrapper.

Deployment:

- Code-only for the new task body.
- If changing daily cron cadence, restart `monitoring.service`; otherwise no
  restart is needed after auto-sync.

### PR 4: Cache migration to SQLite

Files:

- `utils/store.py`
- `utils/cache.py`
- tests

Tasks:

- [ ] Add `monitor_state` table.
- [ ] Add `state_get` and `state_set`.
- [ ] Rewire `get_last_value_for_key_from_file`.
- [ ] Rewire `write_last_value_to_file`.
- [ ] Preserve all public function signatures.
- [ ] Preserve missing-key return value `0`.
- [ ] Preserve basename namespace behavior for daily/hourly isolation.
- [ ] Add lazy read-through from legacy text files.
- [ ] Add optional `CACHE_BACKEND=file|sqlite` rollback switch if desired.
- [ ] Add tests for namespace isolation, int casts, missing keys, and lazy import.
- [ ] Run formatting, lint, type checks, and tests.

Acceptance criteria:

- No protocol script changes are needed.
- Existing tests that patch `utils.cache` wrappers still pass.
- Existing cache files can remain on disk without affecting SQLite reads after
  values have been imported.

Deployment:

- Land separately after the API is stable.
- Watch the first hourly and daily runs after cutover.

## Test Plan

### Store tests

Create `tests/test_store.py`.

Cases:

- [ ] `db_path` uses `CACHE_DIR`.
- [ ] Schema setup is idempotent.
- [ ] `record_alert` round-trips required fields.
- [ ] `record_alert` handles `NULL` severity.
- [ ] `metadata` is stored and returned as JSON.
- [ ] `update_alert_delivery` sets delivered status and timestamp.
- [ ] `get_alert` returns `None` for missing ids.
- [ ] `query_alerts` orders by `id DESC`.
- [ ] `query_alerts` filters by protocol.
- [ ] `query_alerts` filters by severity.
- [ ] `query_alerts` filters by source.
- [ ] `query_alerts` filters by `from_ts`.
- [ ] `query_alerts` filters by `to_ts`.
- [ ] `query_alerts` applies `cursor`.
- [ ] `query_alerts` clamps limit.
- [ ] `prune_alerts` deletes old rows and returns row count.
- [ ] WAL smoke test: one connection writes while another reads.

### Capture tests

Create `tests/test_alert_capture.py`.

Cases:

- [ ] Raw `send_telegram_message` records `source="protocol"` and no severity.
- [ ] `send_alert` records severity and origin protocol.
- [ ] `send_error_message` records `source="ops_error"` and origin protocol.
- [ ] `run_with_alert` crash path records `source="crash"`.
- [ ] Automation digest records `source="automation_digest"`.
- [ ] Capture insert failure is swallowed.
- [ ] Delivery update failure is swallowed.
- [ ] Telegram failure still raises `TelegramError` after marking failed.
- [ ] Missing credentials records `skipped_missing_credentials`.
- [ ] `LOG_LEVEL=DEBUG` records `skipped_debug`.

### API tests

Create `tests/test_alerts_api.py`.

Cases:

- [ ] `/healthz` returns ok.
- [ ] `/v1/alerts` returns seeded rows.
- [ ] `/v1/alerts/{id}` returns one row.
- [ ] `/v1/alerts/{id}` returns `404` when missing.
- [ ] Protocol filter works.
- [ ] Severity filter works.
- [ ] Source filter works.
- [ ] `from` filter works.
- [ ] `to` filter works.
- [ ] `cursor` returns older rows only.
- [ ] Limit is clamped.
- [ ] Bad severity returns `400`.
- [ ] Naive timestamp returns `400`.
- [ ] `to <= from` returns `400`.
- [ ] Unknown route returns `404`.
- [ ] Non-GET returns `405`.

### Cache migration tests

Create or extend cache tests in the Phase 2 PR.

Cases:

- [ ] Missing key returns `0`.
- [ ] String values round-trip.
- [ ] Int wrapper casts still work.
- [ ] Float-looking strings round-trip.
- [ ] Namespace isolation by basename works.
- [ ] Daily and hourly namespaces do not collide.
- [ ] Legacy text file read-through imports a value into SQLite.
- [ ] Write updates existing row.
- [ ] Optional file backend still uses legacy behavior if implemented.

## Local Verification Commands

Run before each PR is handed off:

```sh
uv run ruff format .
uv run ruff check .
uv run mypy .
uv run pytest tests/
```

Manual smoke test after PR 1:

```sh
mkdir -p /tmp/monitoring-alerts-api
CACHE_DIR=/tmp/monitoring-alerts-api LOG_LEVEL=DEBUG uv run python protocols/aave/main.py
sqlite3 /tmp/monitoring-alerts-api/monitoring.db 'select id, created_at, protocol, source, severity, delivery_status from alert_events order by id desc limit 5;'
```

Manual smoke test after PR 2:

```sh
CACHE_DIR=/tmp/monitoring-alerts-api uv run python -m api
curl http://127.0.0.1:8923/healthz
curl 'http://127.0.0.1:8923/v1/alerts?limit=10&source=protocol'
```

## Operational Risks and Mitigations

### API overload or DDoS

Mitigations:

- API runs as a separate systemd service.
- API binds to localhost.
- Reverse proxy enforces auth and rate limits.
- API clamps `limit`.
- API uses indexed queries only.

Remaining risk:

- If the VPS itself is CPU, memory, disk, or file-descriptor exhausted, monitoring
  jobs can still be affected. This is true for any API on the same host.

### SQLite lock contention

Mitigations:

- WAL mode.
- `busy_timeout=5000`.
- Short-lived connections.
- No long API transactions.
- Indexed filters.
- Separate API and cron processes.

### Disk growth

Mitigations:

- Daily prune task.
- Optional WAL checkpoint after prune.
- Reverse proxy request limits.

### Alert delivery ambiguity

Mitigation:

- Store generated alerts separately from delivery status.
- API consumers can filter or inspect `delivery_status`.

### Future circular imports

Mitigation:

- Put `CACHE_DIR` and `cache_path` in `utils/paths.py`.
- Do not let `utils/store.py` import `utils/cache.py`.

### Local filesystem requirement

Mitigation:

- Confirm `/srv/cache` is local disk.
- Do not put SQLite WAL databases on NFS.

## Definition of Done

The whole project is complete when:

- [ ] Alerts are persisted to SQLite without affecting Telegram delivery.
- [ ] The API is available from localhost through `monitoring-api.service`.
- [ ] API consumers can query by timestamp range, protocol, severity, source, and
  cursor.
- [ ] `monitoring.service` continues to run if `monitoring-api.service` is stopped.
- [ ] Retention prevents unbounded DB growth.
- [ ] Cache migration is either complete or explicitly left as a separate tracked
  follow-up with no API dependency.
- [ ] Runbook documents API operation and reverse-proxy responsibilities.

