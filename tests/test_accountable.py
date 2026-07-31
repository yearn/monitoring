"""Tests for the Accountable Proof of Solvency client."""

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from utils import accountable
from utils.accountable import (
    AccountableError,
    AccountableFeedConfig,
    AccountableStatus,
    evaluate_report,
    fetch_report,
    parse_frequency_seconds,
    parse_report,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "accountable_3jane_dashboard.json"
# Fixture was recorded at ts=1785490814726; this is a few minutes later.
FIXTURE_NOW_MS = 1785491000000

CONFIG = AccountableFeedConfig(
    dfid="100000026",
    dashboard_url="https://accountable.3jane.xyz/dashboard",
    dashboard_type="three-jane",
)


def load_payload() -> dict[str, Any]:
    """Return a mutable copy of the recorded dashboard response."""
    return json.loads(FIXTURE_PATH.read_text())


# --- Parsing the real recorded payload ---


def test_parses_recorded_live_payload() -> None:
    report = parse_report(load_payload(), CONFIG, FIXTURE_NOW_MS)

    assert report.dfid == "100000026"
    assert report.total_reserves == Decimal("75021555.53")
    assert report.total_supply == Decimal("74999990.44")
    assert report.net == Decimal("21565.08")
    assert report.verifiability == Decimal("100")
    assert report.ts_ms == 1785490814726


def test_ratio_is_derived_at_full_precision_not_taken_from_rounded_field() -> None:
    report = parse_report(load_payload(), CONFIG, FIXTURE_NOW_MS)

    assert report.reported_collateralization == Decimal("1.000288")
    assert report.collateralization == Decimal("75021555.53") / Decimal("74999990.44")
    # The derived value carries precision the rounded API field discards.
    assert report.collateralization != report.reported_collateralization


def test_coerces_numeric_strings() -> None:
    """ts and verifiability ship as strings despite being documented as numbers."""
    payload = load_payload()
    assert isinstance(payload["data"]["ts"], str)
    assert isinstance(payload["data"]["reserves"]["verifiability"], str)

    report = parse_report(payload, CONFIG, FIXTURE_NOW_MS)

    assert isinstance(report.ts_ms, int)
    assert report.verifiability == Decimal("100")


def test_live_payload_is_not_stale() -> None:
    """Document Report sources lag their cadence; per-type grace must absorb that."""
    result = evaluate_report(parse_report(load_payload(), CONFIG, FIXTURE_NOW_MS), CONFIG)

    assert result.status is AccountableStatus.OK
    assert result.report is not None
    assert result.report.stale_sources == ()


# --- Rejection cases ---


def test_rejects_non_ok_res() -> None:
    payload = load_payload()
    payload["res"] = "error"

    with pytest.raises(AccountableError, match="res"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_missing_reserves() -> None:
    payload = load_payload()
    del payload["data"]["reserves"]

    with pytest.raises(AccountableError, match="reserves"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_missing_total_supply_value() -> None:
    payload = load_payload()
    del payload["data"]["reserves"]["total_supply"]["value"]

    with pytest.raises(AccountableError, match="total_supply.value"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_non_numeric_field() -> None:
    payload = load_payload()
    payload["data"]["reserves"]["total_reserves"]["value"] = "not-a-number"

    with pytest.raises(AccountableError, match="not numeric"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_non_finite_value() -> None:
    payload = load_payload()
    payload["data"]["reserves"]["total_reserves"]["value"] = float("inf")

    with pytest.raises(AccountableError, match="not finite"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_zero_supply_instead_of_dividing_by_zero() -> None:
    payload = load_payload()
    payload["data"]["reserves"]["total_supply"]["value"] = 0

    with pytest.raises(AccountableError, match="implausible totals"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_inconsistent_collateralization() -> None:
    """A reported ratio that disagrees with the totals means we cannot trust either."""
    payload = load_payload()
    payload["data"]["collateralization"] = 1.5

    with pytest.raises(AccountableError, match="disagrees"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_inconsistent_net() -> None:
    payload = load_payload()
    payload["data"]["net"] = 999_999.0

    with pytest.raises(AccountableError, match="net"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_accepts_rounding_level_disagreement() -> None:
    """The API rounds collateralization to 6dp, so tolerance must absorb that."""
    payload = load_payload()
    reserves = Decimal(str(payload["data"]["reserves"]["total_reserves"]["value"]))
    supply = Decimal(str(payload["data"]["reserves"]["total_supply"]["value"]))
    payload["data"]["collateralization"] = float(round(reserves / supply, 6))

    report = parse_report(payload, CONFIG, FIXTURE_NOW_MS)

    assert report.collateralization == reserves / supply


def test_rejects_non_usd_pegged_feed() -> None:
    """liabilities == total_supply only holds when fx is 1."""
    payload = load_payload()
    payload["data"]["reserves"]["total_supply"]["fx"] = 0.92

    with pytest.raises(AccountableError, match="fx"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


def test_rejects_future_timestamp() -> None:
    payload = load_payload()
    payload["data"]["ts"] = str(FIXTURE_NOW_MS + 86_400_000)

    with pytest.raises(AccountableError, match="future"):
        parse_report(payload, CONFIG, FIXTURE_NOW_MS)


# --- Freshness ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15 MIN", 900),
        ("DAILY", 86_400),
        ("WEEKLY", 604_800),
        ("48 H", 172_800),
        ("1 W", 604_800),
        ("live", 900),
        ("", None),
        ("sometimes", None),
        (None, None),
    ],
)
def test_parse_frequency_seconds(text: Any, expected: int | None) -> None:
    assert parse_frequency_seconds(text) == expected


def test_stale_aggregate_report_is_detected() -> None:
    payload = load_payload()
    late_ms = FIXTURE_NOW_MS + (CONFIG.max_report_age_seconds + 3600) * 1000

    result = evaluate_report(parse_report(payload, CONFIG, late_ms), CONFIG)

    assert result.status is AccountableStatus.STALE
    assert "old" in result.reason


def test_fresh_aggregate_with_stale_source_is_detected() -> None:
    """The headline timestamp can be fresh while an input has gone dark."""
    payload = load_payload()
    payload["data"]["dataSources"]["USD3 On-Chain Reserves"]["lastUpdated"] = str(FIXTURE_NOW_MS - 86_400_000)

    result = evaluate_report(parse_report(payload, CONFIG, FIXTURE_NOW_MS), CONFIG)

    assert result.status is AccountableStatus.STALE
    assert "USD3 On-Chain Reserves" in result.reason
    assert result.report is not None
    assert result.report.collateralization > 1


def test_document_report_grace_is_wider_than_onchain_grace() -> None:
    report = parse_report(load_payload(), CONFIG, FIXTURE_NOW_MS)
    budgets = {source.source_type: source.max_age_seconds for source in report.sources}

    assert budgets["Document Report"] > budgets["ERC4626"]


def test_unparseable_source_frequency_is_skipped_not_flagged_stale() -> None:
    payload = load_payload()
    payload["data"]["dataSources"]["Mystery Source"] = {
        "type": "Unknown",
        "frequency": "whenever",
        "lastUpdated": "1",
    }

    result = evaluate_report(parse_report(payload, CONFIG, FIXTURE_NOW_MS), CONFIG)

    assert result.status is AccountableStatus.OK
    assert result.report is not None
    assert all(source.name != "Mystery Source" for source in result.report.sources)


# --- fetch_report network behaviour ---


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_fetch_report_returns_unavailable_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(accountable, "request_with_retry", boom)

    result = fetch_report(CONFIG, FIXTURE_NOW_MS)

    assert result.status is AccountableStatus.UNAVAILABLE
    assert result.report is None
    assert "refused" in result.reason


def test_fetch_report_returns_unavailable_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        accountable,
        "request_with_retry",
        lambda *_a, **_k: _FakeResponse(ValueError("not json")),
    )

    result = fetch_report(CONFIG, FIXTURE_NOW_MS)

    assert result.status is AccountableStatus.UNAVAILABLE
    assert "invalid JSON" in result.reason


def test_fetch_report_returns_unavailable_on_schema_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = load_payload()
    del payload["data"]["collateralization"]
    monkeypatch.setattr(accountable, "request_with_retry", lambda *_a, **_k: _FakeResponse(payload))

    result = fetch_report(CONFIG, FIXTURE_NOW_MS)

    assert result.status is AccountableStatus.UNAVAILABLE
    assert result.report is None


def test_fetch_report_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accountable, "request_with_retry", lambda *_a, **_k: _FakeResponse(load_payload()))

    result = fetch_report(CONFIG, FIXTURE_NOW_MS)

    assert result.is_ok
    assert result.report is not None
    assert result.report.collateralization > 1


def test_fixture_is_not_mutated_between_tests() -> None:
    """Guards the shared-fixture pattern used throughout this module."""
    first = load_payload()
    second = load_payload()

    assert first == second
    assert first is not second
    assert copy.deepcopy(first) == second
