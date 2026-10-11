"""Tests for shared HTTP retry behavior."""

from collections.abc import Iterator

import pytest
import requests

from utils import http_client


def _response(status_code: int) -> requests.Response:
    """Build a minimal response whose ``raise_for_status`` behaves normally."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://example.com/data"
    return response


def test_request_with_retry_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: Iterator[requests.Response] = iter((_response(429), _response(200)))
    calls: list[str] = []

    def request(method: str, url: str, **_kwargs: object) -> requests.Response:
        calls.append(f"{method}:{url}")
        return next(responses)

    monkeypatch.setattr(http_client.requests, "request", request)
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)

    response = http_client.request_with_retry(
        "get",
        "https://example.com/data",
        retries=1,
        backoff_factor=0,
        timeout=1,
    )

    assert response.status_code == 200
    assert len(calls) == 2


def test_request_with_retry_retries_tls_error(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: Iterator[requests.Response | requests.exceptions.SSLError] = iter(
        (requests.exceptions.SSLError("unexpected EOF"), _response(200))
    )
    calls: list[str] = []

    def request(method: str, url: str, **_kwargs: object) -> requests.Response:
        calls.append(f"{method}:{url}")
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(http_client.requests, "request", request)
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)

    response = http_client.request_with_retry(
        "get",
        "https://example.com/data",
        retries=1,
        backoff_factor=0,
        timeout=1,
    )

    assert response.status_code == 200
    assert len(calls) == 2


def test_request_with_retry_does_not_retry_permanent_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def request(method: str, url: str, **_kwargs: object) -> requests.Response:
        calls.append(f"{method}:{url}")
        return _response(400)

    monkeypatch.setattr(http_client.requests, "request", request)

    with pytest.raises(requests.HTTPError):
        http_client.request_with_retry(
            "get",
            "https://example.com/data",
            retries=3,
            backoff_factor=0,
            timeout=1,
        )

    assert len(calls) == 1


def test_request_with_retry_stops_after_rate_limit_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def request(method: str, url: str, **_kwargs: object) -> requests.Response:
        calls.append(f"{method}:{url}")
        return _response(429)

    monkeypatch.setattr(http_client.requests, "request", request)
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)

    with pytest.raises(requests.HTTPError):
        http_client.request_with_retry(
            "get",
            "https://example.com/data",
            retries=2,
            backoff_factor=0,
            timeout=1,
        )

    assert len(calls) == 3


@pytest.mark.parametrize("failure", [400, 503, "timeout", "invalid-json"])
def test_fetch_json_allows_caller_to_report_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, failure: int | str
) -> None:
    """Optional clients can report their own outage without shared ERROR logs."""
    from unittest.mock import Mock

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        assert "log_errors" not in kwargs
        if failure == "timeout":
            raise requests.Timeout("read timeout")
        response = _response(failure if isinstance(failure, int) else 200)
        response._content = b"not json"
        return response

    monkeypatch.setattr(http_client.requests, "request", request)
    log_error = Mock()
    monkeypatch.setattr(http_client.logger, "error", log_error)

    assert http_client.fetch_json("https://example.com/data", retries=0, log_errors=False) is None
    log_error.assert_not_called()


def test_fetch_json_still_logs_failures_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Required API callers retain existing error reporting."""
    from unittest.mock import Mock

    monkeypatch.setattr(http_client.requests, "request", lambda *_args, **_kwargs: _response(400))
    log_error = Mock()
    monkeypatch.setattr(http_client.logger, "error", log_error)

    assert http_client.fetch_json("https://example.com/data") is None
    assert log_error.call_count == 2
