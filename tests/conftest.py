"""Pytest fixtures shared across the test suite."""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_live_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block accidental live API calls during tests.

    Strips Etherscan + RPC provider env vars so any test that forgets to mock
    a network-touching helper fails fast (with a clear "no provider" error)
    rather than hitting the real Internet and making the test slow / flaky /
    dependent on live state. Tests that actually want to exercise these
    paths override via @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "...",
    "PROVIDER_URL_MAINNET": "..."}).
    """
    for key in list(os.environ):
        if key == "ETHERSCAN_TOKEN" or key.startswith("PROVIDER_URL_"):
            monkeypatch.delenv(key, raising=False)

    # ChainManager memoizes Web3Client instances. A previous test that ran
    # with a real provider URL could have cached one — stale singletons
    # would then make live calls even after we strip env vars above.
    try:
        from utils.web3_wrapper import ChainManager

        ChainManager._instances.clear()
    except Exception:  # noqa: BLE001 - test setup is best-effort
        pass
