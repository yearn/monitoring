"""Tests for utils/abi_surface.py."""

import unittest

from utils.abi_surface import abi_functions, diff_abi_surface


def _fn(name: str, inputs: list[dict], mutability: str = "nonpayable") -> dict:
    return {"type": "function", "name": name, "inputs": inputs, "outputs": [], "stateMutability": mutability}


ADDRESS = {"name": "who", "type": "address"}
UINT = {"name": "amount", "type": "uint256"}
TUPLE_ARRAY = {
    "name": "params",
    "type": "tuple[]",
    "components": [{"name": "id", "type": "bytes32"}, {"name": "cap", "type": "uint128"}],
}


class TestAbiFunctions(unittest.TestCase):
    def test_canonicalizes_tuples_and_arrays(self) -> None:
        sigs = abi_functions([_fn("setCaps", [TUPLE_ARRAY])])
        self.assertIn("setCaps((bytes32,uint128)[])", sigs)

    def test_parameter_names_are_not_part_of_identity(self) -> None:
        a = abi_functions([_fn("setCap", [{"name": "cap", "type": "uint256"}])])
        b = abi_functions([_fn("setCap", [{"name": "newCap", "type": "uint256"}])])
        self.assertEqual(set(a), set(b))

    def test_overloads_are_separate_entries(self) -> None:
        sigs = abi_functions([_fn("setCap", [UINT]), _fn("setCap", [UINT, ADDRESS])])
        self.assertEqual(sorted(sigs), ["setCap(uint256)", "setCap(uint256,address)"])

    def test_fallback_and_receive_included(self) -> None:
        sigs = abi_functions([{"type": "fallback", "stateMutability": "payable"}, {"type": "receive"}])
        self.assertEqual(sorted(sigs), ["fallback()", "receive()"])

    def test_events_and_errors_excluded(self) -> None:
        sigs = abi_functions([{"type": "event", "name": "Upgraded", "inputs": [ADDRESS]}, {"type": "constructor"}])
        self.assertEqual(sigs, {})


class TestDiffAbiSurface(unittest.TestCase):
    def test_additions_and_removals(self) -> None:
        old = [_fn("whitelist", [ADDRESS], "view"), _fn("setCap", [UINT])]
        new = [_fn("setCap", [UINT]), _fn("supplyCapExempt", [ADDRESS], "view")]
        diff = diff_abi_surface(old, new)
        self.assertEqual([f.signature for f in diff.added], ["supplyCapExempt(address)"])
        self.assertEqual([f.signature for f in diff.removed], ["whitelist(address)"])
        self.assertFalse(diff.is_empty)

    def test_mutability_change(self) -> None:
        diff = diff_abi_surface([_fn("claim", [], "nonpayable")], [_fn("claim", [], "payable")])
        self.assertEqual(diff.added, [])
        self.assertEqual(len(diff.mutability_changed), 1)
        old, new = diff.mutability_changed[0]
        self.assertEqual((old.state_mutability, new.state_mutability), ("nonpayable", "payable"))

    def test_identical_abis(self) -> None:
        abi = [_fn("setCap", [UINT])]
        self.assertTrue(diff_abi_surface(abi, list(abi)).is_empty)


if __name__ == "__main__":
    unittest.main()
