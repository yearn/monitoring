from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import cast
from urllib.parse import ParseResult

import pytest

from api import server as api_server
from api.guards import ConcurrencyGate, RateLimiter, client_key
from api.server import AlertsHandler, AlertsServer, configure_guards
from utils import paths, store


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_allows_burst_then_refills():
    clock = FakeClock()
    limiter = RateLimiter(3, 60, clock=clock)
    assert [limiter.acquire("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.acquire("a") == pytest.approx(20.0)  # 1 token per 20s
    assert limiter.acquire("b") == 0.0  # other clients are unaffected

    clock.now += 20
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") > 0


def test_rate_limiter_rejected_requests_do_not_extend_the_wait():
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock=clock)
    assert limiter.acquire("a") == 0.0
    for _ in range(5):
        limiter.acquire("a")
    clock.now += 60
    assert limiter.acquire("a") == 0.0


def test_rate_limiter_samples_time_under_lock(monkeypatch):
    lock = threading.Lock()

    def clock() -> float:
        assert lock.locked()
        return 1000.0

    limiter = RateLimiter(1, 60, clock=clock)
    monkeypatch.setattr(limiter, "_lock", lock)
    assert limiter.acquire("a") == 0.0


def test_rate_limiter_evicts_least_recent_client():
    limiter = RateLimiter(1, 60, max_clients=2, clock=FakeClock())
    limiter.acquire("a")
    limiter.acquire("b")
    limiter.acquire("c")  # evicts "a"
    assert limiter.acquire("a") == 0.0  # fresh bucket
    assert limiter.acquire("c") > 0


def test_rate_limiter_rejects_bad_config():
    with pytest.raises(ValueError):
        RateLimiter(0, 60)
    with pytest.raises(ValueError):
        RateLimiter(1, 0)


def test_concurrency_gate():
    gate = ConcurrencyGate(1)
    assert gate.try_enter()
    assert not gate.try_enter()
    gate.leave()
    assert gate.try_enter()


@pytest.mark.parametrize(
    ("proxy_client_ip", "expected"),
    [
        (None, "127.0.0.1"),
        ("", "127.0.0.1"),
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:0:0:0:0:0:7", "2001:db8::7"),
        ("198.51.100.1, 203.0.113.7", "127.0.0.1"),
    ],
)
def test_client_key(proxy_client_ip, expected):
    assert client_key(proxy_client_ip, "127.0.0.1") == expected


def test_configure_guards_reads_env(monkeypatch):
    monkeypatch.setattr(AlertsHandler, "rate_limiter", None)
    monkeypatch.setattr(AlertsHandler, "worker_rate_limiter", None)
    monkeypatch.setattr(AlertsHandler, "gate", None)
    monkeypatch.setenv("MONITORING_API_RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setenv("MONITORING_API_WORKER_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("MONITORING_API_MAX_IN_FLIGHT", "4")
    configure_guards()
    assert AlertsHandler.rate_limiter is None
    assert AlertsHandler.worker_rate_limiter is not None
    assert isinstance(AlertsHandler.gate, ConcurrencyGate)

    monkeypatch.setenv("MONITORING_API_RATE_LIMIT_PER_MINUTE", "abc")
    with pytest.raises(ValueError, match="MONITORING_API_RATE_LIMIT_PER_MINUTE"):
        configure_guards()


def _get(server: ThreadingHTTPServer, path: str, headers: dict[str, str] | None = None):
    host, port = cast(tuple[str, int], server.server_address)
    conn = HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read().decode())
    finally:
        conn.close()


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)
    monkeypatch.setattr(AlertsHandler, "rate_limiter", None)
    monkeypatch.setattr(AlertsHandler, "worker_rate_limiter", None)
    monkeypatch.setattr(AlertsHandler, "gate", None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), AlertsHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_rate_limited_requests_get_429_per_client(server, monkeypatch):
    monkeypatch.setattr(AlertsHandler, "rate_limiter", RateLimiter(2, 60))
    a = {"X-Monitoring-Client-IP": "203.0.113.7"}
    assert _get(server, "/healthz", a)[0] == 200
    assert _get(server, "/v1/alerts", a)[0] == 200
    assert _get(server, "/v1/alerts", a)[0] == 200
    status, headers, body = _get(server, "/v1/alerts", a)
    assert status == 429
    assert body["error"] == "rate_limited"
    assert 1 <= int(headers["Retry-After"]) <= 30
    assert _get(server, "/v1/alerts", {"X-Monitoring-Client-IP": "198.51.100.1"})[0] == 200


def test_worker_has_one_separate_budget_across_egress_ips(server, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(AlertsHandler, "rate_limiter", RateLimiter(1, 60, clock=clock))
    monkeypatch.setattr(AlertsHandler, "worker_rate_limiter", RateLimiter(2, 60, clock=clock))
    a = {"X-Monitoring-Client-IP": "203.0.113.7"}
    worker = {"X-Monitoring-Rate-Class": "worker"}
    assert _get(server, "/v1/alerts", a)[0] == 200
    assert _get(server, "/v1/alerts", a)[0] == 429
    assert _get(server, "/v1/alerts", a | worker)[0] == 200
    assert _get(server, "/v1/alerts", {"X-Monitoring-Client-IP": "198.51.100.1"} | worker)[0] == 200
    assert _get(server, "/v1/alerts", {"X-Monitoring-Client-IP": "192.0.2.1"} | worker)[0] == 429


def test_full_gate_returns_503(server, monkeypatch):
    gate = ConcurrencyGate(1)
    monkeypatch.setattr(AlertsHandler, "gate", gate)
    assert gate.try_enter()  # simulate a request already in flight
    status, headers, body = _get(server, "/v1/alerts")
    assert (status, body["error"], headers["Retry-After"]) == (503, "busy", "1")
    assert _get(server, "/healthz")[0] == 200
    gate.leave()
    assert _get(server, "/v1/alerts")[0] == 200


def test_connection_limit_returns_503_before_starting_a_handler(monkeypatch):
    entered = threading.Event()
    finish = threading.Event()
    route = AlertsHandler._route

    def hold_route(handler: AlertsHandler, parsed: ParseResult) -> None:
        entered.set()
        assert finish.wait(5)
        route(handler, parsed)

    monkeypatch.setattr(AlertsHandler, "_route", hold_route)
    with AlertsServer(("127.0.0.1", 0), max_workers=1) as bounded_server, ThreadPoolExecutor() as clients:
        thread = threading.Thread(target=bounded_server.serve_forever, daemon=True)
        thread.start()
        try:
            first = clients.submit(_get, bounded_server, "/healthz")
            assert entered.wait(5)
            status, headers, body = _get(bounded_server, "/healthz")
            assert (status, body["error"], headers["Retry-After"]) == (503, "busy", "1")
        finally:
            finish.set()
            bounded_server.shutdown()
            thread.join(5)
        assert first.result()[0] == 200


def test_response_headers_hide_python_version(server):
    _, headers, _ = _get(server, "/healthz")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Server"].startswith("monitoring-api")
    assert "Python" not in headers["Server"]


@pytest.mark.parametrize(
    "query",
    [
        "protocol=" + "a" * 65,
        "protocol=aave%27%20OR%201=1",
        "source=%00",
        "protocol=",
        "limit=1&" + "x=" + "y" * api_server.MAX_QUERY_LENGTH,
    ],
)
def test_bad_filters_and_long_queries_are_rejected(server, query):
    status, _, body = _get(server, f"/v1/alerts?{query}")
    assert (status, body["error"]) == (400, "bad_request")


def test_real_filter_values_are_accepted(server):
    for protocol in ("aave", "yearn-internal", "YEARN_TIMELOCK_INTERNAL", "3jane"):
        assert _get(server, f"/v1/alerts?protocol={protocol}&source=timelock_subscriber")[0] == 200
