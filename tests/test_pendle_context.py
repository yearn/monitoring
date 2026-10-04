"""PendleSwap upgrade context regressions using the 2026-10-04 verified sources."""

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils.calldata.decoder import DecodedCall
from utils.llm import pendle_context
from utils.llm.ai_explainer import _build_prompt
from utils.llm.pendle_context import (
    PENDLE_SWAP,
    _read_owner,
    _source_evidence,
    format_pendle_prompt,
    format_pendle_report,
    resolve_pendle_context,
)
from utils.llm.protocol_context import resolve_protocol_context
from utils.verified_contract import VerifiedContract

OLD = "0xBC17404b7bb500051c75C83E4aA5aE447D967811"
NEW = "0xD14feb6Aaf8650BbfcC8aBEc299B249a80FE7C78"
OWNER = "0x8119EC16F0573B7dAc7C0CB94EB504FB32456ee1"
type Boundaries = tuple[MagicMock, MagicMock, MagicMock]


def _record(name: str) -> VerifiedContract:
    """Load the scoped verified bundle fixture."""
    data = json.loads((Path(__file__).parent / "fixtures" / "pendle_upgrade" / f"{name}.json").read_text())
    record = VerifiedContract.from_cache_dict(data)
    assert record is not None
    return record


def _upgrade(migrate: bool = False) -> DecodedCall:
    """A decoded upgrade with or without an initialization payload."""
    if migrate:
        return DecodedCall(
            "upgradeToAndCall", "upgradeToAndCall(address,bytes)", [("address", NEW), ("bytes", b"\x01")]
        )
    return DecodedCall("upgradeTo", "upgradeTo(address)", [("address", NEW)])


@pytest.fixture
def boundaries() -> Iterator[Boundaries]:
    """Replace RPC and source fetches while retaining all context resolution logic."""
    with (
        patch.object(pendle_context, "get_current_implementation", return_value=OLD) as implementation,
        patch.object(pendle_context, "fetch_verified_contract") as source,
        patch.object(pendle_context.ChainManager, "get_client") as client,
    ):
        source.side_effect = lambda chain, address: _record("old" if address.lower() == OLD.lower() else "new")
        client.return_value.eth.contract.return_value.functions.owner.return_value.call.return_value = OWNER
        yield implementation, source, client


@pytest.mark.parametrize("chain_id", [1, 42161])
def test_upgrade_includes_scope_controls_and_route_semantics(boundaries: Boundaries, chain_id: int) -> None:
    """Unchanged ABI must not hide enum changes, unsupported scaling or existing authorization."""
    contexts = resolve_pendle_context("PENDLE", chain_id, [(PENDLE_SWAP.lower(), _upgrade())])
    assert len(contexts) == 1
    prompt = format_pendle_prompt(contexts)
    assert "optional aggregator leg" in prompt
    assert "NONE and ETH_WETH branches bypass" in prompt
    assert "calling router for the next step" in prompt
    assert "not a live trace" in prompt
    assert "not proof every caller uses a nonzero minimum" in prompt
    assert f"Current proxy owner() (live read): {OWNER}" in prompt
    assert "_authorizeUpgrade(address) internal virtual override onlyOwner" in prompt
    assert 'require(msg.sender == owner, "Ownable: caller is not the owner")' in prompt
    assert "type(uint256).max" in prompt
    before, after = prompt.split("Proposed implementation evidence:")
    assert "SwapType.ODOS" in before
    assert "RESERVE_2" in before
    assert "RESERVE_1" in after
    assert "ZEROX" in after
    assert "SwapType.ODOS" not in after
    assert "assert(false)" in after
    assert "(quotedMinOut * amountIn) / quotedAmountIn" in after
    assert "_transferOut(tokenOut, msg.sender, netOut)" in after
    assert "No fixed risk rating" in prompt
    assert "removes ODOS calldata scaling" in prompt
    assert "does not by itself establish a higher likelihood of storage collisions" in prompt


def test_registry_publishes_context_and_full_address_links(boundaries: Boundaries) -> None:
    """The adapter's facts and additional owner/implementation addresses reach the report."""
    resolved = resolve_protocol_context("pendle", 1, [(PENDLE_SWAP, _upgrade())])
    assert "optional aggregator leg" in resolved.prompt
    assert "optional aggregator leg" in resolved.report
    for address in (PENDLE_SWAP, OLD, NEW, OWNER):
        assert address in resolved.addresses
        assert f"https://etherscan.io/address/{address}" in resolved.report
    assert resolved.labels[PENDLE_SWAP] == "PendleSwap"


def test_prompt_preserves_integration_reference_and_live_observation_distinction(boundaries: Boundaries) -> None:
    """The explainer must not relabel documented router flow as a live execution trace."""
    contexts = resolve_pendle_context("pendle", 1, [(PENDLE_SWAP, _upgrade())])
    prompt = _build_prompt(
        target=PENDLE_SWAP,
        value=0,
        decoded_calls=[_upgrade()],
        simulation=None,
        protocol_context=format_pendle_prompt(contexts),
    )
    assert "Distinguish documented integration architecture from live observations" in prompt
    assert "not a live trace" in prompt


def test_arbitrum_report_uses_arbitrum_links(boundaries: Boundaries) -> None:
    """Same deployment addresses must link to the actual alert chain."""
    contexts = resolve_pendle_context("pendle", 42161, [(PENDLE_SWAP, _upgrade())])
    report = format_pendle_report(contexts, 42161, {})
    assert f"https://arbiscan.io/address/{PENDLE_SWAP}" in report
    assert "etherscan.io/address" not in report


@pytest.mark.parametrize(
    "protocol,chain,target,call",
    [
        ("yearn", 1, PENDLE_SWAP, _upgrade()),
        ("pendle", 10, PENDLE_SWAP, _upgrade()),
        ("pendle", 1, OWNER, _upgrade()),
        ("pendle", 1, PENDLE_SWAP, DecodedCall("swap", "swap(address,uint256)", [])),
        ("pendle", 1, PENDLE_SWAP, DecodedCall("upgradeTo", "upgradeTo(address)", [])),
        ("pendle", 1, PENDLE_SWAP, DecodedCall("upgradeTo", "upgradeTo(address)", [("uint256", 1)])),
        ("pendle", 1, PENDLE_SWAP, DecodedCall("upgradeTo", "upgradeTo(address)", [("address", "bad")])),
    ],
)
def test_unrelated_or_malformed_calls_make_no_network_requests(
    boundaries: Boundaries,
    protocol: str,
    chain: int,
    target: str,
    call: DecodedCall,
) -> None:
    """Scope checks run before all RPC/source work."""
    assert resolve_pendle_context(protocol, chain, [(target, call)]) == []
    for boundary in boundaries:
        boundary.assert_not_called()


def test_empty_batch_makes_no_network_requests(boundaries: Boundaries) -> None:
    """Empty alerts need no enrichment."""
    assert resolve_pendle_context("pendle", 1, []) == []
    for boundary in boundaries:
        boundary.assert_not_called()


def test_duplicate_calls_and_migration_are_distinguished(boundaries: Boundaries) -> None:
    """A Safe batch retains migration context while repeated identical upgrades are coalesced."""
    contexts = resolve_pendle_context(
        "pendle",
        1,
        [
            (PENDLE_SWAP, _upgrade()),
            (PENDLE_SWAP, _upgrade()),
            (PENDLE_SWAP, _upgrade(True)),
        ],
    )
    assert len(contexts) == 2
    prompt = format_pendle_prompt(contexts)
    assert "upgradeTo has no initialization/migration payload" in prompt
    assert "upgradeToAndCall includes a bytes payload" in prompt


def test_owner_read_failure_preserves_source_evidence(boundaries: Boundaries) -> None:
    """An unavailable owner is explicit and cannot remove the code comparison."""
    boundaries[2].return_value.eth.contract.return_value.functions.owner.return_value.call.side_effect = RuntimeError(
        "RPC failed"
    )
    contexts = resolve_pendle_context("pendle", 1, [(PENDLE_SWAP, _upgrade())])
    prompt = format_pendle_prompt(contexts)
    assert "Current proxy owner() (live read): unavailable" in prompt
    assert "_zeroExSwap" in prompt
    assert contexts[0].owner is None


def test_owner_is_read_from_proxy_not_implementation() -> None:
    """Ownership state lives at the proxy address."""
    client = MagicMock()
    client.eth.contract.return_value.functions.owner.return_value.call.return_value = OWNER
    with patch.object(pendle_context.ChainManager, "get_client", return_value=client):
        assert _read_owner(1, PENDLE_SWAP) == OWNER
    assert client.eth.contract.call_args.kwargs["address"] == PENDLE_SWAP


def test_missing_source_and_implementation_are_explicit(boundaries: Boundaries) -> None:
    """Missing evidence never turns into a guessed old implementation or safe-storage claim."""
    boundaries[0].return_value = None
    boundaries[1].side_effect = None
    boundaries[1].return_value = None
    contexts = resolve_pendle_context("pendle", 1, [(PENDLE_SWAP, _upgrade())])
    prompt = format_pendle_prompt(contexts)
    assert "Current implementation: unavailable" in prompt
    assert prompt.count("target source unavailable") == 2
    assert "Storage UNKNOWN is a validation gap, not a proven collision" in prompt


def test_failed_batch_member_does_not_hide_next_upgrade(boundaries: Boundaries) -> None:
    """Source/RPC errors remain local to a batch member."""
    boundaries[0].side_effect = [RuntimeError("RPC failed"), OLD]
    contexts = resolve_pendle_context("pendle", 1, [(PENDLE_SWAP, _upgrade()), (PENDLE_SWAP, _upgrade(True))])
    assert len(contexts) == 1
    assert contexts[0].is_upgrade_and_call


def test_source_excerpts_are_scoped_to_the_deployed_contract() -> None:
    """A same-name member in another bundled contract must not become PendleSwap evidence."""
    record = _record("new")
    assert record.contract_file is not None
    sources = dict(record.sources)
    sources[record.contract_file] += '\ncontract Decoy { function swap() external { revert("DECOY"); } }'
    assert "DECOY" not in _source_evidence(replace(record, sources=sources))


def test_flattened_source_does_not_include_unrelated_contracts() -> None:
    """Imported swap declarations must not cause an entire flattened bundle to enter the prompt."""
    record = _record("new")
    flattened = "\n".join(record.sources.values())
    flattened += '\ncontract Decoy { function swap() external { revert("DECOY"); } }'
    evidence = _source_evidence(replace(record, sources={"flat.sol": flattened}, contract_file="flat.sol"))
    assert "DECOY" not in evidence
    assert "enum SwapType" in evidence
    assert "struct SwapData" in evidence
    assert "_zeroExSwap" in evidence
    assert 'require(msg.sender == owner, "Ownable: caller is not the owner")' in evidence


def test_other_replacement_and_unresolved_target_are_not_assumed_to_be_pendle() -> None:
    """Labels and imported interfaces cannot establish replacement behavior."""
    record = _record("new")
    for candidate in (None, replace(record, contract_name="OtherContract"), replace(record, contract_file=None)):
        evidence = _source_evidence(candidate)
        assert "unavailable" in evidence
        assert "_zeroExSwap" not in evidence


def test_ambiguous_interfaces_and_owner_bases_are_not_guessed() -> None:
    """Duplicate declarations in a bundle leave their facts unresolved."""
    record = _record("new")
    sources = dict(record.sources)
    for path, source in record.sources.items():
        if path.endswith(("IPSwapAggregator.sol", "BoringOwnableUpgradeableV2.sol")):
            sources[f"decoy/{path}"] = source
    evidence = _source_evidence(replace(record, sources=sources))
    assert "Verified swap payload/enum" not in evidence
    assert "Verified ownership guard" not in evidence
    assert "_zeroExSwap" in evidence
