"""Guard that alerts are stored under a protocol key the website can display.

Regression: timelock alerts were stored under their uppercase Telegram routing key
(`YEARN_TIMELOCK`, `CAP`, ...) while each protocol page queries
`GET /v1/alerts?protocol=<key>` with an exact match, so no timelock alert ever
appeared on https://curation.yearn.fi/monitoring/. The key was wrong, not missing —
`alert_events.protocol` is NOT NULL, so an untagged alert cannot exist. These tests
check the keys instead: a new monitor with an unmatched key fails CI rather than
going quietly missing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from protocols.timelock.timelock_alerts import (
    TIMELOCK_LIST,
    YEARN_TIMELOCK_INTERNAL_PROTOCOL,
    alert_history_protocol,
    process_events,
)
from utils.alert_protocols import (
    PAGELESS_ALERT_PROTOCOLS,
    SLUG_ALERT_OVERRIDES,
    known_alert_protocols,
)
from utils.monitoring_config import load_monitoring_config

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("protocols", "utils")


def _known_keys() -> set[str]:
    return known_alert_protocols(load_monitoring_config().protocols)


def _protocol_constants() -> dict[Path, str]:
    """Return each module-level ``PROTOCOL = "<key>"`` literal, by file."""
    found: dict[Path, str] = {}
    for directory in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
                if "PROTOCOL" in targets and isinstance(node.value, ast.Constant):
                    if isinstance(node.value.value, str):
                        found[path.relative_to(REPO_ROOT)] = node.value.value
    return found


def test_protocol_constants_scan_finds_monitors() -> None:
    """Guard the AST scan itself: a silently empty scan would pass every check below."""
    constants = _protocol_constants()
    assert len(constants) > 20, f"expected the PROTOCOL scan to find most monitors, got {constants}"


def test_protocol_constants_are_displayable() -> None:
    known = _known_keys()
    unknown = {path: key for path, key in _protocol_constants().items() if key not in known}
    assert not unknown, (
        f"PROTOCOL keys that no website page queries: {unknown}. Add the protocol to "
        "monitoring.yaml, map its slug in SLUG_ALERT_OVERRIDES, or list the key in "
        "PAGELESS_ALERT_PROTOCOLS (utils/alert_protocols.py) if it is meant to stay off the site."
    )


def test_timelock_alert_keys_are_displayable() -> None:
    known = _known_keys()
    unknown = {
        t.protocol: alert_history_protocol(t.protocol)
        for t in TIMELOCK_LIST
        if alert_history_protocol(t.protocol) not in known
    }
    assert not unknown, (
        f"timelock routing keys stored under a key no page queries: {unknown}. "
        "Map them in ALERT_HISTORY_PROTOCOLS (protocols/timelock/timelock_alerts.py)."
    )


@patch("protocols.timelock.timelock_alerts.send_telegram_message")
@patch("protocols.timelock.timelock_alerts.build_alert_message", return_value="msg")
def test_process_events_stores_every_timelock_under_a_displayable_key(
    _mock_build: object,
    mock_send: object,
) -> None:
    """End-to-end guard on the wiring: unmapped keys reach the store, not just the map.

    Checking `alert_history_protocol` alone would still pass if the
    `origin_protocol=` argument were dropped, which is the original bug.
    """
    events = [
        {
            "id": f"{t.address}-{t.chain_id}",
            "chainId": str(t.chain_id),
            "timelockAddress": t.address,
            "timelockType": "TimelockController",
            "operationId": f"0x{i:064x}",
            "target": "0x" + "ab" * 20,
            "data": "0x",
            "value": "0",
            "blockTimestamp": "1700000000",
        }
        for i, t in enumerate(TIMELOCK_LIST)
    ]

    process_events(events, use_cache=False)

    known = _known_keys()
    stored = {
        call.args[1]: call.kwargs.get("origin_protocol") or call.args[1]
        for call in mock_send.call_args_list  # type: ignore[attr-defined]
    }
    assert stored, "no alerts were sent; the fixture no longer matches TIMELOCKS"
    unknown = {routing: key for routing, key in stored.items() if key not in known}
    assert not unknown, (
        f"process_events stored alerts under keys no website page queries: {unknown}. "
        "Pass the page key as `origin_protocol=` in process_events."
    )
    assert stored[YEARN_TIMELOCK_INTERNAL_PROTOCOL] == YEARN_TIMELOCK_INTERNAL_PROTOCOL


def test_yearn_timelock_alerts_land_on_the_yearn_page() -> None:
    """The original bug, pinned: YEARN_TIMELOCK must be stored as the `yearn` page key."""
    assert alert_history_protocol("YEARN_TIMELOCK") == "yearn"
    assert "yearn" in _known_keys()


def test_slug_overrides_reference_real_slugs() -> None:
    slugs = set(load_monitoring_config().protocols)
    stale = set(SLUG_ALERT_OVERRIDES) - slugs
    assert not stale, f"SLUG_ALERT_OVERRIDES maps slugs missing from monitoring.yaml: {stale}"


def test_pageless_keys_are_not_slugs() -> None:
    """A key that has a page should be matched by slug, not allowlisted as page-less."""
    slugs = set(load_monitoring_config().protocols)
    overlap = {key for key in PAGELESS_ALERT_PROTOCOLS if key in slugs}
    assert not overlap, f"PAGELESS_ALERT_PROTOCOLS lists keys that do have a page: {overlap}"
