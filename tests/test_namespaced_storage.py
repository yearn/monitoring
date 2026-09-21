"""Tests for utils/namespaced_storage.py."""

import unittest

from utils.namespaced_storage import collect_namespaces, compare_namespaces
from utils.verified_contract import VerifiedContract

INITIALIZABLE = """
abstract contract Initializable {
    /**
     * @dev Storage of the initializable contract.
     *
     * @custom:storage-location erc7201:openzeppelin.storage.Initializable
     */
    struct InitializableStorage {
        /// @dev Indicates that the contract has been initialized.
        uint64 _initialized;
        bool _initializing;
    }
}
"""


def _contract(sources: dict[str, str], name: str = "Vault", file: str | None = "src/Vault.sol") -> VerifiedContract:
    return VerifiedContract(
        contract_name=name,
        compiler_version="v0.8.22",
        language="Solidity",
        sources=sources,
        contract_file=file,
    )


class TestCollectNamespaces(unittest.TestCase):
    def test_finds_struct_level_annotation_in_an_inherited_base(self) -> None:
        """The standard ERC-7201 placement, reached through a two-level chain."""
        contract = _contract(
            {
                "src/Vault.sol": "contract Vault is Middle { uint256 cap; }",
                "src/Middle.sol": "abstract contract Middle is Initializable {}",
                "lib/Initializable.sol": INITIALIZABLE,
            }
        )
        namespaces = collect_namespaces(contract)
        assert namespaces is not None
        self.assertEqual(list(namespaces), ["erc7201:openzeppelin.storage.Initializable"])
        namespace = namespaces["erc7201:openzeppelin.storage.Initializable"]
        self.assertEqual(namespace.owners, ("Initializable",))
        self.assertEqual(
            namespace.definitions[0], "struct InitializableStorage { uint64 _initialized; bool _initializing; }"
        )

    def test_comments_do_not_affect_the_definition(self) -> None:
        commented = INITIALIZABLE.replace("bool _initializing;", "bool _initializing; // in progress")
        sources = {"src/Vault.sol": "contract Vault is Initializable {}", "lib/I.sol": INITIALIZABLE}
        plain = collect_namespaces(_contract(sources))
        noisy = collect_namespaces(_contract({**sources, "lib/I.sol": commented}))
        self.assertEqual(plain, noisy)

    def test_uninherited_contract_is_not_scanned(self) -> None:
        contract = _contract({"src/Vault.sol": "contract Vault { uint256 cap; }", "lib/I.sol": INITIALIZABLE})
        self.assertEqual(collect_namespaces(contract), {})

    def test_unannotated_struct_is_not_a_namespace(self) -> None:
        contract = _contract({"src/Vault.sol": "contract Vault { struct Config { uint256 fee; } Config cfg; }"})
        self.assertEqual(collect_namespaces(contract), {})

    def test_base_constructor_arguments_are_handled(self) -> None:
        contract = _contract(
            {"src/Vault.sol": 'contract Vault is Initializable, ERC20("Vault", "V") {}', "lib/I.sol": INITIALIZABLE}
        )
        self.assertIn("erc7201:openzeppelin.storage.Initializable", collect_namespaces(contract) or {})

    def test_unresolved_target_returns_none(self) -> None:
        self.assertIsNone(collect_namespaces(_contract({"a.sol": "contract Vault {}"}, file=None)))


class TestCompareNamespaces(unittest.TestCase):
    def _namespaces(self, members: str, owner_source: str | None = None):
        source = (
            owner_source
            or f"""
        abstract contract Base {{
            /// @custom:storage-location erc7201:app.main
            struct Main {{ {members} }}
        }}
        """
        )
        return collect_namespaces(_contract({"src/Vault.sol": "contract Vault is Base {}", "src/Base.sol": source}))

    def test_identical_elementary_namespace_is_unchanged(self) -> None:
        result = compare_namespaces(
            self._namespaces("uint256 a; mapping(address => bool) b;"),
            self._namespaces("uint256 a; mapping(address => bool) b;"),
        )
        self.assertTrue(result.is_validated)
        self.assertEqual(result.unchanged, ["erc7201:app.main"])

    def test_reordered_members_are_not_validated(self) -> None:
        result = compare_namespaces(self._namespaces("uint256 a; bool b;"), self._namespaces("bool b; uint256 a;"))
        self.assertFalse(result.is_validated)
        self.assertIn("struct definition changed", result.unvalidated[0])

    def test_appended_member_is_not_validated(self) -> None:
        """Appending is usually safe, but proving it needs a real layout — not text."""
        result = compare_namespaces(self._namespaces("uint256 a;"), self._namespaces("uint256 a; uint256 b;"))
        self.assertFalse(result.is_validated)

    def test_removed_namespace_is_not_validated(self) -> None:
        result = compare_namespaces(self._namespaces("uint256 a;"), {})
        self.assertIn("no longer declared", result.unvalidated[0])

    def test_user_defined_member_type_is_not_validated(self) -> None:
        for member in ("IERC20 token;", "Status s;", "Config cfg;", "mapping(address => Position) p;"):
            with self.subTest(member=member):
                ns = self._namespaces(member)
                result = compare_namespaces(ns, ns)
                self.assertFalse(result.is_validated)
                self.assertIn("user-defined types", result.unvalidated[0])

    def test_elementary_containers_are_validated(self) -> None:
        for member in ("uint256[] xs;", "mapping(address => mapping(uint256 => bytes32)) m;", "string name;"):
            with self.subTest(member=member):
                ns = self._namespaces(member)
                self.assertTrue(compare_namespaces(ns, ns).is_validated)

    def test_unresolved_side_is_not_validated(self) -> None:
        result = compare_namespaces(None, {})
        self.assertFalse(result.is_validated)


if __name__ == "__main__":
    unittest.main()
