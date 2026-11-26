from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from api.guards import ConcurrencyGate, RateLimiter, client_key
from automation.config import JobsConfig, load_jobs_config
from utils.logger import get_logger
from utils.monitoring_config import load_monitoring_config, monitoring_to_json
from utils.store import AlertEvent, get_alert, normalize_timestamp, query_alerts

logger = get_logger("api.server")

ALLOWED_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
MAX_LIMIT = 500
# Alert ids are SQLite INTEGERs (signed 64-bit); larger values overflow the driver.
MAX_ALERT_ID = 2**63 - 1
# Real queries are well under 100 chars; anything this long is probing or abuse.
MAX_QUERY_LENGTH = 2048
# protocol/source filter values: identifiers like "aave", "yearn-internal", "YEARN_MS".
_FILTER_VALUE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Socket timeout per connection. Caddy already enforces its own; this bounds direct
# local callers and anything that slips past it, so a stalled client can't pin a thread.
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_MAX_IN_FLIGHT = 16


class BadRequest(ValueError):
    pass


@dataclass(frozen=True)
class AlertQuery:
    protocol: str | None = None
    severity: str | None = None
    source: str | None = None
    from_ts: str | None = None
    to_ts: str | None = None
    cursor: int | None = None
    limit: int = 100


def _one(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    return values[-1]


def parse_timestamp(value: str, name: str) -> str:
    try:
        return normalize_timestamp(value)
    except ValueError as exc:
        message = f"{name} timestamp must include timezone" if "timezone" in str(exc) else f"invalid {name} timestamp"
        raise BadRequest(message) from exc


def _filter_param(params: dict[str, list[str]], key: str) -> str | None:
    """Return the ``key`` filter value, rejecting anything that isn't a plain identifier."""
    value = _one(params, key)
    if value is not None and not _FILTER_VALUE_RE.fullmatch(value):
        raise BadRequest(f"invalid {key}")
    return value


def parse_alert_query(query: str) -> AlertQuery:
    if len(query) > MAX_QUERY_LENGTH:
        raise BadRequest("query too long")
    params = parse_qs(query, keep_blank_values=True)
    severity = _one(params, "severity")
    if severity is not None and severity not in ALLOWED_SEVERITIES:
        raise BadRequest("invalid severity")

    limit = 100
    limit_raw = _one(params, "limit")
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError as exc:
            raise BadRequest("invalid limit") from exc
        if limit < 1:
            raise BadRequest("invalid limit")
    limit = min(limit, MAX_LIMIT)

    cursor = None
    cursor_raw = _one(params, "cursor")
    if cursor_raw:
        try:
            cursor = int(cursor_raw)
        except ValueError as exc:
            raise BadRequest("invalid cursor") from exc
        if not 1 <= cursor <= MAX_ALERT_ID:
            raise BadRequest("invalid cursor")

    from_raw = _one(params, "from") or _one(params, "since")
    to_raw = _one(params, "to")
    from_ts = parse_timestamp(from_raw, "from") if from_raw else None
    to_ts = parse_timestamp(to_raw, "to") if to_raw else None
    if from_ts and to_ts and to_ts <= from_ts:
        raise BadRequest("to must be after from")

    return AlertQuery(
        protocol=_filter_param(params, "protocol"),
        severity=severity,
        source=_filter_param(params, "source"),
        from_ts=from_ts,
        to_ts=to_ts,
        cursor=cursor,
        limit=limit,
    )


def alert_to_json(row: AlertEvent) -> dict[str, object]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "source": row["source"],
        "protocol": row["protocol"],
        "channel": row["channel"],
        "severity": row["severity"],
        "message": row["message"],
        "plain_text": row["plain_text"],
        "silent": row["silent"],
        "delivery_status": row["delivery_status"],
        "delivered_at": row["delivered_at"],
        "delivery_error": row["delivery_error"],
        "metadata": row["metadata"],
    }


def _protocol_from_script(script: str) -> str | None:
    parts = script.split("/")
    if len(parts) < 2 or parts[0] != "protocols" or not parts[1]:
        return None
    return parts[1]


def protocols_to_json(config: JobsConfig) -> dict[str, object]:
    protocols: dict[str, list[dict[str, object]]] = {}
    for profile in config.enabled_profiles:
        for task in profile.enabled_tasks:
            protocol = _protocol_from_script(task.script)
            if protocol is not None:
                protocols.setdefault(protocol, []).append(
                    {
                        "name": task.name,
                        "script": task.script,
                        "args": task.args,
                        "profile": profile.name,
                        "cron": profile.cron,
                    }
                )
    data = [{"name": protocol, "tasks": tasks} for protocol, tasks in sorted(protocols.items())]
    return {"data": data, "count": len(data)}


def write_json(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict[str, object],
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def write_error(
    handler: BaseHTTPRequestHandler,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> None:
    write_json(handler, status, {"error": code, "message": message}, headers)


class AlertsHandler(BaseHTTPRequestHandler):
    # Don't advertise the Python version (Caddy strips Server too; this covers direct callers).
    server_version = "monitoring-api"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS
    # Set by configure_guards(); None (no limit) keeps tests and ad-hoc servers unthrottled.
    rate_limiter: RateLimiter | None = None
    gate: ConcurrencyGate | None = None

    def do_GET(self) -> None:
        """Apply the concurrency and rate guards, then route the request."""
        gate = self.gate
        if gate is not None and not gate.try_enter():
            write_error(self, 503, "busy", "too many concurrent requests", {"Retry-After": "1"})
            return
        try:
            limiter = self.rate_limiter
            if limiter is not None:
                retry_after = limiter.acquire(client_key(self.headers.get("X-Forwarded-For"), self.client_address[0]))
                if retry_after > 0:
                    write_error(
                        self, 429, "rate_limited", "too many requests", {"Retry-After": str(math.ceil(retry_after))}
                    )
                    return
            self._route()
        finally:
            if gate is not None:
                gate.leave()

    def _route(self) -> None:
        """Dispatch a GET that passed the guards to its endpoint."""
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                write_json(self, 200, {"status": "ok"})
                return
            if parsed.path == "/v1/protocols":
                write_json(self, 200, protocols_to_json(load_jobs_config()))
                return
            if parsed.path == "/v1/monitoring":
                write_json(self, 200, monitoring_to_json(load_monitoring_config()))
                return
            if parsed.path == "/v1/alerts":
                alert_query = parse_alert_query(parsed.query)
                rows = query_alerts(
                    protocol=alert_query.protocol,
                    severity=alert_query.severity,
                    source=alert_query.source,
                    from_ts=alert_query.from_ts,
                    to_ts=alert_query.to_ts,
                    cursor=alert_query.cursor,
                    limit=alert_query.limit,
                )
                data = [alert_to_json(row) for row in rows]
                # Rows are ordered id DESC, so the last row carries the smallest id.
                next_cursor = str(rows[-1]["id"]) if len(rows) == alert_query.limit else None
                write_json(self, 200, {"data": data, "next_cursor": next_cursor, "limit": alert_query.limit})
                return
            if parsed.path.startswith("/v1/alerts/"):
                alert_id_raw = parsed.path.removeprefix("/v1/alerts/")
                try:
                    alert_id = int(alert_id_raw)
                except ValueError:
                    write_error(self, 404, "not_found", "unknown path")
                    return
                row = get_alert(alert_id) if 1 <= alert_id <= MAX_ALERT_ID else None
                if row is None:
                    write_error(self, 404, "not_found", "alert not found")
                    return
                write_json(self, 200, alert_to_json(row))
                return
            write_error(self, 404, "not_found", "unknown path")
        except BadRequest as exc:
            write_error(self, 400, "bad_request", str(exc))
        except Exception:
            logger.exception("API request failed for path %s", parsed.path)
            write_error(self, 500, "server_error", "unexpected server error")

    def do_POST(self) -> None:
        write_error(self, 405, "method_not_allowed", "only GET is supported")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


def _env_int(name: str, default: int) -> int:
    """Read a non-negative integer setting from the environment, failing loudly on bad values."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def configure_guards() -> None:
    """Install the rate limiter and concurrency cap from the environment.

    ``MONITORING_API_RATE_LIMIT_PER_MINUTE`` (default 60, 0 disables) caps requests per
    client; ``MONITORING_API_MAX_IN_FLIGHT`` (default 16, 0 disables) caps concurrent ones.
    """
    per_minute = _env_int("MONITORING_API_RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE)
    max_in_flight = _env_int("MONITORING_API_MAX_IN_FLIGHT", DEFAULT_MAX_IN_FLIGHT)
    AlertsHandler.rate_limiter = RateLimiter(per_minute, 60) if per_minute else None
    AlertsHandler.gate = ConcurrencyGate(max_in_flight) if max_in_flight else None
    logger.info(
        "request guards: %s req/min per client, %s in flight", per_minute or "unlimited", max_in_flight or "unlimited"
    )


def run(host: str, port: int) -> None:
    configure_guards()
    server = ThreadingHTTPServer((host, port), AlertsHandler)
    logger.info("monitoring API listening on %s:%d", host, port)
    server.serve_forever()


def main() -> None:
    host = os.getenv("MONITORING_API_HOST", "127.0.0.1")
    port = int(os.getenv("MONITORING_API_PORT", "8923"))
    run(host, port)
