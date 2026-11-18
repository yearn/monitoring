"""Tests for publishing complete upgrade source diffs."""

from unittest.mock import patch

import pytest

from utils.upgrade_diff import publish_upgrade_diff
from utils.verified_contract import VerifiedContract

OLD_IMPL = "0x" + "a" * 40
NEW_IMPL = "0x" + "b" * 40
GIST_URL = "https://gist.wavey.info/contract-diff"


def _verified(sources: dict[str, str]) -> VerifiedContract:
    """Return a verified contract record with the given sources."""
    return VerifiedContract("Vault", "v0.8.28", "Solidity", sources)


@pytest.fixture(autouse=True)
def _wavey_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of the local Wavey configuration."""
    monkeypatch.setenv("WAVEY_GIST_API_KEY", "test-key")


def test_publishes_every_changed_file_and_hunk() -> None:
    """Include added and removed files, and changes beyond the AI prompt budget."""
    old = {f"file{i}.sol": "old\n" for i in range(7)}
    new = {f"file{i}.sol": "new\n" for i in range(7)}
    old["removed.sol"] = "removed\n"
    new["added.sol"] = ("// source\n" * 70) + "// ``` final line"
    old["same.sol"] = new["same.sol"] = "unchanged\n"

    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[_verified(old), _verified(new)]) as fetch,
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == GIST_URL

    assert [call.args for call in fetch.call_args_list] == [(1, OLD_IMPL), (1, NEW_IMPL)]
    assert upload.call_args.kwargs["title"] == "Vault upgrade source diff"
    body = upload.call_args.args[0]
    assert f"https://etherscan.io/address/{OLD_IMPL}#code" in body
    assert f"https://etherscan.io/address/{NEW_IMPL}#code" in body
    assert "+++ b/file6.sol" in body
    assert "--- /dev/null\n+++ b/added.sol" in body
    assert "--- a/removed.sol\n+++ /dev/null" in body
    assert "````diff" in body
    assert "+// ``` final line\n\\ No newline at end of file" in body
    assert "same.sol" not in body


def test_missing_key_skips_source_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the explorer fallback available without extra API calls."""
    monkeypatch.delenv("WAVEY_GIST_API_KEY")
    with patch("utils.upgrade_diff.fetch_verified_contract") as fetch:
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == ""
    fetch.assert_not_called()


@pytest.mark.parametrize("new", [None, _verified({})])
def test_missing_verified_source_skips_upload(new: VerifiedContract | None) -> None:
    """Only publish when both verified source bundles are available."""
    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[_verified({"a.sol": "old"}), new]),
        patch("utils.upgrade_diff.upload_to_gist") as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == ""
    upload.assert_not_called()


def test_oversized_diff_skips_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the explorer fallback when the full page exceeds the service limit."""
    monkeypatch.setattr("utils.upgrade_diff.MAX_GIST_BYTES", 600)
    with (
        patch(
            "utils.upgrade_diff.fetch_verified_contract",
            side_effect=[_verified({"a.sol": "old"}), _verified({"a.sol": "new" * 200})],
        ),
        patch("utils.upgrade_diff.upload_to_gist") as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == ""
    upload.assert_not_called()
