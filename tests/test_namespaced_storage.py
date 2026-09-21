"""Tests for utils/namespaced_storage.py."""

import unittest

from utils.namespaced_storage import collect_namespaces, compare_namespaces
from utils.storage_access import erc7201_root
from utils.verified_contract import VerifiedContract

# OpenZeppelin v5's shape, including its published root constant.
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

    bytes32 private constant INITIALIZABLE_STORAGE = 0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00;

    function _getInitializableStorage() private pure returns (InitializableStorage storage $) {
        assembly { $.slot := INITIALIZABLE_STORAGE }
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
    def _namespaces(self, members: str, root: int | None = None):
        """A base declaring `app.main` with an accessor at ``root`` (the correct one by default)."""
        root = erc7201_root("app.main") if root is None else root
        source = f"""
        abstract contract Base {{
            /// @custom:storage-location erc7201:app.main
            struct Main {{ {members} }}

            function _main() private pure returns (Main storage $) {{
                assembly {{ $.slot := 0x{root:064x} }}
            }}
        }}
        """
        return collect_namespaces(_contract({"src/Vault.sol": "contract Vault is Base {}", "src/Base.sol": source}))

    def test_moved_root_is_a_conflict(self) -> None:
        result = compare_namespaces(self._namespaces("uint256 a;", root=1), self._namespaces("uint256 a;", root=2))
        self.assertIn("storage root changed", result.conflicts[0])
        self.assertEqual(result.unchanged, [])

    def test_root_not_matching_the_annotation_is_not_validated(self) -> None:
        ns = self._namespaces("uint256 a;", root=1)
        result = compare_namespaces(ns, ns)
        self.assertIn("does not match its annotation", result.unvalidated[0])

    def test_namespace_without_an_accessor_is_not_validated(self) -> None:
        """Without an accessor, the root actually in use is unknown."""
        source = """
        abstract contract Base {
            /// @custom:storage-location erc7201:app.main
            struct Main { uint256 a; }
        }
        """
        ns = collect_namespaces(_contract({"src/Vault.sol": "contract Vault is Base {}", "src/Base.sol": source}))
        self.assertIn("no accessor", compare_namespaces(ns, ns).unvalidated[0])

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
