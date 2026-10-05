"""Tests for the allowlist-scope (permission-grant) protocol-context adapter."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm import permission_grant_context
from utils.llm.control_transfer_context import ControllerInfo
from utils.llm.permission_grant_context import (
    format_permission_grant_prompt,
    format_permission_grant_report,
    resolve_permission_grant_context,
)
from utils.verified_contract import VerifiedContract

RELAYER = "0x604e586F17cE106B64185A7a0d2c1Da5bAce711E"
MULTICALL = "0x4E440bbC8D0a63fb53791fCb0375DF8E58f41567"
BRAIN = "0x16388463d60FFE0661Cf7F1f31a7D658aC790ff7"
EXECUTOR = "0xF8f60BF9456A6e0141149Db2DD6f02C60da5779B"
OPS_EOA = "0x1b5f15DCb82d25f91c65b53CEe151E8b9fBdD271"
HARVESTER = "0x00000000000000000000000000000000000000A1"
DENY_VAULT = "0x00000000000000000000000000000000000000A2"

# yHaaSRelayer as verified on mainnet, trimmed to the members that matter here.
RELAYER_SOURCE = """
pragma solidity ^0.8.18;

contract yHaaSRelayer {
    address public owner;
    address public governance;

    mapping(address => bool) public keepers;

    function harvestStrategy(address _strategyAddress) public onlyKeepers returns (uint256 profit, uint256 loss) {
        (profit, loss) = StrategyAPI(_strategyAddress).report();
    }

    function tendStrategy(address _strategyAddress) public onlyKeepers {
        StrategyAPI(_strategyAddress).tend();
    }

    function forwardCall(address debtAllocatorAddress, bytes memory data) public onlyKeepers returns (bool success) {
        (success, ) = debtAllocatorAddress.call(data);
    }

    function setKeeper(address _address, bool _allowed) external virtual onlyAuthorized {
        keepers[_address] = _allowed;
    }

    modifier onlyKeepers() {
        require(msg.sender == owner || keepers[msg.sender] == true || msg.sender == governance, "!keeper yHaaSProxy");
        _;
    }

    modifier onlyAuthorized() {
        require(msg.sender == owner || msg.sender == governance, "!authorized");
        _;
    }
}

interface StrategyAPI {
    function tend() external;
    function report() external returns (uint256 _profit, uint256 _loss);
}
"""

# Yearn's TimelockExecutor: an EnumerableSet allowlist checked through `isExecutor(msg.sender)`,
# with `onlyGovernance` inherited from a base in another file.
EXECUTOR_SOURCE = """
pragma solidity ^0.8.0;

import {Governance} from "@periphery/utils/Governance.sol";

contract TimelockExecutor is Governance {
    using EnumerableSet for EnumerableSet.AddressSet;

    modifier onlyExecutor() {
        require(isExecutor(msg.sender), "TimelockExecutor: not executor");
        _;
    }

    EnumerableSet.AddressSet private _executors;

    function executeBatch(
        address[] calldata targets,
        uint256[] calldata values,
        bytes[] calldata payloads,
        bytes32 predecessor,
        bytes32 salt
    ) external onlyExecutor {
        TIMELOCK.executeBatch(targets, values, payloads, predecessor, salt);
    }

    function isExecutor(address executor) public view returns (bool) {
        return _executors.contains(executor);
    }

    function getExecutors() public view returns (address[] memory) {
        return _executors.values();
    }

    function removeExecutor(address executor) external onlyGovernance {
        require(isExecutor(executor), "TimelockExecutor: not executor");
        _executors.remove(executor);
        emit ExecutorRemoved(executor);
    }
}
"""

GOVERNANCE_SOURCE = """
pragma solidity >=0.8.18;

contract Governance {
    modifier onlyGovernance() {
        _checkGovernance();
        _;
    }

    function _checkGovernance() internal view virtual {
        require(governance == msg.sender, "!governance");
    }

    address public governance;
}
"""

MULTICALL_SOURCE = """
pragma solidity 0.8.23;

contract TKSRelayerMulticall is Multicall {
    address public owner;
    mapping(address => bool) public keepers;

    modifier onlyKeepers() {
        require(keepers[msg.sender] || msg.sender == owner, "!keeper");
        _;
    }

    function forwardCall(address _target, bytes calldata _data) external onlyKeepers {
        Address.functionCall(_target, _data);
    }
}
"""

# A forwarder pinned to one target and one selector: not an arbitrary call.
HARVESTER_SOURCE = """
pragma solidity 0.8.23;

contract Harvester {
    address public strategy;
    mapping(address => bool) public keepers;

    modifier onlyKeepers() {
        require(keepers[msg.sender], "!keeper");
        _;
    }

    function forward(address _target, bytes calldata _data) external onlyKeepers {
        require(_target == strategy, "!strategy");
        require(bytes4(_data) == IStrategy.report.selector, "!report");
        (bool ok, ) = _target.call(_data);
        require(ok);
    }

    function setKeeper(address _keeper, bool _allowed) external {
        keepers[_keeper] = _allowed;
    }
}
"""

# A denylist: a listed caller is refused.
DENY_SOURCE = """
pragma solidity 0.8.23;

contract DenyVault {
    mapping(address => bool) public blocked;

    function withdraw(uint256 amount) external {
        require(!blocked[msg.sender], "blocked");
    }

    function setBlocked(address account, bool isBlocked) external {
        blocked[account] = isBlocked;
    }
}
"""

GET_EXECUTORS_ABI = {
    "type": "function",
    "name": "getExecutors",
    "stateMutability": "view",
    "inputs": [],
    "outputs": [{"name": "", "type": "address[]"}],
}


def _verified(name: str, sources: dict[str, str], abi: list[dict] | None = None) -> VerifiedContract:
    return VerifiedContract(
        contract_name=name,
        compiler_version="v0.8.23",
        language="Solidity",
        sources=sources,
        abi=abi or [],
        contract_file=next(iter(sources)),
    )


VERIFIED = {
    RELAYER: _verified("yHaaSRelayer", {"yHaaSRelayer.sol": RELAYER_SOURCE}),
    EXECUTOR: _verified(
        "TimelockExecutor",
        {"src/TimelockExecutor.sol": EXECUTOR_SOURCE, "lib/Governance.sol": GOVERNANCE_SOURCE},
        [GET_EXECUTORS_ABI],
    ),
    MULTICALL: _verified("TKSRelayerMulticall", {"TKSRelayerMulticall.sol": MULTICALL_SOURCE}),
    HARVESTER: _verified("Harvester", {"Harvester.sol": HARVESTER_SOURCE}),
    DENY_VAULT: _verified("DenyVault", {"DenyVault.sol": DENY_SOURCE}),
}

HOLDERS = {
    MULTICALL: ControllerInfo(
        address=MULTICALL,
        kind="contract",
        label="TKSRelayerMulticall",
        controller_getter="owner",
        controlled_by=ControllerInfo(address=BRAIN, kind="safe", threshold=3, owner_count=7),
    ),
    OPS_EOA: ControllerInfo(address=OPS_EOA, kind="eoa"),
    BRAIN: ControllerInfo(address=BRAIN, kind="safe", threshold=3, owner_count=8),
}


def _set_keeper() -> DecodedCall:
    return DecodedCall("setKeeper", "setKeeper(address,bool)", [("address", MULTICALL), ("bool", True)])


def _remove_executor() -> DecodedCall:
    return DecodedCall("removeExecutor", "removeExecutor(address)", [("address", OPS_EOA)])


@patch.object(permission_grant_context, "get_contract_label", side_effect=lambda chain_id, a: VERIFIED[a].contract_name)
@patch.object(permission_grant_context, "describe_controller", side_effect=lambda chain_id, client, a: HOLDERS[a])
@patch.object(permission_grant_context, "fetch_verified_contract", side_effect=lambda chain_id, a: VERIFIED.get(a))
@patch.object(permission_grant_context, "_client")
class TestResolve(unittest.TestCase):
    def test_keeper_grant_lists_scope_and_holder_forwarders(self, mock_client: MagicMock, *_mocks: MagicMock) -> None:
        (context,) = resolve_permission_grant_context("YEARN_MS", 1, [(RELAYER, _set_keeper())])
        prompt = format_permission_grant_prompt([context])
        self.assertIn(f"setKeeper(address,bool) on {RELAYER} (yHaaSRelayer) GRANTS {MULTICALL}", prompt)
        self.assertIn("modifier onlyKeepers gates harvestStrategy(address), tendStrategy(address)", prompt)
        self.assertIn("ARBITRARY CALL: forwardCall(address,bytes) (call)", prompt)
        self.assertIn("can use every permission yHaaSRelayer itself holds", prompt)
        self.assertIn("its owner() is", prompt)
        self.assertIn("forwardCall(address,bytes) [onlyKeepers]", prompt)
        mock_client.return_value.get_contract.assert_not_called()  # a mapping has no member getter

        report = format_permission_grant_report([context], 1, {})
        self.assertIn("`forwardCall(address,bytes)` (modifier onlyKeepers) — **arbitrary call**", report)

    def test_restricted_forward_is_not_reported_as_arbitrary(self, mock_client: MagicMock, *_mocks: MagicMock) -> None:
        call = DecodedCall("setKeeper", "setKeeper(address,bool)", [("address", OPS_EOA), ("bool", True)])
        (context,) = resolve_permission_grant_context("YEARN_MS", 1, [(HARVESTER, call)])
        prompt = format_permission_grant_prompt([context])
        self.assertNotIn("ARBITRARY CALL", prompt)
        self.assertIn("RESTRICTED FORWARD: forward(address,bytes) checks", prompt)
        self.assertIn('`require(_target == strategy, "!strategy")`', prompt)
        self.assertIn("`require(bytes4(_data) == IStrategy.report.selector", prompt)
        report = format_permission_grant_report([context], 1, {})
        self.assertNotIn("every permission", report)
        self.assertIn("forwards calls, restricted by", report)

    def test_denylist_entry_blocks_instead_of_granting(self, mock_client: MagicMock, *_mocks: MagicMock) -> None:
        call = DecodedCall("setBlocked", "setBlocked(address,bool)", [("address", OPS_EOA), ("bool", True)])
        (context,) = resolve_permission_grant_context("YEARN_MS", 1, [(DENY_VAULT, call)])
        self.assertIn(f"BLOCKS {OPS_EOA}", context.lines()[0])
        self.assertIn("gates withdraw(uint256)", context.lines()[1])
        self.assertIn("a denylist: listed callers are refused", context.lines()[1])
        report = format_permission_grant_report([context], 1, {})
        self.assertIn("**Entry points it closes:**", report)
        self.assertNotIn("opens", report)

    def test_executor_removals_carry_membership_through_the_batch(
        self, mock_client: MagicMock, *_mocks: MagicMock
    ) -> None:
        mock_client.return_value.get_contract.return_value.functions.__getitem__.return_value.return_value.call.return_value = [
            BRAIN,
            OPS_EOA,
        ]
        remove_brain = DecodedCall("removeExecutor", "removeExecutor(address)", [("address", BRAIN)])
        first, second = resolve_permission_grant_context(
            "YEARN_MS", 1, [(EXECUTOR, _remove_executor()), (EXECUTOR, remove_brain)]
        )
        lines = first.lines()
        self.assertIn(f"REVOKES {OPS_EOA} an entry in `_executors`", lines[0])
        # onlyExecutor → isExecutor(msg.sender) → _executors.contains, with onlyGovernance from another file.
        self.assertIn("modifier onlyExecutor (helper isExecutor) gates executeBatch(", lines[1])
        self.assertNotIn("ARBITRARY CALL", "\n".join(lines))
        self.assertEqual((first.holders_before, first.holders_after()), ((BRAIN, OPS_EOA), (BRAIN,)))
        self.assertEqual((second.holders_before, second.holders_after()), ((BRAIN,), ()))
        self.assertIn("members before: " + BRAIN + "; after: none.", second.lines()[-1])

    def test_ignores_non_setters_without_source_lookups(self, mock_client: MagicMock, *_mocks: MagicMock) -> None:
        call = DecodedCall("transfer", "transfer(address,uint256)", [("address", OPS_EOA), ("uint256", 1)])
        self.assertEqual(resolve_permission_grant_context("YEARN_MS", 1, [(RELAYER, call)]), [])
        mock_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
