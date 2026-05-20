"""Tests for utils/impl_diff.py."""

import unittest
from unittest.mock import patch

from utils.impl_diff import (
    FunctionSig,
    ImplDiff,
    StateVarDecl,
    _diff_functions,
    _extract_function_sigs,
    _extract_state_vars,
    _is_namespaced_storage,
    _normalize_args,
    _storage_layout,
    diff_implementations,
    format_impl_diff,
)

CONTRACT_OLD = """
// SPDX-License-Identifier: MIT
contract Vault {
    uint256 public minDeposit;
    address public owner;
    uint256 public constant FEE_BPS = 30;

    function deposit(uint256 _amount) external returns (uint256) { return _amount; }
    function withdraw(uint256 _amount) external onlyOwner { _amount; }
    function setOwner(address _o) external onlyOwner { owner = _o; }
}
"""

CONTRACT_NEW = """
// SPDX-License-Identifier: MIT
contract Vault {
    uint256 public minDeposit;
    address public owner;
    uint256 public maxDeposit;             // NEW state var appended at end
    uint256 public constant FEE_BPS = 30;

    function deposit(uint256 _amount) external returns (uint256) { return _amount; }
    function withdraw(uint256 _amount) external onlyOwner { _amount; }
    function setOwner(address _o) external onlyAdmin { owner = _o; }   // modifier changed
    function setMaxDeposit(uint256 _m) external onlyOwner { maxDeposit = _m; }  // new
}
"""

CONTRACT_REORDERED = """
contract Bad {
    address public owner;        // SWAPPED with minDeposit — unsafe
    uint256 public minDeposit;
}
"""

CONTRACT_NAMESPACED = """
contract NS {
    function _getXxxStorage() private pure returns (XxxStorage storage $) {
        assembly { $.slot := 0x1234 }
    }
}
"""


class TestNormalizeArgs(unittest.TestCase):
    def test_strips_names(self) -> None:
        self.assertEqual(_normalize_args("address _a, uint256 _b"), "address,uint256")

    def test_strips_data_locations(self) -> None:
        self.assertEqual(_normalize_args("uint256[] memory arr, bytes calldata data"), "uint256[],bytes")

    def test_empty(self) -> None:
        self.assertEqual(_normalize_args(""), "")
        self.assertEqual(_normalize_args(" "), "")


class TestExtractFunctionSigs(unittest.TestCase):
    def test_finds_all(self) -> None:
        sigs = _extract_function_sigs(CONTRACT_OLD)
        names = [s.name for s in sigs]
        self.assertIn("deposit", names)
        self.assertIn("withdraw", names)
        self.assertIn("setOwner", names)

    def test_captures_visibility_and_modifiers(self) -> None:
        sigs = {s.name: s for s in _extract_function_sigs(CONTRACT_OLD)}
        self.assertEqual(sigs["setOwner"].visibility, "external")
        self.assertIn("onlyOwner", sigs["setOwner"].modifiers)


class TestExtractStateVars(unittest.TestCase):
    def test_finds_in_order(self) -> None:
        vars_ = _extract_state_vars(CONTRACT_OLD)
        names = [v.name for v in vars_]
        # First two are slot vars in declaration order
        self.assertEqual(names[:2], ["minDeposit", "owner"])
        self.assertIn("FEE_BPS", names)

    def test_marks_constant_as_immutable(self) -> None:
        vars_ = {v.name: v for v in _extract_state_vars(CONTRACT_OLD)}
        self.assertTrue(vars_["FEE_BPS"].immutable)
        self.assertFalse(vars_["minDeposit"].immutable)

    def test_default_internal_state_vars_captured(self) -> None:
        """Regression: Solidity defaults state-var visibility to internal.
        Declarations like `uint256 cap;` were previously skipped because the
        regex required an explicit modifier — that produced a false-safe verdict
        when an upgrade removed or reordered such vars."""
        src = """
        contract C {
            uint256 explicitPublic;  // wait, no — explicit visibility test below
            uint256 cap;             // default internal, NO visibility
            address admin;           // default internal, NO visibility
            mapping(address => uint256) balances;  // default internal mapping
        }
        """
        vars_ = _extract_state_vars(src)
        names = [v.name for v in vars_]
        self.assertEqual(names, ["explicitPublic", "cap", "admin", "balances"])
        # The default-visibility ones should record visibility as ""
        by_name = {v.name: v for v in vars_}
        self.assertEqual(by_name["cap"].visibility, "")
        self.assertEqual(by_name["admin"].visibility, "")
        self.assertEqual(by_name["balances"].visibility, "")

    def test_function_locals_not_captured_after_visibility_fix(self) -> None:
        """Even with visibility now optional, locals inside function bodies must
        be excluded via the brace-depth check."""
        src = """
        contract C {
            uint256 stateVar;
            function f() external {
                uint256 localUint = 1;
                address localAddr;
                if (true) {
                    uint256 deeper = 2;
                }
            }
        }
        """
        names = [v.name for v in _extract_state_vars(src)]
        self.assertEqual(names, ["stateVar"])
        self.assertNotIn("localUint", names)
        self.assertNotIn("localAddr", names)
        self.assertNotIn("deeper", names)

    def test_struct_members_not_captured(self) -> None:
        """Struct members are at brace depth 2 inside the struct, not state vars."""
        src = """
        contract C {
            struct Cfg { uint256 fee; address admin; }
            uint256 stateVar;
        }
        """
        names = [v.name for v in _extract_state_vars(src)]
        self.assertNotIn("fee", names)
        self.assertNotIn("admin", names)
        self.assertIn("stateVar", names)

    def test_removing_default_internal_var_now_detected_as_unsafe(self) -> None:
        """End-to-end: an upgrade that removes a default-internal var must be
        flagged as unsafe, not silently treated as no-change."""
        old = "contract C { uint256 a; uint256 b; uint256 c; }"
        new = "contract C { uint256 a; uint256 c; }"  # b removed, c shifts up
        old_vars = _extract_state_vars(old)
        new_vars = _extract_state_vars(new)
        from utils.impl_diff import _storage_layout

        safe, changes, _, _ = _storage_layout(old_vars, new_vars)
        self.assertFalse(safe)
        self.assertTrue(changes, "expected concrete layout changes")


class TestDiffFunctions(unittest.TestCase):
    def test_detects_added_and_changed(self) -> None:
        old = _extract_function_sigs(CONTRACT_OLD)
        new = _extract_function_sigs(CONTRACT_NEW)
        added, removed, changed = _diff_functions(old, new)

        added_names = [f.name for f in added]
        self.assertIn("setMaxDeposit", added_names)
        self.assertEqual(removed, [])

        changed_names = [(o.name, n.name) for o, n in changed]
        self.assertIn(("setOwner", "setOwner"), changed_names)


class TestStorageLayout(unittest.TestCase):
    def test_append_only_is_safe(self) -> None:
        old_vars = _extract_state_vars(CONTRACT_OLD)
        new_vars = _extract_state_vars(CONTRACT_NEW)
        safe, changes, added, removed = _storage_layout(old_vars, new_vars)
        self.assertTrue(safe)
        self.assertEqual(changes, [])
        added_names = [v.name for v in added]
        self.assertEqual(added_names, ["maxDeposit"])

    def test_reorder_is_unsafe(self) -> None:
        old_vars = _extract_state_vars(CONTRACT_OLD)
        bad_vars = _extract_state_vars(CONTRACT_REORDERED)
        safe, changes, _, _ = _storage_layout(old_vars, bad_vars)
        self.assertFalse(safe)
        self.assertTrue(any("slot 0" in c for c in changes))

    def test_consuming_one_gap_slot_is_safe(self) -> None:
        """Canonical OZ pattern: append a new state var by shrinking the trailing gap."""
        old = _extract_state_vars("contract C { uint256 a; uint256[50] __gap; }")
        new = _extract_state_vars("contract C { uint256 a; uint256 b; uint256[49] __gap; }")
        safe, changes, added, _ = _storage_layout(old, new)
        self.assertTrue(safe, f"expected safe gap consumption, got changes={changes}")
        self.assertEqual([v.name for v in added], ["b"])

    def test_consuming_multiple_gap_slots_is_safe(self) -> None:
        old = _extract_state_vars("contract C { uint256 a; uint256[50] __gap; }")
        new = _extract_state_vars("contract C { uint256 a; uint256 b; address c; uint48 d; uint256[47] __gap; }")
        safe, changes, _, _ = _storage_layout(old, new)
        self.assertTrue(safe, f"expected safe multi-slot consumption, got changes={changes}")

    def test_gap_size_underflow_is_unsafe(self) -> None:
        """If the new contract consumes MORE slots than the old gap reserved."""
        old = _extract_state_vars("contract C { uint256 a; uint256[2] __gap; }")
        new = _extract_state_vars("contract C { uint256 a; uint256 b; uint256 c; uint256 d; }")
        safe, changes, _, _ = _storage_layout(old, new)
        self.assertFalse(safe)
        self.assertTrue(any("overflow" in c for c in changes))

    def test_gap_not_shrunk_correctly_is_unsafe(self) -> None:
        """Consumed 1 slot but gap kept its original size — slots[2..] now shifted."""
        old = _extract_state_vars("contract C { uint256 a; uint256[50] __gap; }")
        new = _extract_state_vars("contract C { uint256 a; uint256 b; uint256[50] __gap; }")
        safe, changes, _, _ = _storage_layout(old, new)
        self.assertFalse(safe)
        self.assertTrue(any("gap mismatch" in c for c in changes))

    def test_fully_consumed_gap_removed_is_safe(self) -> None:
        """Old had a 1-slot gap; new fills it and removes the gap entirely."""
        old = _extract_state_vars("contract C { uint256 a; uint256[1] __gap; }")
        new = _extract_state_vars("contract C { uint256 a; uint256 b; }")
        safe, changes, _, _ = _storage_layout(old, new)
        self.assertTrue(safe, f"expected safe full consumption, got changes={changes}")

    def test_gap_removed_without_consumption_is_unsafe(self) -> None:
        """Removing a gap without filling it changes the slot count and is unsafe
        if any inheriting contract assumed it would still be there."""
        old = _extract_state_vars("contract C { uint256 a; uint256[5] __gap; }")
        new = _extract_state_vars("contract C { uint256 a; }")
        safe, changes, _, _ = _storage_layout(old, new)
        self.assertFalse(safe)
        self.assertTrue(any("gap" in c for c in changes))


class TestNamespacedStorage(unittest.TestCase):
    def test_detected(self) -> None:
        self.assertTrue(_is_namespaced_storage(CONTRACT_NAMESPACED))

    def test_plain_contract_is_not(self) -> None:
        self.assertFalse(_is_namespaced_storage(CONTRACT_OLD))


class TestDiffImplementations(unittest.TestCase):
    @patch("utils.impl_diff.fetch_source")
    def test_end_to_end(self, mock_fetch) -> None:
        mock_fetch.side_effect = [("Vault", CONTRACT_OLD), ("Vault", CONTRACT_NEW)]
        diff = diff_implementations("0xOld", "0xNew", 1)
        self.assertIsNotNone(diff)
        assert diff is not None
        self.assertTrue(diff.storage_layout_safe)
        self.assertEqual(len(diff.added_functions), 1)
        self.assertEqual(diff.added_functions[0].name, "setMaxDeposit")
        self.assertEqual(len(diff.changed_functions), 1)

    @patch("utils.impl_diff.fetch_source", return_value=None)
    def test_returns_none_on_unverified(self, mock_fetch) -> None:
        self.assertIsNone(diff_implementations("0xOld", "0xNew", 1))


class TestFormatImplDiff(unittest.TestCase):
    def test_basic_output(self) -> None:
        diff = ImplDiff(
            old_addr="0xOld",
            new_addr="0xNew",
            old_name="Vault",
            new_name="Vault",
            added_functions=[FunctionSig(name="newFn", args="uint256", visibility="external", modifiers="")],
            removed_functions=[],
            changed_functions=[],
            added_state_vars=[StateVarDecl(name="newVar", type_str="uint256", visibility="public", immutable=False)],
            removed_state_vars=[],
            layout_changes=[],
            storage_layout_safe=True,
            namespaced_storage=False,
        )
        out = format_impl_diff(diff)
        self.assertIn("Old: 0xOld", out)
        self.assertIn("New: 0xNew", out)
        self.assertIn("Functions added", out)
        self.assertIn("newFn(uint256)", out)
        self.assertIn("Storage layout safe", out)

    def test_unsafe_layout_warning(self) -> None:
        diff = ImplDiff(
            old_addr="0xOld",
            new_addr="0xNew",
            old_name="X",
            new_name="X",
            added_functions=[],
            removed_functions=[],
            changed_functions=[],
            added_state_vars=[],
            removed_state_vars=[],
            layout_changes=["slot 0: uint256 a → address b"],
            storage_layout_safe=False,
            namespaced_storage=False,
        )
        out = format_impl_diff(diff)
        self.assertIn("NOT upgrade-safe", out)
        self.assertIn("slot 0", out)

    def test_namespaced_skips_check(self) -> None:
        diff = ImplDiff(
            old_addr="0xOld",
            new_addr="0xNew",
            old_name="",
            new_name="",
            added_functions=[],
            removed_functions=[],
            changed_functions=[],
            added_state_vars=[],
            removed_state_vars=[],
            layout_changes=[],
            storage_layout_safe=True,
            namespaced_storage=True,
        )
        out = format_impl_diff(diff)
        self.assertIn("EIP-7201", out)
        self.assertIn("skipped", out)


if __name__ == "__main__":
    unittest.main()
