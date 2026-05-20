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


class TestNamespacedStorage(unittest.TestCase):
    def test_detected(self) -> None:
        self.assertTrue(_is_namespaced_storage(CONTRACT_NAMESPACED))

    def test_plain_contract_is_not(self) -> None:
        self.assertFalse(_is_namespaced_storage(CONTRACT_OLD))


class TestDiffImplementations(unittest.TestCase):
    @patch("utils.impl_diff._fetch_source")
    def test_end_to_end(self, mock_fetch) -> None:
        mock_fetch.side_effect = [("Vault", CONTRACT_OLD), ("Vault", CONTRACT_NEW)]
        diff = diff_implementations("0xOld", "0xNew", 1)
        self.assertIsNotNone(diff)
        assert diff is not None
        self.assertTrue(diff.storage_layout_safe)
        self.assertEqual(len(diff.added_functions), 1)
        self.assertEqual(diff.added_functions[0].name, "setMaxDeposit")
        self.assertEqual(len(diff.changed_functions), 1)

    @patch("utils.impl_diff._fetch_source", return_value=None)
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
