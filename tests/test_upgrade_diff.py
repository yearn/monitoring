"""Tests for publishing complete upgrade source diffs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import pytest

from utils.upgrade_diff import UpgradeDiffLink, publish_upgrade_diff, resolve_upgrade_diff_links
from utils.verified_contract import VerifiedContract

OLD_IMPL = "0x" + "a" * 40
NEW_IMPL = "0x" + "b" * 40
GIST_URL = "https://gist.wavey.info/contract-diff"
ALLOWLIST_ENTRY = f"1:{OLD_IMPL}:{NEW_IMPL}"
EXPLORER_DIFF = f"https://etherscan.io/contractdiffchecker?a1={OLD_IMPL}&a2={NEW_IMPL}"
UPGRADE = UpgradeDiffLink("0x" + "c" * 40, OLD_IMPL, NEW_IMPL, EXPLORER_DIFF)


def _verified(sources: dict[str, str]) -> VerifiedContract:
    """Return a verified contract record with the given sources."""
    return VerifiedContract("Vault", "v0.8.28", "Solidity", sources)


@pytest.fixture(autouse=True)
def _wavey_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of the local Wavey configuration."""
    monkeypatch.setenv("WAVEY_GIST_API_KEY", "test-key")
    monkeypatch.setenv("WAVEY_PUBLIC_UPGRADE_DIFFS", ALLOWLIST_ENTRY)


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
    assert all(call.kwargs["retries"] == 0 for call in fetch.call_args_list)
    assert upload.call_args.kwargs["title"] == "Vault upgrade source diff"
    assert upload.call_args.kwargs["retries"] == 0
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


@pytest.mark.parametrize("allowlist", ["", f"8453:{OLD_IMPL}:{NEW_IMPL}", f"1:{NEW_IMPL}:{OLD_IMPL}"])
def test_unapproved_pending_upgrade_cannot_publish(allowlist: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require approval for the exact chain and ordered implementation pair."""
    monkeypatch.setenv("WAVEY_PUBLIC_UPGRADE_DIFFS", allowlist)
    with patch("utils.upgrade_diff.fetch_verified_contract") as fetch:
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == ""
        assert resolve_upgrade_diff_links([UPGRADE], 1) == {}
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


def test_matching_sources_show_deployment_changes() -> None:
    """Report compiler, library and constructor differences for identical sources."""
    base = _verified({"a.sol": "contract Vault {}\n"})
    old = replace(base, settings={"libraries": {"a.sol": {"Lib": OLD_IMPL}}}, constructor_arguments="00")
    new = replace(
        base,
        compiler_version="v0.8.29",
        settings={"libraries": {"a.sol": {"Lib": NEW_IMPL}}},
        constructor_arguments="01",
    )
    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[old, new]),
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == GIST_URL
    body = upload.call_args.args[0]
    assert "No source file changes" in body
    assert "compiler: v0.8.28 → v0.8.29" in body
    assert f"linked library Lib: {OLD_IMPL} → {NEW_IMPL}" in body
    assert "Constructor arguments differ" in body


def test_moved_files_keep_paths_and_unusual_line_breaks() -> None:
    """Pair moves and treat Unicode line separators as part of their source lines."""
    old = _verified({"old/Vault.sol": "// before\u2028keep\n", "old/I.sol": "interface I {}\n"})
    new = _verified({"src/Vault.sol": "// after\u2028keep\n", "src/Interface.sol": "interface I {}\n"})
    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[old, new]),
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == GIST_URL
    body = upload.call_args.args[0]
    assert "--- a/old/Vault.sol\n+++ b/src/Vault.sol" in body
    assert "old/I.sol → src/Interface.sol" in body
    assert "File moved. Its contents match." in body
    assert "No newline at end of file" not in body
    assert "/dev/null" not in body


def test_upload_is_reused_across_proxies_and_until_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use one gist for the same pair, including during overlapping alerts."""
    started, release = Event(), Event()

    def wait_and_upload(*_args: object, **_kwargs: object) -> str:
        """Hold publication open while a second alert checks the same pair."""
        started.set()
        assert release.wait(3)
        return GIST_URL

    with (
        patch(
            "utils.upgrade_diff.fetch_verified_contract",
            side_effect=[_verified({"a.sol": "old"}), _verified({"a.sol": "new"})],
        ) as fetch,
        patch("utils.upgrade_diff.upload_to_gist", side_effect=wait_and_upload) as upload,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        job = pool.submit(publish_upgrade_diff, OLD_IMPL, NEW_IMPL, 1)
        try:
            assert started.wait(3)
            assert resolve_upgrade_diff_links([replace(UPGRADE, proxy="0x" + "d" * 40)], 1) == {}
        finally:
            release.set()
        assert job.result() == GIST_URL
        assert resolve_upgrade_diff_links([UPGRADE], 1) == {EXPLORER_DIFF: GIST_URL}
        monkeypatch.delenv("WAVEY_PUBLIC_UPGRADE_DIFFS")
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == ""
    assert fetch.call_count == 2
    upload.assert_called_once()


def test_batch_deadline_leaves_explorer_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the alert's wait even when an upload is still running."""
    other_impl = "0x" + "d" * 40
    other_explorer = f"https://etherscan.io/contractdiffchecker?a1={OLD_IMPL}&a2={other_impl}"
    other_upgrade = replace(UPGRADE, new_impl=other_impl, explorer_url=other_explorer)
    monkeypatch.setenv("WAVEY_PUBLIC_UPGRADE_DIFFS", f"{ALLOWLIST_ENTRY},1:{OLD_IMPL}:{other_impl}")
    monkeypatch.setattr("utils.upgrade_diff.DIFF_TIMEOUT_SECONDS", 0.2)
    release = Event()

    def upload(_old: str, new: str, _chain: int) -> str:
        """Keep the first comparison in flight while the second finishes."""
        if new == NEW_IMPL:
            release.wait(2)
        return GIST_URL

    with patch("utils.upgrade_diff.publish_upgrade_diff", side_effect=upload):
        try:
            assert resolve_upgrade_diff_links([UPGRADE, other_upgrade], 1) == {other_explorer: GIST_URL}
        finally:
            release.set()


def test_publish_failure_logs_exception_and_leaves_explorer_fallback() -> None:
    """Handle unexpected failures without losing the rest of the alert."""
    with (
        patch("utils.upgrade_diff.publish_upgrade_diff", side_effect=ValueError("invalid response")),
        patch("utils.upgrade_diff.logger.exception") as log,
    ):
        assert resolve_upgrade_diff_links([UPGRADE], 1) == {}
    log.assert_called_once()
