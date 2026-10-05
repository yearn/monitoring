"""PendleSwap context behavior using small, synthetic source bundles."""

from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from utils.calldata.decoder import DecodedCall
from utils.llm import pendle_context
from utils.llm.pendle_context import (
    PENDLE_SWAP,
    _source_evidence,
    format_pendle_prompt,
    resolve_pendle_context,
)
from utils.llm.protocol_context import resolve_protocol_context
from utils.verified_contract import VerifiedContract

OLD = "0xBC17404b7bb500051c75C83E4aA5aE447D967811"
NEW = "0xD14feb6Aaf8650BbfcC8aBEc299B249a80FE7C78"
OWNER = "0x8119EC16F0573B7dAc7C0CB94EB504FB32456ee1"
type Boundaries = tuple[MagicMock, MagicMock, MagicMock]

_OLD_SOURCE = """
contract PendleSwap {
    function swap() external { _getScaledInputData(SwapType.ODOS); }
    function _getScaledInputData(SwapType swapType) internal {
        if (swapType == SwapType.ODOS) { _odosScaling(); } else { assert(false); }
    }
    function _authorizeUpgrade(address) internal onlyOwner {}
}
"""
_NEW_SOURCE = """
contract PendleSwap {
    function swap() external { _zeroExSwap(); }
    function _zeroExSwap() internal { _transferOut(tokenOut, msg.sender, netOut); }
    function _getScaledInputData(SwapType swapType) internal { assert(false); }
    function _authorizeUpgrade(address) internal onlyOwner {}
}
"""
_OWNERSHIP_SOURCE = """
abstract contract BoringOwnableUpgradeableV2 {
    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
}
"""


def _record(name: str) -> VerifiedContract:
    """Build only the source declarations the adapter consumes, without cache metadata."""
    swap_types = "NONE, KYBERSWAP, RESERVE_1, ZEROX" if name == "new" else "NONE, KYBERSWAP, ODOS, RESERVE_2"
    return VerifiedContract(
        contract_name="PendleSwap",
        compiler_version="",
        language="Solidity",
        contract_file="PendleSwap.sol",
        sources={
            "PendleSwap.sol": _NEW_SOURCE if name == "new" else _OLD_SOURCE,
            "IPSwapAggregator.sol": (
                "struct SwapData { SwapType swapType; address extRouter; bytes extCalldata; bool needScale; }\n"
                f"enum SwapType {{ {swap_types} }}\ninterface IPSwapAggregator {{}}"
            ),
            "BoringOwnableUpgradeableV2.sol": _OWNERSHIP_SOURCE,
        },
    )


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
    resolved = resolve_protocol_context("PENDLE", chain_id, [(PENDLE_SWAP.lower(), _upgrade())])
    prompt = resolved.prompt
    assert "not a live trace" in prompt
    assert f"Current proxy owner() (live read): {OWNER}" in prompt
    assert "_authorizeUpgrade(address) internal onlyOwner" in prompt
    assert 'require(msg.sender == owner, "not owner")' in prompt
    before, after = prompt.split("Proposed implementation evidence:")
    assert "SwapType.ODOS" in before
    assert "RESERVE_2" in before
    assert "RESERVE_1" in after
    assert "ZEROX" in after
    assert "SwapType.ODOS" not in after
    assert "assert(false)" in after
    assert "_transferOut(tokenOut, msg.sender, netOut)" in after
    explorer = "etherscan.io" if chain_id == 1 else "arbiscan.io"
    for address in (PENDLE_SWAP, OLD, NEW, OWNER):
        assert address in resolved.addresses
        assert f"https://{explorer}/address/{address}" in resolved.report
    assert resolved.labels[PENDLE_SWAP] == "PendleSwap"
    contract_call = boundaries[2].return_value.eth.contract.call_args
    assert contract_call.kwargs["address"] == PENDLE_SWAP


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


def test_missing_source_and_implementation_are_explicit(boundaries: Boundaries) -> None:
    """Missing evidence never turns into a guessed old implementation or safe-storage claim."""
    boundaries[0].return_value = None
    boundaries[1].side_effect = None
    boundaries[1].return_value = None
    contexts = resolve_pendle_context("pendle", 1, [(PENDLE_SWAP, _upgrade())])
    prompt = format_pendle_prompt(contexts)
    assert "Current implementation: unavailable" in prompt
    assert prompt.count("target source unavailable") == 2


def test_failed_batch_member_does_not_hide_next_upgrade(boundaries: Boundaries) -> None:
    """Source/RPC errors remain local to a batch member."""
    boundaries[0].side_effect = [RuntimeError("RPC failed"), OLD]
    contexts = resolve_pendle_context("pendle", 1, [(PENDLE_SWAP, _upgrade()), (PENDLE_SWAP, _upgrade(True))])
    assert len(contexts) == 1
    assert contexts[0].is_upgrade_and_call


@pytest.mark.parametrize("flattened", [False, True])
def test_source_excerpts_exclude_unrelated_contracts(flattened: bool) -> None:
    """Only the deployed target and relevant declarations belong in context, in either source format."""
    record = _record("new")
    sources = dict(record.sources)
    sources["PendleSwap.sol"] += '\ncontract Decoy { function swap() external { revert("DECOY"); } }'
    if flattened:
        record = replace(record, sources={"flat.sol": "\n".join(sources.values())}, contract_file="flat.sol")
    else:
        record = replace(record, sources=sources)
    evidence = _source_evidence(record)
    assert "DECOY" not in evidence
    assert "enum SwapType" in evidence
    assert "struct SwapData" in evidence
    assert "_zeroExSwap" in evidence
    assert 'require(msg.sender == owner, "not owner")' in evidence


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
