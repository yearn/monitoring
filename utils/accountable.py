"""Accountable Proof of Solvency feed client.

Accountable publishes per-protocol Proof of Solvency dashboards backed by TEE
attestations. Each dashboard exposes its full report as public JSON, which this
module fetches, validates, and converts into a typed report.

The dashboard request is URL/type-based: it neither sends nor echoes the feed id
(DFID), so feed identity is bound explicitly through :class:`AccountableFeedConfig`.

Validation is deliberately strict. The reported ``collateralization`` is rounded
to six decimals server-side, so it is only used as a cross-check; the ratio used
for alerting is recomputed from the underlying totals at full precision.

See https://docs.accountable.capital/accountable-documentation/proof-of-solvency
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import requests

from utils.http_client import request_with_retry
from utils.logger import get_logger

logger = get_logger("utils.accountable")

MS_PER_SECOND = 1000
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86_400

# The reported ratio is rounded to 6 decimals, so a cross-check can never be
# tighter than 5e-7. Doubled to leave room for float noise in the totals.
RATIO_CONSISTENCY_TOLERANCE = Decimal("2e-6")
# Absolute floor for the net cross-check, scaled up for large books.
NET_CONSISTENCY_ABSOLUTE_TOLERANCE = Decimal("1")
NET_CONSISTENCY_RELATIVE_TOLERANCE = Decimal("1e-6")

# Reserves/supply below this are treated as implausible rather than merely small:
# a zero or negative book cannot produce a meaningful solvency ratio.
MIN_PLAUSIBLE_TOTAL = Decimal("1")
# A ratio outside this range indicates a schema or units problem, not solvency.
MIN_PLAUSIBLE_RATIO = Decimal("0.01")
MAX_PLAUSIBLE_RATIO = Decimal("100")
# Reject reports timestamped meaningfully in the future (clock skew allowance).
MAX_FUTURE_SKEW_SECONDS = 15 * 60

# How long after a source's own declared cadence it is still considered fresh.
# Keyed by the source's ``type``. "Document Report" sources are manual uploads
# that routinely run past their nominal frequency, so they get a wide grace;
# on-chain sources are automated and should be near-realtime.
DEFAULT_SOURCE_GRACE_SECONDS = 2 * SECONDS_PER_HOUR
SOURCE_TYPE_GRACE_SECONDS: dict[str, int] = {
    "Document Report": 7 * SECONDS_PER_DAY,
}

# Declared cadence strings observed on Accountable dashboards, in seconds.
_NAMED_FREQUENCIES: dict[str, int] = {
    "LIVE": 15 * 60,
    "REALTIME": 15 * 60,
    "HOURLY": SECONDS_PER_HOUR,
    "DAILY": SECONDS_PER_DAY,
    "WEEKLY": 7 * SECONDS_PER_DAY,
    "MONTHLY": 30 * SECONDS_PER_DAY,
}
_FREQUENCY_UNIT_SECONDS: dict[str, int] = {
    "MIN": 60,
    "MINS": 60,
    "MINUTE": 60,
    "MINUTES": 60,
    "H": SECONDS_PER_HOUR,
    "HR": SECONDS_PER_HOUR,
    "HOUR": SECONDS_PER_HOUR,
    "HOURS": SECONDS_PER_HOUR,
    "D": SECONDS_PER_DAY,
    "DAY": SECONDS_PER_DAY,
    "DAYS": SECONDS_PER_DAY,
    "W": 7 * SECONDS_PER_DAY,
    "WEEK": 7 * SECONDS_PER_DAY,
    "WEEKS": 7 * SECONDS_PER_DAY,
    # Deliberately no bare "M": it reads as either minutes or months, and
    # guessing months would silently hand a source a 30x freshness budget.
    "MONTH": 30 * SECONDS_PER_DAY,
    "MONTHS": 30 * SECONDS_PER_DAY,
}


class AccountableError(Exception):
    """Raised when an Accountable report cannot be parsed or fails validation."""


class AccountableStatus(Enum):
    """Outcome of an Accountable feed retrieval.

    ``UNAVAILABLE`` covers every "do not trust this number" case — network
    failure, non-200, schema violation, or a failed consistency check. ``STALE``
    means the report parsed and is self-consistent, but the aggregate report or
    a required source is older than its cadence allows.
    """

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"


@dataclass(frozen=True)
class AccountableFeedConfig:
    """Binds a feed id to the dashboard that serves it.

    Args:
        dfid: Accountable data feed id, e.g. ``"100000026"``.
        dashboard_url: Public JSON endpoint for the report.
        message_url: Public URL for the dashboard, used in alerts.
        dashboard_type: Dashboard type the endpoint serves
        required_sources: Source names that must carry usable freshness metadata.
        max_report_age_seconds: Aggregate report age beyond which it is stale.
    """

    dfid: str
    dashboard_url: str
    message_url: str
    dashboard_type: str
    required_sources: tuple[str, ...] = ()
    max_report_age_seconds: int = 6 * SECONDS_PER_HOUR


@dataclass(frozen=True)
class DataSourceSnapshot:
    """One upstream data source feeding a Proof of Solvency report."""

    name: str
    source_type: str
    frequency: str
    last_updated_ms: int
    age_seconds: int
    max_age_seconds: int

    @property
    def is_stale(self) -> bool:
        """Whether the source is older than its cadence plus grace."""
        return self.age_seconds > self.max_age_seconds


@dataclass(frozen=True)
class AccountableReport:
    """A validated Proof of Solvency report.

    ``collateralization`` is recomputed from ``total_reserves / total_supply``
    at full precision. ``reported_collateralization`` is the (rounded) value the
    API returned, retained for cross-checking and display.

    ``source_problems`` describes required sources whose freshness could not be
    established. The ratio is still trustworthy in that case, so the report is
    returned as ``STALE`` rather than withheld.
    """

    dfid: str
    collateralization: Decimal
    reported_collateralization: Decimal
    net: Decimal
    total_reserves: Decimal
    total_supply: Decimal
    verifiability: Decimal
    ts_ms: int
    report_age_seconds: int
    sources: tuple[DataSourceSnapshot, ...]
    source_problems: tuple[str, ...] = ()

    @property
    def stale_sources(self) -> tuple[DataSourceSnapshot, ...]:
        """Sources older than their declared cadence plus grace."""
        return tuple(source for source in self.sources if source.is_stale)

    @property
    def report_timestamp(self) -> datetime:
        """Report timestamp as a timezone-aware UTC datetime."""
        return datetime.fromtimestamp(self.ts_ms / MS_PER_SECOND, tz=timezone.utc)


@dataclass(frozen=True)
class AccountableFetchResult:
    """Result of fetching a feed: always a status, never a silent ``None``.

    A ``STALE`` result still carries its report so callers can log the values
    while alerting on the staleness.
    """

    status: AccountableStatus
    report: AccountableReport | None
    reason: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status is AccountableStatus.OK


def _coerce_decimal(value: Any, field_name: str) -> Decimal:
    """Convert an API value to Decimal, accepting numeric strings.

    The live payload types several documented numbers as strings (``ts``,
    ``verifiability``, ``timeline[].point``), so strict type checks would reject
    valid data.

    Raises:
        AccountableError: If the value is missing, non-numeric, or not finite.
    """
    if isinstance(value, bool) or value is None:
        raise AccountableError(f"{field_name} is not numeric: {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise AccountableError(f"{field_name} is not finite: {value!r}")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AccountableError(f"{field_name} is not numeric: {value!r}") from exc
    if not result.is_finite():
        raise AccountableError(f"{field_name} is not finite: {value!r}")
    return result


def _coerce_int(value: Any, field_name: str) -> int:
    """Convert an API value to int, accepting numeric strings."""
    return int(_coerce_decimal(value, field_name))


def _require_mapping(payload: Any, field_name: str) -> dict[str, Any]:
    """Return ``payload`` as a dict or raise."""
    if not isinstance(payload, dict):
        raise AccountableError(f"{field_name} is missing or not an object")
    return payload


def _require_value(container: dict[str, Any], key: str) -> Decimal:
    """Read ``container[key]['value']`` as a Decimal."""
    entry = _require_mapping(container.get(key), key)
    if "value" not in entry:
        raise AccountableError(f"{key}.value is missing")
    return _coerce_decimal(entry["value"], f"{key}.value")


def parse_frequency_seconds(frequency: Any) -> int | None:
    """Parse a declared source cadence into seconds.

    Handles named cadences (``"DAILY"``, ``"WEEKLY"``, ``"live"``) and
    quantity/unit forms (``"15 MIN"``, ``"48 H"``, ``"1 W"``).

    Returns:
        Cadence in seconds, or None when the format is unrecognised.
    """
    if not isinstance(frequency, str):
        return None
    normalized = frequency.strip().upper()
    if not normalized:
        return None
    if normalized in _NAMED_FREQUENCIES:
        return _NAMED_FREQUENCIES[normalized]

    parts = normalized.split()
    if len(parts) == 2:
        quantity_text, unit = parts
    else:
        # Accept compact forms such as "48H".
        digits = "".join(ch for ch in normalized if ch.isdigit())
        unit = normalized[len(digits) :].strip()
        quantity_text = digits
    if not quantity_text or unit not in _FREQUENCY_UNIT_SECONDS:
        return None
    try:
        quantity = int(quantity_text)
    except ValueError:
        return None
    if quantity <= 0:
        return None
    return quantity * _FREQUENCY_UNIT_SECONDS[unit]


def _parse_data_sources(
    payload: Any,
    now_ms: int,
    required_sources: tuple[str, ...] = (),
) -> tuple[tuple[DataSourceSnapshot, ...], tuple[str, ...]]:
    """Build source snapshots with per-source-type staleness budgets.

    Unknown sources with an unrecognised cadence or missing timestamp are
    skipped, so an Accountable schema addition cannot spuriously page us. A
    configured required source that is missing or unparseable is recorded as a
    problem rather than raised: freshness can no longer be established, but the
    collateral ratio itself is unaffected and must still be evaluated. A source
    rename upstream degrades the feed to ``STALE``, it does not blind the
    sub-100% check.

    Returns:
        The parsed snapshots, and descriptions of any required-source problems.
    """
    if not isinstance(payload, dict):
        if required_sources:
            return (), ("dataSources is missing or not an object",)
        return (), ()

    required = set(required_sources)
    problems: list[str] = []
    missing = sorted(required.difference(payload))
    if missing:
        problems.append(f"required dataSources are missing: {', '.join(missing)}")

    snapshots: list[DataSourceSnapshot] = []
    for name, entry in payload.items():
        is_required = name in required
        if not isinstance(entry, dict):
            if is_required:
                problems.append(f"dataSources.{name} is not an object")
            continue
        cadence_seconds = parse_frequency_seconds(entry.get("frequency"))
        if cadence_seconds is None:
            if is_required:
                problems.append(f"dataSources.{name}.frequency is not recognised: {entry.get('frequency')!r}")
            else:
                logger.debug("Accountable source %s has unparseable frequency %r", name, entry.get("frequency"))
            continue
        try:
            last_updated_ms = _coerce_int(entry.get("lastUpdated"), f"dataSources.{name}.lastUpdated")
        except AccountableError as exc:
            if is_required:
                problems.append(str(exc))
            else:
                logger.debug("Accountable source %s has no usable lastUpdated", name)
            continue

        age_seconds = (now_ms - last_updated_ms) // MS_PER_SECOND
        if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
            # Clamping a future timestamp to age 0 would make a source with a
            # broken clock look permanently fresh, which is the one thing the
            # freshness check exists to catch.
            if is_required:
                problems.append(f"dataSources.{name}.lastUpdated is {-age_seconds}s in the future")
            else:
                logger.debug("Accountable source %s has a future lastUpdated", name)
            continue

        source_type_value = entry.get("type")
        if not isinstance(source_type_value, str) or not source_type_value.strip():
            if is_required:
                problems.append(f"dataSources.{name}.type is missing or not a string")
                continue
            source_type = ""
        else:
            source_type = source_type_value
        grace = SOURCE_TYPE_GRACE_SECONDS.get(source_type, DEFAULT_SOURCE_GRACE_SECONDS)
        snapshots.append(
            DataSourceSnapshot(
                name=str(name),
                source_type=source_type,
                frequency=str(entry.get("frequency") or ""),
                last_updated_ms=last_updated_ms,
                age_seconds=max(0, age_seconds),
                max_age_seconds=cadence_seconds + grace,
            )
        )
    return tuple(snapshots), tuple(problems)


def _validate_consistency(
    collateralization: Decimal,
    reported: Decimal,
    net: Decimal,
    total_reserves: Decimal,
    total_supply: Decimal,
) -> None:
    """Cross-check the reported ratio and net against the underlying totals.

    Raises:
        AccountableError: If either identity fails its tolerance.
    """
    if abs(collateralization - reported) > RATIO_CONSISTENCY_TOLERANCE:
        raise AccountableError(
            f"collateralization {reported} disagrees with total_reserves/total_supply {collateralization}"
        )

    net_tolerance = max(
        NET_CONSISTENCY_ABSOLUTE_TOLERANCE,
        NET_CONSISTENCY_RELATIVE_TOLERANCE * abs(total_reserves),
    )
    expected_net = total_reserves - total_supply
    if abs(net - expected_net) > net_tolerance:
        raise AccountableError(f"net {net} disagrees with total_reserves - total_supply {expected_net}")


def _validate_usd_supply(supply_entry: dict[str, Any]) -> None:
    """Establish that raw supply is the USD liability denominator.

    Accountable documents ``total_supply.fx`` as 1 for USD-pegged feeds, but the
    live 3Jane response currently omits the field. When present, require it to be
    exactly 1.

    When absent, the invariant is already enforced by the net cross-check in
    :func:`_validate_consistency`: the server computes ``net`` against
    liabilities, so ``net ≈ total_reserves - total_supply`` holds only when
    liabilities equal raw supply, i.e. when fx is 1. A non-pegged feed fails
    there instead, on the same tolerance.
    """
    if "fx" not in supply_entry:
        return
    fx = _coerce_decimal(supply_entry["fx"], "total_supply.fx")
    if fx != 1:
        raise AccountableError(f"total_supply.fx is {fx}, expected 1 (non-USD-pegged feed is unsupported)")


def parse_report(payload: Any, config: AccountableFeedConfig, now_ms: int) -> AccountableReport:
    """Validate a raw dashboard payload into an :class:`AccountableReport`.

    Args:
        payload: Decoded JSON body from the dashboard endpoint.
        config: Feed configuration supplying the DFID to bind the report to.
        now_ms: Current time in milliseconds, for age calculations.

    Raises:
        AccountableError: On any schema, type, range, or consistency violation.
    """
    body = _require_mapping(payload, "response")
    if body.get("res") != "ok":
        raise AccountableError(f"response res is {body.get('res')!r}, expected 'ok'")

    data = _require_mapping(body.get("data"), "data")
    reserves = _require_mapping(data.get("reserves"), "data.reserves")

    total_reserves = _require_value(reserves, "total_reserves")
    total_supply = _require_value(reserves, "total_supply")
    if total_reserves < MIN_PLAUSIBLE_TOTAL or total_supply < MIN_PLAUSIBLE_TOTAL:
        raise AccountableError(f"implausible totals: reserves={total_reserves}, supply={total_supply}")

    supply_entry = _require_mapping(reserves.get("total_supply"), "total_supply")
    reported_collateralization = _coerce_decimal(data.get("collateralization"), "collateralization")
    net = _coerce_decimal(data.get("net"), "net")

    # The docs define collateralization and net against liabilities, which equal
    # raw total_supply only for a USD-pegged feed. Validate the explicit fx when
    # available; the net cross-check below enforces the same invariant when it
    # is absent, as it is on the live 3Jane response.
    _validate_usd_supply(supply_entry)

    collateralization = total_reserves / total_supply
    if not MIN_PLAUSIBLE_RATIO <= collateralization <= MAX_PLAUSIBLE_RATIO:
        raise AccountableError(f"implausible collateralization ratio: {collateralization}")

    _validate_consistency(collateralization, reported_collateralization, net, total_reserves, total_supply)

    ts_ms = _coerce_int(data.get("ts"), "ts")
    age_seconds = (now_ms - ts_ms) // MS_PER_SECOND
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
        raise AccountableError(f"report timestamp is {-age_seconds}s in the future")

    sources, source_problems = _parse_data_sources(data.get("dataSources"), now_ms, config.required_sources)

    return AccountableReport(
        dfid=config.dfid,
        collateralization=collateralization,
        reported_collateralization=reported_collateralization,
        net=net,
        total_reserves=total_reserves,
        total_supply=total_supply,
        verifiability=_coerce_decimal(reserves.get("verifiability"), "reserves.verifiability"),
        ts_ms=ts_ms,
        report_age_seconds=max(0, age_seconds),
        sources=sources,
        source_problems=source_problems,
    )


def evaluate_report(report: AccountableReport, config: AccountableFeedConfig) -> AccountableFetchResult:
    """Classify a parsed report as OK or STALE.

    Staleness covers the aggregate report age, any individual source that has
    outrun its own cadence plus grace, and any required source whose freshness
    could not be established at all.
    """
    if report.report_age_seconds > config.max_report_age_seconds:
        return AccountableFetchResult(
            AccountableStatus.STALE,
            report,
            f"report is {report.report_age_seconds // SECONDS_PER_HOUR}h old",
        )

    if report.source_problems:
        return AccountableFetchResult(
            AccountableStatus.STALE,
            report,
            f"unusable source freshness metadata: {'; '.join(report.source_problems)}",
        )

    stale = report.stale_sources
    if stale:
        detail = ", ".join(f"{source.name} ({source.age_seconds // SECONDS_PER_HOUR}h)" for source in stale)
        return AccountableFetchResult(AccountableStatus.STALE, report, f"stale sources: {detail}")

    return AccountableFetchResult(AccountableStatus.OK, report)


def fetch_report(config: AccountableFeedConfig, now_ms: int | None = None) -> AccountableFetchResult:
    """Fetch, validate, and classify a Proof of Solvency report.

    Network and schema failures are converted into an ``UNAVAILABLE`` result
    rather than raised, so a feed outage cannot interrupt a caller's other
    checks. Retries follow ``utils.http_client`` (transient 5xx/timeouts only).

    Args:
        config: Feed to fetch.
        now_ms: Current time in milliseconds; defaults to wall clock.

    Returns:
        A result carrying the status and, when parseable, the report.
    """
    if now_ms is None:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * MS_PER_SECOND)

    # The URL is overridable by env, and the feed is authenticated by TLS alone
    # in v1 (no signature verification yet), so plaintext is not acceptable.
    if not config.dashboard_url.lower().startswith("https://"):
        logger.error("Accountable feed %s has a non-HTTPS URL: %s", config.dfid, config.dashboard_url)
        return AccountableFetchResult(AccountableStatus.UNAVAILABLE, None, "dashboard URL is not HTTPS")

    try:
        response = request_with_retry("get", config.dashboard_url, headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        logger.warning("Accountable feed %s unreachable: %s", config.dfid, exc)
        return AccountableFetchResult(AccountableStatus.UNAVAILABLE, None, f"request failed: {exc}")

    # Kept out of the block above: requests raises JSONDecodeError, which is
    # itself a RequestException, so a shared handler would report a decode
    # failure as a network failure.
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("Accountable feed %s returned non-JSON: %s", config.dfid, exc)
        return AccountableFetchResult(AccountableStatus.UNAVAILABLE, None, f"invalid JSON: {exc}")

    try:
        report = parse_report(payload, config, now_ms)
    except AccountableError as exc:
        logger.warning("Accountable feed %s failed validation: %s", config.dfid, exc)
        return AccountableFetchResult(AccountableStatus.UNAVAILABLE, None, str(exc))

    return evaluate_report(report, config)
