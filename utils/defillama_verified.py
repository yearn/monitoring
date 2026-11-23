"""Client for DefiLlama Verified loan records at /api/public/verified/{slug}."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import requests

from utils.formatting import format_age
from utils.http_client import request_with_retry

FIREFOX_USER_AGENT = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:160.0) Gecko/20100101 Firefox/160.0"
MAX_CLOCK_SKEW = timedelta(minutes=5)
PRINCIPAL_TOTAL_TOLERANCE = Decimal("0.01")

type SourceKey = tuple[str, str]


class DefiLlamaVerifiedError(Exception):
    """The DefiLlama Verified response could not be retrieved or validated."""


@dataclass(frozen=True)
class VerifiedFeedConfig:
    """Identify a loan feed and the maximum ages for its required sources."""

    slug: str
    report_max_age: timedelta
    loan_max_ages: Mapping[SourceKey, timedelta] = field(default_factory=dict)
    balance_max_ages: Mapping[SourceKey, timedelta] = field(default_factory=dict)
    endpoint_url: str | None = None

    def __post_init__(self) -> None:
        """Validate the slug used to construct the URLs and bind the record."""
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.slug):
            raise DefiLlamaVerifiedError("invalid protocol slug")

    @property
    def api_url(self) -> str:
        """Return the configured JSON endpoint or DefiLlama's public route."""
        return self.endpoint_url or f"https://defillama.com/api/public/verified/{self.slug}"

    @property
    def dashboard_url(self) -> str:
        """Return the human-readable verification record."""
        return f"https://defillama.com/verified/{self.slug}"


@dataclass(frozen=True)
class VerifiedLoan:
    """Outstanding principal and document date for one loan originator."""

    principal_usd: Decimal
    report_date: date
    collateral_usd: Decimal | None


@dataclass(frozen=True)
class VerifiedBalance:
    """Balance and source timestamp for one reported cash position."""

    balance_usd: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class VerifiedLoanReport:
    """DefiLlama's published loan record, bound to a configured protocol."""

    project_id: str
    up_to_date: bool
    timestamp: datetime
    total_principal_usd: Decimal
    loans: Mapping[SourceKey, VerifiedLoan]
    balances: Mapping[SourceKey, VerifiedBalance]


def _object(value: Any, path: str) -> dict[str, Any]:
    """Require a JSON object at the given path."""
    if not isinstance(value, dict):
        raise DefiLlamaVerifiedError(f"{path} must be an object")
    return value


def _amount(value: Any, path: str) -> Decimal:
    """Require a finite, nonnegative JSON number for a USD amount."""
    if type(value) not in (int, float, Decimal):
        raise DefiLlamaVerifiedError(f"{path} must be a number")
    amount = Decimal(str(value))
    if not amount.is_finite() or not math.isfinite(amount) or amount < 0:
        raise DefiLlamaVerifiedError(f"{path} must be a finite, nonnegative number")
    return amount


def _timestamp(value: Any, path: str, latest: datetime) -> datetime:
    """Require a positive Unix timestamp in seconds within clock skew."""
    if type(value) is not int or not 0 < value <= (latest + MAX_CLOCK_SKEW).timestamp():
        raise DefiLlamaVerifiedError(f"{path} must be a positive Unix timestamp in seconds within clock skew")
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _report_date(value: Any, path: str, latest: date) -> date:
    """Require a source date in YYYY-MM-DD format."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DefiLlamaVerifiedError(f"{path} must be a YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DefiLlamaVerifiedError(f"{path} must be a valid calendar date") from exc
    if parsed > latest:
        raise DefiLlamaVerifiedError(f"{path} is in the future")
    return parsed


def _source_rows(value: Any, path: str) -> dict[SourceKey, dict[str, Any]]:
    """Validate the two-level category and source breakdown."""
    rows = {}
    for category, sources in _object(value, path).items():
        if not isinstance(category, str) or not category.strip():
            raise DefiLlamaVerifiedError(f"{path} has an invalid category")
        for name, entry in _object(sources, f"{path}.{category}").items():
            if not isinstance(name, str) or not name.strip():
                raise DefiLlamaVerifiedError(f"{path}.{category} has an invalid source name")
            rows[category, name] = _object(entry, f"{path}.{category}.{name}")
    return rows


def parse_report(payload: Any, config: VerifiedFeedConfig, now: datetime) -> VerifiedLoanReport:
    """Validate identity, amounts, and timestamps for a Verified loan record.

    Pass a timezone-aware current time as ``now``. The reported total is cross-checked against the published originators, with
    a cent of tolerance for floating-point noise. Cash and eligible collateral
    are separate figures.
    """
    body = _object(payload, "response")
    if body.get("projectId") != config.slug:
        raise DefiLlamaVerifiedError(f"projectId does not match configured slug {config.slug}")
    state = _object(body.get("state"), "state")
    up_to_date = state.get("upToDate")
    if not isinstance(up_to_date, bool):
        raise DefiLlamaVerifiedError("state.upToDate must be a boolean")

    data = _object(body.get("data"), "data")
    timestamp = _timestamp(data.get("timestampOnRun"), "data.timestampOnRun", now)
    latest_source_date = (timestamp + MAX_CLOCK_SKEW).date()
    loans = {}
    for key, entry in _source_rows(data.get("loans"), "data.loans").items():
        path = f"data.loans.{'.'.join(key)}"
        collateral = entry.get("collateralUsd")
        loans[key] = VerifiedLoan(
            principal_usd=_amount(entry.get("principalUsd"), f"{path}.principalUsd"),
            report_date=_report_date(entry.get("reportDate"), f"{path}.reportDate", latest_source_date),
            collateral_usd=None if collateral is None else _amount(collateral, f"{path}.collateralUsd"),
        )

    balances = {}
    for key, entry in _source_rows(data.get("balances", {}), "data.balances").items():
        path = f"data.balances.{'.'.join(key)}"
        balances[key] = VerifiedBalance(
            balance_usd=_amount(entry.get("balanceUsd"), f"{path}.balanceUsd"),
            timestamp=_timestamp(entry.get("timestamp"), f"{path}.timestamp", min(now, timestamp)),
        )

    total = sum((loan.principal_usd for loan in loans.values()), Decimal(0))
    reported_total = _amount(data.get("totalPrincipalUsd"), "data.totalPrincipalUsd")
    if abs(total - reported_total) > PRINCIPAL_TOTAL_TOLERANCE:
        raise DefiLlamaVerifiedError("data.totalPrincipalUsd disagrees with the sum of originator principal")

    return VerifiedLoanReport(config.slug, up_to_date, timestamp, total, loans, balances)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before they can overwrite a published value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise DefiLlamaVerifiedError("response contains a duplicate JSON key")
        result[key] = value
    return result


def fetch_report(config: VerifiedFeedConfig) -> VerifiedLoanReport:
    """Fetch the configured loan record and raise on redirects or unusable JSON."""
    try:
        endpoint = urlsplit(config.api_url)
    except ValueError as exc:
        raise DefiLlamaVerifiedError("invalid endpoint URL") from exc
    if endpoint.scheme != "https" or not endpoint.hostname:
        raise DefiLlamaVerifiedError("endpoint must be an HTTPS URL")

    try:
        response = request_with_retry(
            "get",
            config.api_url,
            headers={"Accept": "application/json", "User-Agent": FIREFOX_USER_AGENT},
            allow_redirects=False,
            timeout=10,
            retries=1,
            log_errors=False,
        )
    except requests.HTTPError as exc:
        if exc.response is not None:
            reason = f"HTTP {exc.response.status_code}"
            if exc.response.headers.get("cf-mitigated") == "challenge":
                reason += " (Cloudflare challenge)"
        else:
            reason = "HTTP request failed"
        raise DefiLlamaVerifiedError(reason) from exc
    except requests.RequestException as exc:
        raise DefiLlamaVerifiedError(f"request failed ({type(exc).__name__})") from exc

    if 300 <= response.status_code < 400:
        raise DefiLlamaVerifiedError(f"unexpected HTTP {response.status_code} redirect or cache response")
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json" and not (
        content_type.startswith("application/") and content_type.endswith("+json")
    ):
        raise DefiLlamaVerifiedError("endpoint returned a non-JSON content type")
    try:
        payload = response.json(parse_float=Decimal, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise DefiLlamaVerifiedError("endpoint returned invalid JSON") from exc
    return parse_report(payload, config, datetime.now(tz=timezone.utc))


def report_issues(report: VerifiedLoanReport, config: VerifiedFeedConfig, now: datetime) -> dict[str, str]:
    """Return stable identifiers and descriptions for stale or missing sources."""
    issues = {}
    if not report.up_to_date:
        issues["provider_state"] = "DefiLlama marks this record as out of date"

    age = now - report.timestamp
    if age > config.report_max_age:
        issues["report_age"] = (
            f"Last verification is {format_age(int(age.total_seconds()))} old "
            f"(limit {format_age(int(config.report_max_age.total_seconds()))})"
        )
    today_utc = now.astimezone(timezone.utc).date()
    for key, max_age in config.loan_max_ages.items():
        category, name = key
        loan = report.loans.get(key)
        label = f"{name} ({category})"
        if loan is None:
            issues[f"loan_missing:{label}"] = f"Required originator missing: {label}"
        elif today_utc - loan.report_date > max_age:
            issues[f"loan_stale:{label}"] = (
                f"{label} document dated {loan.report_date:%Y-%m-%d} UTC is older than {max_age.days} calendar days"
            )
    for key, max_age in config.balance_max_ages.items():
        category, name = key
        balance = report.balances.get(key)
        label = f"{name} ({category})"
        if balance is None:
            issues[f"balance_missing:{label}"] = f"Required balance source missing: {label}"
        elif now - balance.timestamp > max_age:
            issues[f"balance_stale:{label}"] = (
                f"{label} balance is {format_age(int((now - balance.timestamp).total_seconds()))} old "
                f"(limit {format_age(int(max_age.total_seconds()))})"
            )
    return issues
