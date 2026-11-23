"""Tests for the DefiLlama Verified loan feed client."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import requests

from utils import defillama_verified as verified

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "defillama_verified_3jane.json"
NOW = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
CONFIG = verified.VerifiedFeedConfig(
    slug="3jane",
    report_max_age=timedelta(hours=3),
    loan_max_ages={
        ("Warehouse Loans", "GigFi"): timedelta(days=10),
        ("Forward Flows", "Settle"): timedelta(days=38),
    },
    balance_max_ages={("Bank Cash", "Erebor Bank"): timedelta(hours=3)},
)


@pytest.fixture
def payload() -> dict[str, Any]:
    """Load a fresh copy of the captured response."""
    return json.loads(FIXTURE_PATH.read_text(), parse_float=Decimal)


def test_parse_live_sample(payload: dict[str, Any]) -> None:
    """Read actual source fields and recompute the principal at full precision."""
    report = verified.parse_report(payload, CONFIG, NOW)
    assert report.project_id == "3jane"
    assert report.up_to_date is True
    assert report.timestamp == datetime(2026, 11, 23, 14, 15, 9, tzinfo=timezone.utc)
    assert report.total_principal_usd == Decimal("98615763.74")
    assert report.loans["Warehouse Loans", "LendSwift"] == verified.VerifiedLoan(
        principal_usd=Decimal("6564597.25"),
        report_date=date(2026, 11, 20),
        collateral_usd=Decimal("8997455.51"),
    )
    assert report.loans["Forward Flows", "Settle"].collateral_usd is None
    assert report.balances["Bank Cash", "Erebor Bank"].balance_usd == Decimal("12602750.86")
    assert verified.report_issues(report, CONFIG, NOW) == {}


@pytest.mark.parametrize(
    ("path", "invalid", "reason"),
    [
        (("projectId",), "noon", "projectId"),
        (("state", "upToDate"), "false", "upToDate"),
        (("data", "timestampOnRun"), 1795443309000, "timestampOnRun"),
        (("data", "timestampOnRun"), True, "timestampOnRun"),
        (("data", "loans"), [], "data.loans"),
        (("data", "loans", "Warehouse Loans"), [], "Warehouse Loans"),
        (("data", "loans", "Forward Flows", "Settle"), None, "Settle"),
        (("data", "loans", "Forward Flows", "Settle", "principalUsd"), True, "principalUsd"),
        (("data", "loans", "Forward Flows", "Settle", "principalUsd"), "40409976.09", "principalUsd"),
        (("data", "loans", "Forward Flows", "Settle", "principalUsd"), -1, "principalUsd"),
        (("data", "loans", "Forward Flows", "Settle", "principalUsd"), Decimal("NaN"), "principalUsd"),
        (("data", "loans", "Forward Flows", "Settle", "principalUsd"), Decimal("Infinity"), "principalUsd"),
        (("data", "loans", "Forward Flows", "Settle", "reportDate"), "20261111", "YYYY-MM-DD"),
        (("data", "loans", "Forward Flows", "Settle", "reportDate"), "2026-11-31", "valid calendar date"),
        (("data", "loans", "Forward Flows", "Settle", "reportDate"), "2026-11-24", "in the future"),
        (("data", "balances", "Bank Cash", "Erebor Bank", "timestamp"), 1795443610, "timestamp"),
        (("data", "balances", "Bank Cash", "Erebor Bank", "balanceUsd"), -1, "balanceUsd"),
        (("data", "totalPrincipalUsd"), 98615764, "sum of originator principal"),
    ],
)
def test_invalid_fields(payload: dict[str, Any], path: tuple[str, ...], invalid: Any, reason: str) -> None:
    """Reject malformed or inconsistent fields before using the report."""
    parent = payload
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = invalid

    with pytest.raises(verified.DefiLlamaVerifiedError, match=reason):
        verified.parse_report(payload, CONFIG, NOW)


def test_report_and_required_source_freshness(payload: dict[str, Any]) -> None:
    """Check report age, balance age and document ages independently."""
    payload["state"]["upToDate"] = False
    report = verified.parse_report(payload, CONFIG, NOW)

    findings = verified.report_issues(report, CONFIG, NOW + timedelta(days=1))
    assert set(findings) == {
        "provider_state",
        "report_age",
        "loan_stale:GigFi (Warehouse Loans)",
        "balance_stale:Erebor Bank (Bank Cash)",
    }


def test_missing_required_sources(payload: dict[str, Any]) -> None:
    """Report disappearing origins and cash sources even if the total still reconciles."""
    data = payload["data"]
    data["loans"]["Warehouse Loans"].pop("GigFi")
    data["totalPrincipalUsd"] -= 100_000
    del data["balances"]

    findings = verified.report_issues(verified.parse_report(payload, CONFIG, NOW), CONFIG, NOW)
    assert set(findings) == {
        "loan_missing:GigFi (Warehouse Loans)",
        "balance_missing:Erebor Bank (Bank Cash)",
    }


def test_age_boundary(payload: dict[str, Any]) -> None:
    """Accept ages exactly at the configured limit."""
    report = verified.parse_report(payload, CONFIG, NOW)
    at_limit = report.timestamp + CONFIG.report_max_age
    findings = verified.report_issues(report, CONFIG, at_limit)
    assert "report_age" not in findings
    assert "report_age" in verified.report_issues(report, CONFIG, at_limit + timedelta(seconds=1))


def test_document_age_uses_utc_date(payload: dict[str, Any]) -> None:
    """Keep date-only documents within their limit until UTC midnight."""
    report = verified.parse_report(payload, CONFIG, NOW)
    local_time = datetime(2026, 11, 24, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    assert "loan_stale:GigFi (Warehouse Loans)" not in verified.report_issues(report, CONFIG, local_time)


def _response(status: int, content_type: str, body: bytes) -> requests.Response:
    """Build a response with real requests decoding."""
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response._content = body
    return response


def test_fetch_by_configured_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the slug for both endpoint and identity validation."""
    config = verified.VerifiedFeedConfig(slug="another-protocol", report_max_age=timedelta(hours=3))
    body = FIXTURE_PATH.read_bytes().replace(b'"projectId": "3jane"', b'"projectId": "another-protocol"')
    request = Mock(return_value=_response(200, "application/json; charset=utf-8", body))
    monkeypatch.setattr(verified, "request_with_retry", request)
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(verified, "datetime", clock)

    report = verified.fetch_report(config)

    assert report.project_id == "another-protocol"
    assert request.call_args.args == ("get", "https://defillama.com/api/public/verified/another-protocol")
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.kwargs["headers"] == {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:160.0) Gecko/20100101 Firefox/160.0",
    }


@pytest.mark.parametrize(
    ("status", "content_type", "body", "reason"),
    [
        (307, "application/json", b"{}", "HTTP 307 redirect"),
        (200, "text/html; charset=utf-8", b"<html>placeholder</html>", "non-JSON content type"),
        (200, "application/json", b"<html>placeholder</html>", "invalid JSON"),
        (200, "application/json", b'{"projectId":"3jane","projectId":"noon"}', "duplicate JSON key"),
    ],
)
def test_fetch_unavailable_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content_type: str,
    body: bytes,
    reason: str,
) -> None:
    """Reject redirects, HTML fallbacks, and invalid JSON."""
    request = Mock(return_value=_response(status, content_type, body))
    monkeypatch.setattr(verified, "request_with_retry", request)

    with pytest.raises(verified.DefiLlamaVerifiedError, match=reason):
        verified.fetch_report(CONFIG)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (requests.Timeout(), "Timeout"),
        (requests.HTTPError(), "HTTP request failed"),
    ],
)
def test_transport_failure(monkeypatch: pytest.MonkeyPatch, error: requests.RequestException, reason: str) -> None:
    """Translate requests exceptions to client errors understood by a monitor."""
    monkeypatch.setattr(verified, "request_with_retry", Mock(side_effect=error))
    with pytest.raises(verified.DefiLlamaVerifiedError, match=reason):
        verified.fetch_report(CONFIG)


def test_cloudflare_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinguish a Cloudflare challenge from other HTTP 403 responses."""
    response = _response(403, "text/html", b"<html>challenge</html>")
    response.headers["cf-mitigated"] = "challenge"
    monkeypatch.setattr(verified, "request_with_retry", Mock(side_effect=requests.HTTPError(response=response)))

    with pytest.raises(verified.DefiLlamaVerifiedError, match=r"HTTP 403 \(Cloudflare challenge\)"):
        verified.fetch_report(CONFIG)


def test_invalid_endpoint_is_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require HTTPS for a configured endpoint override."""
    config = verified.VerifiedFeedConfig(
        slug="3jane", report_max_age=timedelta(hours=3), endpoint_url="http://example.com/3jane"
    )
    request = Mock()
    monkeypatch.setattr(verified, "request_with_retry", request)
    with pytest.raises(verified.DefiLlamaVerifiedError, match="HTTPS"):
        verified.fetch_report(config)
    request.assert_not_called()
