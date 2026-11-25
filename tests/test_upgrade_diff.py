"""Tests for publishing complete upgrade source diffs."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import pytest

from utils.upgrade_diff import (
    REQUEST_TIMEOUT_SECONDS,
    UpgradeDiffLink,
    publish_upgrade_diff,
    resolve_upgrade_diff_links,
)
from utils.verified_contract import VerifiedContract, parse_etherscan_entry

OLD_IMPL = "0x" + "a" * 40
NEW_IMPL = "0x" + "b" * 40
GIST_URL = "https://gist.wavey.info/contract-diff"
EXPLORER_DIFF = f"https://etherscan.io/contractdiffchecker?a1={OLD_IMPL}&a2={NEW_IMPL}"
UPGRADE = UpgradeDiffLink("0x" + "c" * 40, OLD_IMPL, NEW_IMPL, EXPLORER_DIFF)


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
    for call in [*fetch.call_args_list, upload.call_args]:
        assert call.kwargs["retries"] == 0
        assert 0 < call.kwargs["timeout"] <= REQUEST_TIMEOUT_SECONDS
    assert all(call.kwargs["require_constructor_arguments"] for call in fetch.call_args_list)
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
    assert "No source file changes." in body
    assert body.index("## Verified source") < body.index("## Deployment changes")
    assert "compiler: v0.8.28 → v0.8.29" in body
    assert f"linked library Lib: {OLD_IMPL} → {NEW_IMPL}" in body
    assert "constructor arguments: differ" in body


@pytest.mark.parametrize("constructor_arguments", [("00", "00"), (None, "01")])
def test_unchanged_deployment_is_not_listed(constructor_arguments: tuple[str | None, str | None]) -> None:
    """Omit the deployment section when nothing changed or nothing can be compared."""
    base = _verified({"a.sol": "contract Vault {}\n"})
    old = replace(base, constructor_arguments=constructor_arguments[0])
    new = replace(
        base, sources={"a.sol": "contract Vault { uint256 x; }\n"}, constructor_arguments=constructor_arguments[1]
    )
    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[old, new]),
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == GIST_URL
    body = upload.call_args.args[0]
    assert "Deployment" not in body
    assert ("constructor arguments unavailable" in body) == (None in constructor_arguments)
    assert "constructor arguments: differ" not in body


@pytest.mark.parametrize("aligned_side", ["new", "old", None])
def test_mixed_providers_are_compared_from_one_provider(aligned_side: str | None) -> None:
    """Refetch one side so formatting differences between providers do not show as changes."""
    etherscan_old = replace(_verified({"Vault.vy": "# old\n"}), compiler_version="v0.3.10")
    sourcify_new = replace(
        _verified({"contracts/Vault.vy": "# new\n"}), compiler_version="0.3.10+commit.9136169", provider="Sourcify"
    )
    etherscan_new = replace(
        sourcify_new, sources={"Vault.vy": "# new\n"}, compiler_version="v0.3.10", provider="Etherscan"
    )
    sourcify_old = replace(sourcify_new, sources={"contracts/Vault.vy": "# old\n"})
    responses = {"new": [etherscan_new], "old": [None, sourcify_old], None: [None, None]}[aligned_side]
    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[etherscan_old, sourcify_new]),
        patch("utils.upgrade_diff.fetch_contract_from_provider", side_effect=responses) as refetch,
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == GIST_URL

    assert [call.args for call in refetch.call_args_list] == [(1, NEW_IMPL, "Etherscan"), (1, OLD_IMPL, "Sourcify")][
        : len(responses)
    ]
    assert all(0 < call.kwargs["timeout"] <= REQUEST_TIMEOUT_SECONDS for call in refetch.call_args_list)
    body = upload.call_args.args[0]
    assert ("different source providers" in body) == (aligned_side is None)
    if aligned_side is None:
        return
    path = "Vault.vy" if aligned_side == "new" else "contracts/Vault.vy"
    assert f"--- a/{path}\n+++ b/{path}" in body
    assert "/dev/null" not in body
    assert "compiler:" not in body


@pytest.mark.parametrize("new_arguments", ["1234", "abcd"])
def test_same_provider_diff_preserves_verified_constructor_arguments(new_arguments: str) -> None:
    """Align source files without replacing verified deployment arguments."""
    old_source = "contract Vault {}"
    old = replace(
        _verified({"src/Vault.sol": old_source}),
        provider="Sourcify",
        constructor_arguments="1234",
    )
    new = replace(_verified({"Vault.sol": "contract Vault { uint256 x; }"}), constructor_arguments=new_arguments)
    aligned_old = parse_etherscan_entry(
        {"SourceCode": old_source, "ContractName": "Vault", "ConstructorArguments": "", "SimilarMatch": NEW_IMPL}
    )
    with (
        patch("utils.upgrade_diff.fetch_verified_contract", side_effect=[old, new]),
        patch("utils.upgrade_diff.fetch_contract_from_provider", side_effect=[None, aligned_old]),
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1) == GIST_URL

    body = upload.call_args.args[0]
    assert "--- a/Vault.sol\n+++ b/Vault.sol" in body
    assert "constructor arguments unavailable" not in body
    assert ("constructor arguments: differ" in body) == (new_arguments != "1234")


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


def test_upload_is_reused_across_proxies() -> None:
    """Use one gist for the same pair; an overlapping alert waits for it."""
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
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        job = pool.submit(publish_upgrade_diff, OLD_IMPL, NEW_IMPL, 1)
        try:
            assert started.wait(3)
            # A second alert waits for the in-flight publication and reuses it.
            other_proxy = replace(UPGRADE, proxy="0x" + "d" * 40)
            overlapping = pool.submit(resolve_upgrade_diff_links, [other_proxy], 1)
            assert not overlapping.done()
        finally:
            release.set()
        assert job.result() == GIST_URL
        assert overlapping.result(timeout=5) == {EXPLORER_DIFF: GIST_URL}
        assert resolve_upgrade_diff_links([UPGRADE], 1) == {EXPLORER_DIFF: GIST_URL}
    assert fetch.call_count == 2
    upload.assert_called_once()


def test_batch_deadline_leaves_explorer_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the alert's wait even when an upload is still running."""
    other_impl = "0x" + "d" * 40
    other_explorer = f"https://etherscan.io/contractdiffchecker?a1={OLD_IMPL}&a2={other_impl}"
    other_upgrade = replace(UPGRADE, new_impl=other_impl, explorer_url=other_explorer)
    monkeypatch.setattr("utils.upgrade_diff.DIFF_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr("utils.upgrade_diff.DEADLINE_GRACE_SECONDS", 0)
    release = Event()

    def upload(_old: str, new: str, _chain: int, **_kwargs: object) -> str:
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


def test_jobs_share_one_alert_deadline() -> None:
    """Give every comparison in the alert the same absolute deadline."""
    other = replace(UPGRADE, new_impl="0x" + "d" * 40, explorer_url="other")
    with patch("utils.upgrade_diff.publish_upgrade_diff", return_value="") as publish:
        before = time.monotonic()
        resolve_upgrade_diff_links([UPGRADE, other], 1)
    deadlines = {call.kwargs["deadline"] for call in publish.call_args_list}
    assert len(deadlines) == 1
    assert deadlines.pop() >= before + 60


def test_no_upload_after_deadline() -> None:
    """Never publish a gist the alert has stopped waiting for."""
    clock = iter([0.0, 0.0, 30.0, 59.5])

    with (
        patch("utils.upgrade_diff.monotonic", side_effect=lambda: next(clock)),
        patch(
            "utils.upgrade_diff.fetch_verified_contract",
            side_effect=[_verified({"a.sol": "old"}), _verified({"a.sol": "new"})],
        ) as fetch,
        patch("utils.upgrade_diff.upload_to_gist") as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1, deadline=60.0) == ""
    assert [call.kwargs["timeout"] for call in fetch.call_args_list] == [20, 20]
    upload.assert_not_called()


def test_request_timeout_is_capped_by_remaining_deadline() -> None:
    """Keep a late request from running past the alert deadline."""
    clock = iter([0.0, 50.0, 55.0, 57.5])

    with (
        patch("utils.upgrade_diff.monotonic", side_effect=lambda: next(clock)),
        patch(
            "utils.upgrade_diff.fetch_verified_contract",
            side_effect=[_verified({"a.sol": "old"}), _verified({"a.sol": "new"})],
        ) as fetch,
        patch("utils.upgrade_diff.upload_to_gist", return_value=GIST_URL) as upload,
    ):
        assert publish_upgrade_diff(OLD_IMPL, NEW_IMPL, 1, deadline=60.0) == GIST_URL
    assert [call.kwargs["timeout"] for call in fetch.call_args_list] == [10, 5]
    assert upload.call_args.kwargs["timeout"] == 2.5
