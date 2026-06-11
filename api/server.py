from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from utils.logging import get_logger
from utils.store import AlertEvent, get_alert, query_alerts

logger = get_logger("api.server")

ALLOWED_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
MAX_LIMIT = 500


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
    raw = value
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BadRequest(f"invalid {name} timestamp") from exc
    if parsed.tzinfo is None:
        raise BadRequest(f"{name} timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_alert_query(query: str) -> AlertQuery:
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
        if cursor < 1:
            raise BadRequest("invalid cursor")

    from_raw = _one(params, "from") or _one(params, "since")
    to_raw = _one(params, "to")
    from_ts = parse_timestamp(from_raw, "from") if from_raw else None
    to_ts = parse_timestamp(to_raw, "to") if to_raw else None
    if from_ts and to_ts and to_ts <= from_ts:
        raise BadRequest("to must be after from")

    return AlertQuery(
        protocol=_one(params, "protocol"),
        severity=severity,
        source=_one(params, "source"),
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


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, object]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def write_error(handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
    write_json(handler, status, {"error": code, "message": message})


class AlertsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                write_json(self, 200, {"status": "ok"})
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
                next_cursor = str(min(row["id"] for row in rows)) if len(rows) == alert_query.limit else None
                write_json(self, 200, {"data": data, "next_cursor": next_cursor, "limit": alert_query.limit})
                return
            if parsed.path.startswith("/v1/alerts/"):
                alert_id_raw = parsed.path.removeprefix("/v1/alerts/")
                try:
                    alert_id = int(alert_id_raw)
                except ValueError:
                    write_error(self, 404, "not_found", "unknown path")
                    return
                row = get_alert(alert_id)
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


def run(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), AlertsHandler)
    logger.info("monitoring API listening on %s:%d", host, port)
    server.serve_forever()


def main() -> None:
    host = os.getenv("MONITORING_API_HOST", "127.0.0.1")
    port = int(os.getenv("MONITORING_API_PORT", "8923"))
    run(host, port)
