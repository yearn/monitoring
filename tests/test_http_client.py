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
