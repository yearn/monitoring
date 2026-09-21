"""Tests for utils/storage_scope.py and utils/storage_access.py."""

import unittest

from utils.storage_access import erc7201_root, find_storage_access
from utils.storage_scope import storage_scope
from utils.verified_contract import VerifiedContract

# Published vectors, independent of the implementation under test.
OZ_INITIALIZABLE_ROOT = 0xF0C57E16840DF040F15088DC2F81FE391C3923BEC73E23A9662EFC9C229C6A00
EIP1967_IMPLEMENTATION_SLOT = 0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC


def _contract(sources: dict[str, str]) -> VerifiedContract:
    return VerifiedContract("Vault", "v0.8.22", "Solidity", sources, contract_file="src/Vault.sol")


def _roots(target_body: str, extra: dict[str, str] | None = None) -> list[int | None]:
    contract = _contract({"src/Vault.sol": f"contract Vault {{ {target_body} }}", **(extra or {})})
    scope = storage_scope(contract)
    assert scope is not None
    return [a.root for a in find_storage_access(contract, scope).assignments]


def _accessor(expr: str, prelude: str = "") -> str:
    return f"""
    struct S {{ uint256 a; }}
    {prelude}
    function _s() private pure returns (S storage $) {{ assembly {{ $.slot := {expr} }} }}
    """


class TestRootMath(unittest.TestCase):
    def test_erc7201_formula_matches_openzeppelin(self) -> None:
        self.assertEqual(erc7201_root("openzeppelin.storage.Initializable"), OZ_INITIALIZABLE_ROOT)


class TestRootResolution(unittest.TestCase):
    def test_hex_literal(self) -> None:
        self.assertEqual(_roots(_accessor("0x05")), [5])

    def test_constant(self) -> None:
        self.assertEqual(_roots(_accessor("ROOT", "bytes32 private constant ROOT = 0x07;")), [7])

    def test_eip1967_expression(self) -> None:
        prelude = 'bytes32 internal constant ROOT = bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1);'
        self.assertEqual(_roots(_accessor("ROOT", prelude)), [EIP1967_IMPLEMENTATION_SLOT])

    def test_erc7201_expression(self) -> None:
        prelude = (
            "bytes32 private constant ROOT = keccak256(abi.encode(uint256(keccak256("
            '"openzeppelin.storage.Initializable")) - 1)) & ~bytes32(uint256(0xff));'
        )
        self.assertEqual(_roots(_accessor("ROOT", prelude)), [OZ_INITIALIZABLE_ROOT])

    def test_local_assigned_from_a_getter(self) -> None:
        """OpenZeppelin v5.1's form: `bytes32 slot = _slot(); $.slot := slot`."""
        body = """
        struct S { uint256 a; }
        bytes32 private constant ROOT = 0x09;
        function _slot() internal pure virtual returns (bytes32) { return ROOT; }
        function _s() private pure returns (S storage $) {
            bytes32 slot = _slot();
            assembly { $.slot := slot }
        }
        """
        self.assertEqual(_roots(body), [9])

    def test_disagreeing_override_is_unresolved(self) -> None:
        """A derived override returning another root means the base's constant can't be assumed."""
        base = """
        abstract contract Base {
            struct S { uint256 a; }
            function _slot() internal pure virtual returns (bytes32) { return 0x01; }
            function _s() internal pure returns (S storage $) { bytes32 slot = _slot(); assembly { $.slot := slot } }
        }
        """
        contract = _contract(
            {
                "src/Vault.sol": "contract Vault is Base { function _slot() internal pure override returns (bytes32) { return 0x02; } }",
                "src/Base.sol": base,
            }
        )
        scope = storage_scope(contract)
        assert scope is not None
        self.assertEqual([a.root for a in find_storage_access(contract, scope).assignments], [None])

    def test_constant_declared_inconsistently_is_unresolved(self) -> None:
        other = "contract Other { bytes32 private constant ROOT = 0x08; }"
        self.assertEqual(
            _roots(_accessor("ROOT", "bytes32 private constant ROOT = 0x07;"), {"src/Other.sol": other}), [None]
        )

    def test_reassigned_local_is_unresolved(self) -> None:
        """Any write after the declaration means the initializer may not hold at the assignment."""
        for write in (
            "root = bytes32(uint256(root) + 256);",
            "root ^= bytes32(uint256(1));",
            "assembly { root := add(root, 1) }",
            "(root, x) = (bytes32(0), 1);",
        ):
            with self.subTest(write=write):
                body = f"""
                struct S {{ uint256 a; }}
                function _s() private pure returns (S storage $) {{
                    bytes32 root = 0x07; uint256 x;
                    {write}
                    assembly {{ $.slot := root }}
                }}
                """
                self.assertEqual(_roots(body), [None])

    def test_comparison_is_not_a_reassignment(self) -> None:
        body = """
        struct S { uint256 a; }
        function _s() private pure returns (S storage $) {
            bytes32 root = 0x07;
            require(root != bytes32(0) && root == root);
            assembly { $.slot := root }
        }
        """
        self.assertEqual(_roots(body), [7])

    def test_local_shadowing_a_constant_is_unresolved(self) -> None:
        """`let ROOT := …` in assembly hides the constant of the same name."""
        body = """
        struct S { uint256 a; }
        bytes32 private constant ROOT = 0x07;
        function _s() private pure returns (S storage $) { assembly { let ROOT := 0x09 $.slot := ROOT } }
        """
        self.assertEqual(_roots(body), [None])

    def test_struct_identity_is_its_declaration(self) -> None:
        """`Main` in a library and `Main` in the contract are different structs."""
        sources = {
            "src/Vault.sol": """
            contract Vault {
                struct Main { uint256 a; }
                function _m() private pure returns (Main storage $) { assembly { $.slot := 0x01 } }
                function f() external { Lib.m(); }
            }
            """,
            "src/Lib.sol": """
            library Lib {
                struct Main { bytes32 x; }
                function m() internal pure returns (Main storage $) { assembly { $.slot := 0x02 } }
            }
            """,
        }
        contract = _contract(sources)
        scope = storage_scope(contract)
        assert scope is not None
        keys = sorted((a.struct_key, a.struct_shape) for a in find_storage_access(contract, scope).assignments)
        self.assertEqual(keys, [("Lib.Main", ("bytes32",)), ("Vault.Main", ("uint256",))])

    def test_unsupported_expression_is_unresolved(self) -> None:
        self.assertEqual(_roots(_accessor("add(ROOT, 1)", "bytes32 private constant ROOT = 0x07;")), [None])

    def test_struct_is_taken_from_the_returned_pointer(self) -> None:
        contract = _contract({"src/Vault.sol": f"contract Vault {{ {_accessor('0x05')} }}"})
        scope = storage_scope(contract)
        assert scope is not None
        (assignment,) = find_storage_access(contract, scope).assignments
        self.assertEqual((assignment.struct, assignment.where), ("S", "Vault._s"))


class TestScope(unittest.TestCase):
    LIBRARY = """
    library Lib {
        function safe(uint256 x) internal pure returns (uint256) { return x; }
        function risky(address t) internal { t.delegatecall(""); }
    }
    """

    def _raw(self, target: str) -> list[str]:
        contract = _contract({"src/Vault.sol": target, "src/Lib.sol": self.LIBRARY})
        scope = storage_scope(contract)
        assert scope is not None
        return [f"{r.owner}.{r.function}:{r.kind}" for r in find_storage_access(contract, scope).raw]

    def test_uncalled_library_function_is_not_in_scope(self) -> None:
        """Using one function of a library doesn't pull in its delegatecalling sibling."""
        self.assertEqual(self._raw("contract Vault { function f() external { Lib.safe(1); } }"), [])

    def test_called_library_function_is_in_scope(self) -> None:
        target = "contract Vault { function f(address t) external { Lib.risky(t); } }"
        self.assertEqual(self._raw(target), ["Lib.risky:delegatecall"])

    def test_using_for_makes_a_library_reachable(self) -> None:
        target = "contract Vault { using Lib for address; function f(address t) external { t.risky(); } }"
        self.assertEqual(self._raw(target), ["Lib.risky:delegatecall"])

    def test_imported_but_unreferenced_library_is_not_in_scope(self) -> None:
        self.assertEqual(self._raw("contract Vault { function f(address t) external { risky(t); } }"), [])

    def test_constructor_is_not_in_scope(self) -> None:
        """A constructor runs against the implementation's own storage, never the proxy's."""
        target = "contract Vault { constructor() { assembly { sstore(0, 1) } } }"
        self.assertEqual(self._raw(target), [])

    def test_unresolved_target_has_no_scope(self) -> None:
        contract = VerifiedContract("Vault", "v0.8.22", "Solidity", {"a.sol": "contract Vault {}"}, contract_file=None)
        self.assertIsNone(storage_scope(contract))


if __name__ == "__main__":
    unittest.main()
