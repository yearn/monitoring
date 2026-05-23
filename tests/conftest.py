"""Pytest fixtures shared across the test suite."""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_live_etherscan_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ETHERSCAN_TOKEN to empty so tests don't accidentally hit the live API.

    Several tests in test_ai_explainer.py exercise paths that internally call
    Etherscan (via fetch_source / fetch_function_input_names). When a developer
    has ETHERSCAN_TOKEN set in their environment, those tests would make real
    network requests and assertions would depend on live API state. Tests that
    actually want to exercise the Etherscan path override this via
    @patch.dict("os.environ", {"ETHERSCAN_TOKEN": "test-key"}).
    """
    if os.environ.get("ETHERSCAN_TOKEN"):
        monkeypatch.delenv("ETHERSCAN_TOKEN", raising=False)
