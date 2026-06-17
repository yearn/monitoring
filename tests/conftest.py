"""Pytest fixtures shared across the test suite.

Tests are supposed to mock their own network dependencies. None of our tests
make real RPC calls or hit Etherscan — every place that touches ChainManager
or `fetch_source` patches the call site. The fixtures here are defense-in-
depth: they make sure that if a future test forgets a mock, the failure is a
clean "no token" / "no provider" rather than a slow live call that depends
on which env vars the developer happens to have set.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_live_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block accidental live API/RPC calls and reset cross-test singletons.

    Strips `ETHERSCAN_TOKEN`, every `PROVIDER_URL_*`, every `TELEGRAM_*`
    credential, and emergency webhook credentials so a missing mock short-circuits cheaply via
    the "no token / no provider / no credentials" code paths that already exist
    for production use. Forces `LOG_LEVEL=INFO` so a developer's `.env`
    `LOG_LEVEL=DEBUG` (which skips Telegram sends) can't change tested behavior.
    Tests that intentionally exercise those code paths opt back in via
    monkeypatch / @patch.dict. This keeps local runs deterministic and matching
    CI, where none of these vars are set.

    Also clears `ChainManager._instances` so a real client object cached by
    one test can't leak into the next.
    """
    for key in list(os.environ):
        if key in {"ETHERSCAN_TOKEN", "LIQUIDITY_WEBHOOK_SECRET"} or key.startswith(
            ("PROVIDER_URL_", "TELEGRAM_", "LIQUIDITY_WEBHOOK_")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    try:
        from utils.web3_wrapper import ChainManager

        ChainManager._instances.clear()
    except Exception:  # noqa: BLE001 - test setup is best-effort
        pass
