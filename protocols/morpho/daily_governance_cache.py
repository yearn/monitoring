"""Point this process's Morpho cache at the daily governance jobs' namespace.

The daily profile in ``automation/jobs.yaml`` runs ``governance.py`` and
``governance_v2.py`` with ``MORPHO_FILENAME`` set to its own namespace, and
``utils.cache`` resolves that name once, at import. The continuous subscriber
runs outside that profile, so without this its live governance checks would read
(and in production, write) a different namespace: no role/set baselines, no
record of what the daily jobs already alerted.

Importing this module sets ``MORPHO_FILENAME`` from the daily profile, unless the
process already sets it explicitly, so it must be imported before anything
imports ``utils.cache``. ``check_daily_governance_cache`` verifies at startup that
the order held.
"""

import os

from automation.config import load_jobs_config

DAILY_PROFILE = "daily"


def daily_morpho_filename() -> str:
    """Return the ``MORPHO_FILENAME`` the daily governance jobs run with."""
    filename = load_jobs_config().profiles[DAILY_PROFILE].env.get("MORPHO_FILENAME")
    if not filename:
        raise RuntimeError(f"automation/jobs.yaml profile {DAILY_PROFILE!r} sets no MORPHO_FILENAME")
    return filename


def use_daily_governance_cache() -> str:
    """Set ``MORPHO_FILENAME`` to the daily jobs' namespace unless already set, and return it."""
    return os.environ.setdefault("MORPHO_FILENAME", daily_morpho_filename())


def check_daily_governance_cache() -> None:
    """Fail unless ``utils.cache`` resolved the Morpho cache to the daily jobs' namespace.

    Raises:
        RuntimeError: ``utils.cache`` was imported before this module, or with another name.
    """
    from utils import cache  # noqa: PLC0415 - checks the binding the import above must not have fixed early

    expected = os.environ.get("MORPHO_FILENAME") or daily_morpho_filename()
    if os.path.basename(cache.morpho_filename) != expected:
        raise RuntimeError(
            f"utils.cache uses {cache.morpho_filename}, not the daily governance namespace {expected}: "
            f"import {__name__} before anything imports utils.cache"
        )


use_daily_governance_cache()
