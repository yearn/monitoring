"""Tests for the control-transfer protocol-context adapter."""

import unittest
from collections.abc import Mapping
from unittest.mock import MagicMock, patch

from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector

from utils.calldata.decoder import DecodedCall
from utils.llm import control_transfer_context
from utils.llm.control_transfer_context import (
    ControllerInfo,
    ControlTransferContext,
    format_control_transfer_prompt,
    format_control_transfer_report,
    resolve_control_transfer_context,
)

TARGET = "0xbCc932e4750C3E465A7E54A06A34F9EdF8f6116b"  # Funding Distributor
YCHAD = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"  # 6-of-9 Safe
EXECUTOR = "0xac7D4A37Ba61C2CAc7f64d3e2b5773D85613Fe7b"  # contract managed by a 2-of-4 Safe
EXEC_SAFE = "0xABCDEF0028B6Cc3539C2397aCab017519f8744c8"
EOA = "0x55Bc991b2edF3DDb4c520B222bE4F378418ff0fA"


def _sel(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


def _chain(responses: dict[tuple[str, str], bytes], code: Mapping[str, bytes]) -> MagicMock:
    """Mock client: eth_call answers (address, signature) pairs; anything else reverts."""
    by_selector = {(address.lower(), _sel(sig)): raw for (address, sig), raw in responses.items()}

    def call(tx: dict) -> bytes:
        key = (tx["to"].lower(), tx["data"])
        if key not in by_selector:
            raise ValueError("execution reverted")
        return by_selector[key]

    client = MagicMock()
    client.eth.call.side_effect = call
    client.eth.get_code.side_effect = lambda address: code.get(address.lower(), b"")
    return client


def _safe(address: str, threshold: int, owners: int) -> dict[tuple[str, str], bytes]:
    return {
        (address, "getThreshold()"): abi_encode(["uint256"], [threshold]),
        (address, "getOwners()"): abi_encode(["address[]"], [[f"0x{i + 1:040x}" for i in range(owners)]]),
    }


def _funding_distributor_chain() -> MagicMock:
    responses = {
        (TARGET, "management()"): abi_encode(["address"], [YCHAD]),
        (TARGET, "pending_management()"): abi_encode(["address"], ["0x" + "00" * 20]),
        (EXECUTOR, "management()"): abi_encode(["address"], [EXEC_SAFE]),
        **_safe(YCHAD, 6, 9),
        **_safe(EXEC_SAFE, 2, 4),
    }
    code: dict[str, bytes] = {a.lower(): b"\x60\x80" for a in (TARGET, YCHAD, EXECUTOR, EXEC_SAFE)}
    return _chain(responses, code)


SET_MANAGEMENT = DecodedCall("set_management", "set_management(address)", [("address", EXECUTOR)])


# Vyper setter that only writes the pending slot (Funding Distributor, yETH claim).
NOMINATING_VYPER = """
pending_management: public(address)
management: public(address)

@external
def set_management(_management: address):
    assert msg.sender == self.management
    self.pending_management = _management
"""


@patch.object(control_transfer_context, "get_contract_label", return_value="")
class TestResolve(unittest.TestCase):
    @patch.object(control_transfer_context, "resolve_function_source", return_value=NOMINATING_VYPER)
    @patch.object(control_transfer_context, "exposes", side_effect=lambda _c, address, _w: address == EXECUTOR)
    @patch.object(control_transfer_context, "ChainManager")
    def test_management_to_contract_behind_smaller_safe(
        self, mock_cm: MagicMock, _exposes: MagicMock, _source: MagicMock, _label: MagicMock
    ) -> None:
        mock_cm.get_client.return_value = _funding_distributor_chain()

        (context,) = resolve_control_transfer_context("YEARN_MS", 1, [(TARGET, SET_MANAGEMENT)])

        assert context.current is not None
        self.assertEqual((context.current.kind, context.current.threshold, context.current.owner_count), ("safe", 6, 9))
        self.assertEqual(context.proposed.kind, "contract")
        self.assertTrue(context.proposed.has_operators)
        assert context.proposed.controlled_by is not None
        self.assertEqual(context.proposed.controlled_by.address, EXEC_SAFE)
        self.assertEqual(context.proposed.safe_threshold, (2, 4))
        self.assertTrue(context.two_step)
        self.assertIn("6-of-9 to 2-of-4 — a LOWER signing threshold", context.threshold_change())

    @patch.object(control_transfer_context, "exposes", return_value=False)
    @patch.object(control_transfer_context, "ChainManager")
    def test_transfer_to_eoa_is_single_key(self, mock_cm: MagicMock, _exposes: MagicMock, _label: MagicMock) -> None:
        responses = {(TARGET, "owner()"): abi_encode(["address"], [YCHAD]), **_safe(YCHAD, 6, 9)}
        mock_cm.get_client.return_value = _chain(responses, {TARGET.lower(): b"\x60", YCHAD.lower(): b"\x60"})
        call = DecodedCall("transferOwnership", "transferOwnership(address)", [("address", EOA)])

        (context,) = resolve_control_transfer_context("aave", 1, [(TARGET, call)])

        self.assertEqual(context.role, "owner")
        self.assertEqual(context.proposed.kind, "eoa")
        self.assertFalse(context.two_step)
        self.assertIn("an EOA (single private key)", format_control_transfer_prompt([context]))

    @patch.object(control_transfer_context, "exposes", return_value=False)
    @patch.object(control_transfer_context, "ChainManager")
    def test_eip7702_delegated_account(self, mock_cm: MagicMock, _exposes: MagicMock, _label: MagicMock) -> None:
        code: dict[str, bytes] = {EOA.lower(): bytes.fromhex("ef0100" + "11" * 20)}
        mock_cm.get_client.return_value = _chain({}, code)
        call = DecodedCall("grantRole", "grantRole(bytes32,address)", [("bytes32", b"\x01" * 32), ("address", EOA)])

        (context,) = resolve_control_transfer_context("x", 1, [(TARGET, call)])

        self.assertEqual(context.proposed.kind, "eip7702")
        self.assertEqual(context.role_hash, "0x" + "01" * 32)
        self.assertIsNone(context.current)

    @patch.object(control_transfer_context, "ChainManager")
    def test_unrelated_calls_skip_rpc(self, mock_cm: MagicMock, _label: MagicMock) -> None:
        call = DecodedCall("withdraw", "withdraw(uint256,address,address)", [("uint256", 1)])
        self.assertEqual(resolve_control_transfer_context("x", 1, [(TARGET, call)]), [])
        mock_cm.get_client.assert_not_called()

    @patch.object(control_transfer_context, "exposes", return_value=False)
    @patch.object(control_transfer_context, "ChainManager")
    def test_zero_address_renounces(self, mock_cm: MagicMock, _exposes: MagicMock, _label: MagicMock) -> None:
        mock_cm.get_client.return_value = _chain({}, {})
        call = DecodedCall("set_management", "set_management(address)", [("address", "0x" + "00" * 20)])
        (context,) = resolve_control_transfer_context("x", 1, [(TARGET, call)])
        self.assertEqual(context.proposed.kind, "none")
        self.assertIn("control is renounced", context.proposed.describe())


# BoringOwnable: `direct` decides between an immediate transfer and a nomination.
BORING_OWNABLE = """
contract BoringOwnable {
    address public owner;
    address public pendingOwner;
    function transferOwnership(address newOwner, bool direct, bool renounce) public onlyOwner {
        if (direct) {
            owner = newOwner;
            pendingOwner = address(0);
        } else {
            pendingOwner = newOwner;
        }
    }
}
"""

OZ_OWNABLE_2STEP = """
abstract contract Ownable2Step is Ownable {
    address private _pendingOwner;
    function pendingOwner() public view virtual returns (address) { return _pendingOwner; }
    function transferOwnership(address newOwner) public virtual override onlyOwner {
        _pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner(), newOwner);
    }
}
"""


def _owned_chain(pending: bool = True) -> MagicMock:
    """A target whose owner is yChad, optionally exposing ``pendingOwner()``."""
    responses = {(TARGET, "owner()"): abi_encode(["address"], [YCHAD]), **_safe(YCHAD, 6, 9)}
    if pending:
        responses[(TARGET, "pendingOwner()")] = abi_encode(["address"], ["0x" + "00" * 20])
    return _chain(responses, {TARGET.lower(): b"\x60", YCHAD.lower(): b"\x60"})


@patch.object(control_transfer_context, "exposes", return_value=False)
@patch.object(control_transfer_context, "get_contract_label", return_value="")
@patch.object(control_transfer_context, "ChainManager")
class TestTwoStepFromTheCall(unittest.TestCase):
    """A pending slot on the target does not make every transfer two-step."""

    def _resolve(self, call: DecodedCall) -> ControlTransferContext:
        (context,) = resolve_control_transfer_context("x", 1, [(TARGET, call)])
        return context

    def _boring(self, direct: bool) -> DecodedCall:
        return DecodedCall(
            "transferOwnership",
            "transferOwnership(address,bool,bool)",
            [("address", EOA), ("bool", direct), ("bool", False)],
        )

    def test_boring_ownable_direct_transfer_is_immediate(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        mock_cm.get_client.return_value = _owned_chain()
        with patch.object(control_transfer_context, "resolve_function_source", return_value=BORING_OWNABLE):
            context = self._resolve(self._boring(direct=True))
        self.assertIs(context.two_step, False)
        self.assertTrue(context.pending_slot)
        prompt = format_control_transfer_prompt([context])
        self.assertIn("Immediate: this call transfers control directly", prompt)
        self.assertNotIn("only nominates", prompt)

    def test_boring_ownable_claimable_transfer_nominates(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        mock_cm.get_client.return_value = _owned_chain()
        context = self._resolve(self._boring(direct=False))
        self.assertIs(context.two_step, True)

    def test_nominate_only_setter(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        mock_cm.get_client.return_value = _owned_chain()
        context = self._resolve(DecodedCall("setPendingOwner", "setPendingOwner(address)", [("address", EOA)]))
        self.assertIs(context.two_step, True)

    def test_oz_ownable2step_private_pending_slot(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        mock_cm.get_client.return_value = _owned_chain()
        call = DecodedCall("transferOwnership", "transferOwnership(address)", [("address", EOA)])
        with patch.object(control_transfer_context, "resolve_function_source", return_value=OZ_OWNABLE_2STEP):
            context = self._resolve(call)
        self.assertIs(context.two_step, True)

    def test_unreadable_source_is_undetermined(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        mock_cm.get_client.return_value = _owned_chain()
        call = DecodedCall("transferOwnership", "transferOwnership(address)", [("address", EOA)])
        with patch.object(control_transfer_context, "resolve_function_source", return_value=None):
            context = self._resolve(call)
        self.assertIsNone(context.two_step)
        prompt = format_control_transfer_prompt([context])
        self.assertIn("could not be determined", prompt)
        self.assertNotIn("only nominates", prompt)
        self.assertNotIn("Effective once accepted", prompt)

    def test_no_pending_slot_is_direct_without_source(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        mock_cm.get_client.return_value = _owned_chain(pending=False)
        call = DecodedCall("transferOwnership", "transferOwnership(address)", [("address", EOA)])
        with patch.object(control_transfer_context, "resolve_function_source") as mock_source:
            context = self._resolve(call)
        self.assertIs(context.two_step, False)
        self.assertEqual(context.timing_note(), "")
        mock_source.assert_not_called()


class TestRendering(unittest.TestCase):
    CONTEXT = ControlTransferContext(
        target=TARGET,
        target_label="Funding Distributor",
        signature="set_management(address)",
        role="management",
        current=ControllerInfo(address=YCHAD, kind="safe", label="yChad", threshold=6, owner_count=9),
        proposed=ControllerInfo(
            address=EXECUTOR,
            kind="contract",
            label="Executor",
            controller_getter="management",
            controlled_by=ControllerInfo(address=EXEC_SAFE, kind="safe", threshold=2, owner_count=4),
            has_operators=True,
        ),
        two_step=True,
        pending_slot=True,
    )

    def test_prompt(self) -> None:
        prompt = format_control_transfer_prompt([self.CONTEXT])
        self.assertIn(f"Current management: {YCHAD} (yChad) — Safe multisig, 6-of-9 owners.", prompt)
        self.assertIn(f"its management() is {EXEC_SAFE} — Safe multisig, 2-of-4 owners", prompt)
        self.assertIn("operator whitelist", prompt)
        self.assertIn("Two-step", prompt)
        self.assertIn("a LOWER signing threshold. Effective once accepted.", prompt)

    def test_report_links_every_controller(self) -> None:
        report = format_control_transfer_report([self.CONTEXT], 1, {})
        for address in (TARGET, YCHAD, EXECUTOR, EXEC_SAFE):
            self.assertIn(f"https://etherscan.io/address/{address}", report)
        self.assertIn("**2-of-4**", report)
        self.assertIn("- **Two-step:** this call only nominates", report)

    def test_labels_and_addresses_include_the_hop(self) -> None:
        self.assertEqual(self.CONTEXT.labels, {YCHAD: "Safe 6-of-9", EXEC_SAFE: "Safe 2-of-4"})
        self.assertEqual(self.CONTEXT.addresses, [TARGET, YCHAD, EXECUTOR, EXEC_SAFE])


if __name__ == "__main__":
    unittest.main()
