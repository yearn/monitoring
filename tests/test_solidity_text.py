"""Tests for utils/solidity_text.py."""

import unittest

from utils.solidity_text import declares_contract, find_contract_body, iter_functions, normalize_params, strip_noise

SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity 0.8.22;

interface IVault {
    function rescue(address token) external;
}

abstract contract Base {
    function baseOnly() internal view returns (uint256) { return 1; }
}

contract Vault is Base, IVault {
    uint256 public cap;

    // function restartStrategy() external onlyOwner { paused = false; }

    function rescue(address token) external override onlyOwner {
        emit Rescued(token);
    }

    function setCap(uint256 newCap) public virtual returns (bool) {
        cap = newCap;
        return true;
    }

    function setCap(uint256 newCap, bool force) public { cap = newCap; }

    function _inner() private pure returns (uint256) {
        uint256 local = 1;
        if (local > 0) { local = 2; }
        return local;
    }

    receive() external payable {}
}

contract Other {
    function rescue(address token) external {}
}
"""


class TestStripNoise(unittest.TestCase):
    def test_comments_blanked_and_offsets_preserved(self) -> None:
        source = "uint256 a; // set a\nuint256 b;"
        cleaned = strip_noise(source)
        self.assertEqual(len(cleaned), len(source))
        self.assertNotIn("set a", cleaned)
        self.assertEqual(cleaned.count("\n"), source.count("\n"))

    def test_string_literals_blanked(self) -> None:
        self.assertNotIn("function", strip_noise('string x = "function foo() {}";'))


class TestDeclaresContract(unittest.TestCase):
    def test_finds_contract_interface_and_abstract(self) -> None:
        for name in ("Vault", "Base", "IVault", "Other"):
            self.assertTrue(declares_contract(SOURCE, name), name)

    def test_import_is_not_a_declaration(self) -> None:
        self.assertFalse(declares_contract('import {Vault} from "./Vault.sol";', "Vault"))

    def test_commented_declaration_ignored(self) -> None:
        self.assertFalse(declares_contract("// contract Ghost {}", "Ghost"))

    def test_prefix_name_not_matched(self) -> None:
        self.assertFalse(declares_contract("contract VaultFactory {}", "Vault"))


class TestFindContractBody(unittest.TestCase):
    def test_scopes_to_the_named_contract(self) -> None:
        body = find_contract_body(SOURCE, "Vault")
        assert body is not None
        self.assertIn("setCap", body)
        self.assertNotIn("baseOnly", body)  # base contract
        self.assertNotIn("contract Other", body)

    def test_missing_contract_returns_none(self) -> None:
        self.assertIsNone(find_contract_body(SOURCE, "Nope"))


class TestIterFunctions(unittest.TestCase):
    def setUp(self) -> None:
        body = find_contract_body(SOURCE, "Vault")
        assert body is not None
        self.fns = {f.signature: f for f in iter_functions(body)}

    def test_finds_members_of_this_contract_only(self) -> None:
        self.assertIn("rescue(address)", self.fns)
        self.assertIn("_inner()", self.fns)
        self.assertNotIn("baseOnly()", self.fns)

    def test_overloads_are_distinct(self) -> None:
        self.assertIn("setCap(uint256)", self.fns)
        self.assertIn("setCap(uint256,bool)", self.fns)

    def test_commented_out_function_ignored(self) -> None:
        self.assertNotIn("restartStrategy()", self.fns)

    def test_visibility_and_modifiers(self) -> None:
        rescue = self.fns["rescue(address)"]
        self.assertEqual(rescue.visibility, "external")
        self.assertEqual(rescue.modifiers, ("onlyOwner",))  # `override` dropped as noise

    def test_returns_clause_is_not_a_modifier(self) -> None:
        self.assertEqual(self.fns["setCap(uint256)"].modifiers, ())

    def test_body_is_whitespace_normalized_and_brace_matched(self) -> None:
        inner = self.fns["_inner()"]
        self.assertTrue(inner.has_body)
        self.assertEqual(inner.body, "uint256 local = 1; if (local > 0) { local = 2; } return local;")

    def test_receive_captured(self) -> None:
        self.assertIn("receive()", self.fns)

    def test_declaration_without_body(self) -> None:
        body = find_contract_body(SOURCE, "IVault")
        assert body is not None
        decl = iter_functions(body)[0]
        self.assertFalse(decl.has_body)
        self.assertEqual(decl.body, "")

    def test_nested_function_keyword_not_captured(self) -> None:
        body = find_contract_body("contract C { function f() external { bytes4 s = this.f.selector; } }", "C")
        assert body is not None
        self.assertEqual([f.signature for f in iter_functions(body)], ["f()"])


class TestNormalizeParams(unittest.TestCase):
    def test_drops_names_and_locations(self) -> None:
        self.assertEqual(normalize_params("uint256[] memory a, bytes calldata b"), "uint256[],bytes")

    def test_empty(self) -> None:
        self.assertEqual(normalize_params(""), "")
        self.assertEqual(normalize_params("  "), "")

    def test_nested_commas_not_split(self) -> None:
        self.assertEqual(
            normalize_params("MarketParams(uint256,uint256) p, bool f"), "MarketParams(uint256,uint256),bool"
        )


if __name__ == "__main__":
    unittest.main()
