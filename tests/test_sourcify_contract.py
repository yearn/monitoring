"""Exercise both verified-source providers through the shared lookup."""

from unittest.mock import Mock

import pytest
from requests import Timeout

from utils.source_context import (
    _disk_key,
    _source_disk_cache,
    fetch_abi_entries,
    fetch_verified_contract,
    format_source_context,
    get_source_context,
    get_verification_status,
    reset_cache,
)
from utils.verified_contract import VerifiedContract

ADDRESS = "0xCa2081136Dad8d2277A2387Ce68995958C35Ed98"
LIBRARY = "0x" + "ab" * 20
SOURCE = "contract Vault {}"


@pytest.fixture(autouse=True)
def clear_source_memory() -> None:
    """Start with an empty in-process cache for each scenario."""
    reset_cache()


def _sourcify_response(
    *, creation_match: str | None = "match", runtime_match: str | None = "exact_match", source: str = SOURCE
) -> Mock:
    """Return a Sourcify response with source and deployment artifacts."""
    return Mock(
        **{
            "json.return_value": {
                "match": runtime_match or creation_match,
                "runtimeMatch": runtime_match,
                "creationMatch": creation_match,
                "sources": {"src/Vault.sol": {"content": source}},
                "abi": [{"type": "constructor", "inputs": [{"type": "address"}]}],
                "compilation": {
                    "language": "Solidity",
                    "compilerVersion": "0.8.34+commit.80d5c536",
                    "compilerSettings": {"evmVersion": "cancun"},
                    "fullyQualifiedName": "src/Vault.sol:Vault",
                },
                "creationBytecode": {"transformationValues": {"constructorArguments": "0x12AB"}},
                "runtimeBytecode": {"transformationValues": {"libraries": {"src/Lib.sol:Lib": LIBRARY}}},
            }
        }
    )


def _etherscan_response(source: str = SOURCE) -> Mock:
    """Return a mocked Etherscan source request."""
    return Mock(
        return_value={
            "status": "1",
            "result": [{"SourceCode": source, "ContractName": "Vault", "ConstructorArguments": "0x34"}],
        }
    )


def test_sourcify_result_shared_across_consumers_and_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch once without an Etherscan key and persist sources and provenance."""
    sourcify = Mock(return_value=_sourcify_response())
    etherscan = Mock()
    monkeypatch.setattr("utils.sourcify_contract.request_with_retry", sourcify)
    monkeypatch.setattr("utils.source_context.fetch_json", etherscan)

    assert get_verification_status(1, ADDRESS) is True
    assert fetch_abi_entries(1, ADDRESS) == [{"type": "constructor", "inputs": [{"type": "address"}]}]
    reset_cache()
    record = fetch_verified_contract(1, ADDRESS.lower(), require_constructor_arguments=True)

    assert record is not None
    assert record.target_source == SOURCE
    assert record.compilation_target == ("src/Vault.sol", "Vault")
    assert record.compiler_version == "v0.8.34+commit.80d5c536"
    assert record.settings["libraries"] == {"src/Lib.sol": {"Lib": LIBRARY}}
    assert record.constructor_arguments == "12ab"
    assert (record.provider, record.match_quality) == ("Sourcify", "exact_match")
    sourcify.assert_called_once()
    assert sourcify.call_args.kwargs["headers"]["User-Agent"].startswith("yearn-monitoring")
    etherscan.assert_not_called()


@pytest.mark.parametrize("runtime_only", [False, True])
def test_etherscan_supplies_missing_source_or_constructor_arguments(
    monkeypatch: pytest.MonkeyPatch, runtime_only: bool
) -> None:
    """Etherscan completes a missing bundle or a cached runtime-only record."""
    etherscan = _etherscan_response()
    monkeypatch.setenv("ETHERSCAN_TOKEN", "test-key")
    monkeypatch.setattr("utils.source_context.fetch_json", etherscan)
    sourcify = Mock(return_value=_sourcify_response(creation_match=None))
    if runtime_only:
        monkeypatch.setattr("utils.sourcify_contract.request_with_retry", sourcify)
        initial = fetch_verified_contract(1, ADDRESS)
        assert initial is not None
        assert initial.provider == "Sourcify"
        assert initial.constructor_arguments is None
        etherscan.assert_not_called()

    record = fetch_verified_contract(1, ADDRESS, require_constructor_arguments=True)
    assert record is not None
    assert record.provider == "Etherscan"
    assert record.constructor_arguments == "34"
    assert get_verification_status(1, ADDRESS) is True
    etherscan.assert_called_once()
    if runtime_only:
        sourcify.assert_called_once()


def test_only_confirmed_misses_are_cached_as_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider outage keeps verification unknown and allows a lookup in a later run."""
    etherscan = _etherscan_response("")
    monkeypatch.setenv("ETHERSCAN_TOKEN", "test-key")
    monkeypatch.setattr("utils.source_context.fetch_json", etherscan)
    with monkeypatch.context() as unavailable:
        unavailable.setattr("utils.sourcify_contract.request_with_retry", Mock(side_effect=Timeout))
        assert get_verification_status(1, ADDRESS) is None

    reset_cache()
    assert get_verification_status(1, ADDRESS) is False
    reset_cache()
    assert fetch_verified_contract(1, ADDRESS) is None
    assert get_verification_status(1, ADDRESS) is False
    assert etherscan.call_count == 2


@pytest.mark.parametrize("etherscan_key", [False, True])
def test_unknown_result_is_reused_by_unbounded_readers(monkeypatch: pytest.MonkeyPatch, etherscan_key: bool) -> None:
    """Repeat readers do not refetch an unknown result, but a budgeted lookup still tries."""
    sourcify = Mock(side_effect=Timeout)
    etherscan = Mock(return_value=None)
    monkeypatch.setattr("utils.sourcify_contract.request_with_retry", sourcify)
    monkeypatch.setattr("utils.source_context.fetch_json", etherscan)
    if etherscan_key:
        monkeypatch.setenv("ETHERSCAN_TOKEN", "test-key")

    assert fetch_abi_entries(1, ADDRESS) is None
    assert fetch_verified_contract(1, ADDRESS) is None
    assert get_verification_status(1, ADDRESS) is None
    assert sourcify.call_count == 1
    assert etherscan.call_count == int(etherscan_key)

    sourcify.side_effect = None
    sourcify.return_value = _sourcify_response()
    record = fetch_verified_contract(1, ADDRESS, timeout=5, retries=0)
    assert record is not None
    assert sourcify.call_count == 2
    assert get_verification_status(1, ADDRESS) is True


def test_fallback_uses_remaining_alert_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap Etherscan's timeout after Sourcify consumes part of the budget."""
    etherscan = _etherscan_response()
    monkeypatch.setenv("ETHERSCAN_TOKEN", "test-key")
    monkeypatch.setattr("utils.source_context.fetch_json", etherscan)
    monkeypatch.setattr("utils.source_context.monotonic", Mock(side_effect=[10.0, 14.0]))

    assert fetch_verified_contract(1, ADDRESS, timeout=5, retries=0, deadline=16) is not None
    assert etherscan.call_args.kwargs["timeout"] == 2
    assert etherscan.call_args.kwargs["retries"] == 0


def test_creation_only_match_is_verified_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain creation-only source across runs with explicit match provenance."""
    source = "contract Vault {\n    function update() external {\n        uint256 value = 1;\n    }\n}"
    sourcify = Mock(return_value=_sourcify_response(runtime_match=None, source=source))
    monkeypatch.setattr("utils.sourcify_contract.request_with_retry", sourcify)
    assert get_verification_status(1, ADDRESS) is True
    reset_cache()

    record = fetch_verified_contract(1, ADDRESS, require_constructor_arguments=True)
    assert record is not None
    assert record.match_quality == "creation_only_match"
    assert record.constructor_arguments == "12ab"
    context = get_source_context(1, ADDRESS, "update")
    assert context is not None
    assert "Only creation bytecode matched" in format_source_context(context)
    assert "what actually executes" not in format_source_context(context)
    sourcify.assert_called_once()


@pytest.mark.parametrize("similar_match", ["", LIBRARY])
def test_explainer_prefers_address_specific_source_for_partial_match(
    monkeypatch: pytest.MonkeyPatch, similar_match: str
) -> None:
    """Use stronger source for excerpts and label a remaining partial match."""
    source_template = "contract Vault {\n    /// @notice SOURCE_WORDING\n    function update() external {}\n}"
    sourcify_source = source_template.replace("SOURCE_WORDING", "Sourcify wording")
    etherscan_source = source_template.replace("SOURCE_WORDING", "Etherscan wording")
    sourcify = Mock(return_value=_sourcify_response(runtime_match="match", source=sourcify_source))
    etherscan = _etherscan_response(etherscan_source)
    etherscan.return_value["result"][0]["SimilarMatch"] = similar_match
    monkeypatch.setenv("ETHERSCAN_TOKEN", "test-key")
    monkeypatch.setattr("utils.sourcify_contract.request_with_retry", sourcify)
    monkeypatch.setattr("utils.source_context.fetch_json", etherscan)

    for _ in range(2):
        context = get_source_context(1, ADDRESS, "update")
        assert context is not None
        rendered = format_source_context(context)
        if similar_match:
            assert "Source: Sourcify (match)" in rendered
            assert "Do not quote or rely on them." in rendered
            assert "Etherscan wording" not in rendered
        else:
            assert "Source: Etherscan" in rendered
            assert "Etherscan wording" in rendered
            assert "Sourcify wording" not in rendered
        reset_cache()

    sourcify.assert_called_once()
    etherscan.assert_called_once()


@pytest.mark.parametrize("case", ["legacy_negative", "invalid_record", "missing_constructor", "confirmed_miss"])
def test_disk_hits_count_only_usable_entries(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """Stale, malformed, and incomplete entries count as misses."""
    values = {
        "legacy_negative": None,
        "invalid_record": {"schema": 2, "sources": {}},
        "missing_constructor": VerifiedContract("Vault", "v0.8.34", "Solidity", {"Vault.sol": SOURCE}).to_cache_dict(),
        "confirmed_miss": False,
    }
    _source_disk_cache.set_negative(_disk_key(1, ADDRESS), values[case])
    stats = Mock()
    monkeypatch.setattr("utils.source_context._record_cache_event", stats)
    fetch_verified_contract(1, ADDRESS, require_constructor_arguments=True)
    stats.assert_called_once_with("disk", case == "confirmed_miss", (1, ADDRESS.lower()))
