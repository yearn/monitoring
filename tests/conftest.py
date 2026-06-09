"""Pytest fixtures shared across the test suite.

Tests are supposed to mock their own network dependencies. None of our tests
make real RPC calls or hit Etherscan — every place that touches ChainManager
or `fetch_source` patches the call site. The fixtures here are defense-in-
depth: they make sure that if a future test forgets a mock, the failure is a
clean "no token" / "no provider" rather than a slow live call that depends
on which env vars the developer happens to have set.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_live_apis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block accidental live API/RPC calls and reset cross-test singletons.

    Strips `ETHERSCAN_TOKEN` and every `PROVIDER_URL_*` so a missing mock
    short-circuits cheaply via the "no token / no provider" code paths that
    already exist for production use. Tests that intentionally exercise
    those code paths opt back in via @patch.dict.

    Also clears `ChainManager._instances` so a real client object cached by
    one test can't leak into the next, and points `CACHE_DIR` at a per-test
    temp dir so the file-backed disk caches (utils.disk_cache) never litter the
    repo and never leak entries between tests.
    """
    for key in list(os.environ):
        if key == "ETHERSCAN_TOKEN" or key.startswith("PROVIDER_URL_"):
            monkeypatch.delenv(key, raising=False)
    # `cache_path` reads this module global at call time, so the redirect takes
    # effect for caches created at import (they resolve their dir lazily).
    monkeypatch.setattr("utils.cache.CACHE_DIR", str(tmp_path))
    try:
        from utils.web3_wrapper import ChainManager

        ChainManager._instances.clear()
    except Exception:  # noqa: BLE001 - test setup is best-effort
        pass
