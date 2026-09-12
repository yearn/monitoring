"""Registry of the protocol keys the monitoring website can display.

The website renders one page per ``monitoring.yaml`` slug and loads its alert
history with ``GET /v1/alerts?protocol=<key>``, an exact, case-sensitive match.
The key is the slug itself unless listed in :data:`SLUG_ALERT_OVERRIDES`.

An alert stored under any other key still reaches Telegram but never shows on the
site. That is how every timelock alert stayed hidden: they were stored under their
uppercase Telegram routing key (``YEARN_TIMELOCK``, ``CAP``, ...) while the pages
asked for ``yearn``, ``cap``, and so on.
"""

from __future__ import annotations

from collections.abc import Iterable

# monitoring.yaml slug -> the alert protocol key that slug's page queries.
# Mirrors SLUG_TO_ALERT_PROTOCOL in the frontend (risk-score,
# src/data/monitoring.ts); slugs absent here use the slug itself.
SLUG_ALERT_OVERRIDES: dict[str, str] = {
    "compound": "comp",
    "rtoken": "ethplus",
}

# Keys that deliberately have no protocol page. Alerts stored under these are
# Telegram-only (plus the cross-protocol overview feed, which matches
# case-insensitively). Add a key here only when it is meant to stay off the site.
PAGELESS_ALERT_PROTOCOLS: frozenset[str] = frozenset(
    {
        # lrt-pegs monitors emit several keys, so its page has no single one to
        # query and the frontend leaves its key empty.
        "pegs",
        "origin",
        "lrt",
        # Per-asset peg monitors (utils/pegged_assets.py) — no page of their own.
        "lombard",
        "coinbase",
        "wbtc",
        "tether",
        "circle",
        # Internal-only mirror of the Yearn timelock alerts; keeping its own key
        # stops the Yearn page listing every alert twice.
        "YEARN_TIMELOCK_INTERNAL",
        # Automation run digests, not a monitored protocol.
        "automation",
    }
)


def website_alert_protocol(slug: str) -> str:
    """Return the alert protocol key the website queries for a ``monitoring.yaml`` slug."""
    return SLUG_ALERT_OVERRIDES.get(slug, slug)


def known_alert_protocols(slugs: Iterable[str]) -> set[str]:
    """Return every alert protocol key that is valid for the given slugs.

    Args:
        slugs: ``monitoring.yaml`` protocol slugs.

    Returns:
        The keys each slug's page queries, plus the deliberately page-less keys.
    """
    return {website_alert_protocol(slug) for slug in slugs} | set(PAGELESS_ALERT_PROTOCOLS)
