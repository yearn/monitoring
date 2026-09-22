"""Storage verdicts must account for everything the positional layout can't see.

Review of yearn/monitoring#368 (comment 5759098600) showed five ways the storage
verdict could read COMPATIBLE while storage was changed or simply not covered:

1. a custom value type unwrapped to what it wraps was flagged INCOMPATIBLE;
2. the same custom type name wrapping a *different* type passed as COMPATIBLE;
3. storage a used library writes through a slot accessor wasn't covered;
4. an unannotated accessor could move its storage root unnoticed;
5. an ERC-7201 namespace was "unchanged" on struct text alone, even when its
   accessor now points at a different root.

The verdict is now: any proven conflict → INCOMPATIBLE; otherwise any storage
the check cannot see → UNKNOWN; otherwise COMPATIBLE. The positional result stays
visible on its own either way. Everything goes through the public
``diff_implementations`` entry point with only the fetchers mocked.
"""

import json
import unittest
from unittest.mock import patch

from utils.impl_diff import diff_implementations, format_impl_diff, reset_provenance_registry
from utils.sourcify_layout import StorageLayout
from utils.storage_layout import StorageCompatibility
from utils.verified_contract import parse_etherscan_entry

COMPATIBLE = StorageCompatibility.COMPATIBLE
INCOMPATIBLE = StorageCompatibility.INCOMPATIBLE
UNKNOWN = StorageCompatibility.UNKNOWN

# Published ERC-7201 root for "openzeppelin.storage.Initializable" (OpenZeppelin v5),
# used as an independent vector rather than recomputing the formula here.
OZ_INITIALIZABLE_ROOT = "0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00"

TYPES = {
    "t_uint256": {"label": "uint256", "encoding": "inplace", "numberOfBytes": "32"},
    "t_bytes32": {"label": "bytes32", "encoding": "inplace", "numberOfBytes": "32"},
    "t_address": {"label": "address", "encoding": "inplace", "numberOfBytes": "20"},
    "t_userDefinedValueType(Id)10": {"label": "Id", "encoding": "inplace", "numberOfBytes": "32"},
    "t_userDefinedValueType(Id)20": {"label": "Id", "encoding": "inplace", "numberOfBytes": "32"},
    "t_mapping(t_address,t_userDefinedValueType(Id)10)": {
        "key": "t_address",
        "value": "t_userDefinedValueType(Id)10",
        "label": "mapping(address => Id)",
        "encoding": "mapping",
        "numberOfBytes": "32",
    },
    "t_mapping(t_address,t_bytes32)": {
        "key": "t_address",
        "value": "t_bytes32",
        "label": "mapping(address => bytes32)",
        "encoding": "mapping",
        "numberOfBytes": "32",
    },
}

PLAIN_TARGET = "contract Vault { uint256 cap; }"


def _layout(type_id: str = "t_uint256", label: str = "cap") -> StorageLayout:
    storage = [{"slot": "0", "offset": 0, "label": label, "type": type_id, "astId": 1}]
    return StorageLayout(address="0xtest", match="match", storage=storage, types=TYPES)


def _entry(sources: dict[str, str]) -> dict:
    payload = {"language": "Solidity", "sources": {p: {"content": c} for p, c in sources.items()}, "settings": {}}
    return {
        "SourceCode": "{" + json.dumps(payload) + "}",
        "ABI": "[]",
        "ContractName": "Vault",
        "CompilerVersion": "v0.8.22+commit.4fc1097e",
    }


def _accessor_contract(root_expr: str, annotation: str = "", struct: str = "Main", members: str = "uint256 a;") -> str:
    """A target whose storage struct sits behind a `$.slot :=` accessor."""
    note = f"/// @custom:storage-location {annotation}\n    " if annotation else ""
    return f"""
    contract Vault {{
        uint256 cap;

        {note}struct {struct} {{ {members} }}

        bytes32 private constant ROOT = {root_expr};

        function _getMain() private pure returns ({struct} storage $) {{
            assembly {{ $.slot := ROOT }}
        }}
    }}
    """


class CoverageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_provenance_registry()

    def diff(
        self,
        old_sources: dict[str, str],
        new_sources: dict[str, str],
        old_layout: StorageLayout | None = None,
        new_layout: StorageLayout | None = None,
    ):
        contracts = [parse_etherscan_entry(_entry(old_sources)), parse_etherscan_entry(_entry(new_sources))]
        layouts = [old_layout or _layout(), new_layout or _layout()]
        with (
            patch("utils.impl_diff.fetch_verified_contract", side_effect=contracts),
            patch("utils.impl_diff.fetch_storage_layout", side_effect=layouts),
        ):
            diff = diff_implementations("0xOld", "0xNew", 1)
        assert diff is not None
        return diff


class TestUserDefinedValueTypes(CoverageTestCase):
    """A custom value type is compared by what it wraps, resolved from each side's source."""

    ID_BYTES32 = {"src/Vault.sol": PLAIN_TARGET, "src/Types.sol": "type Id is bytes32;"}
    ID_UINT256 = {"src/Vault.sol": PLAIN_TARGET, "src/Types.sol": "type Id is uint256;"}

    def test_unwrapping_to_the_underlying_type_is_compatible(self) -> None:
        """Row 1: `Id` → `bytes32` keeps the same representation."""
        diff = self.diff(
            self.ID_BYTES32,
            self.ID_BYTES32,
            _layout("t_userDefinedValueType(Id)10", "marketId"),
            _layout("t_bytes32", "marketId"),
        )
        self.assertEqual(diff.storage_status, COMPATIBLE)
        self.assertIn("Id → bytes32", format_impl_diff(diff))

    def test_same_name_wrapping_a_different_type_is_incompatible(self) -> None:
        """Row 2: `Id` stays `Id`, but now wraps `uint256` instead of `bytes32`."""
        diff = self.diff(
            self.ID_BYTES32,
            self.ID_UINT256,
            _layout("t_userDefinedValueType(Id)10", "marketId"),
            _layout("t_userDefinedValueType(Id)20", "marketId"),
        )
        self.assertEqual(diff.storage_status, INCOMPATIBLE)

    def test_nested_in_a_mapping_value(self) -> None:
        diff = self.diff(
            self.ID_BYTES32,
            self.ID_BYTES32,
            _layout("t_mapping(t_address,t_userDefinedValueType(Id)10)", "ids"),
            _layout("t_mapping(t_address,t_bytes32)", "ids"),
        )
        self.assertEqual(diff.storage_status, COMPATIBLE)

    def test_undeclared_type_is_unknown_even_when_names_match(self) -> None:
        """An unresolved `Id` must not compare equal just because the name matches."""
        diff = self.diff(
            {"src/Vault.sol": PLAIN_TARGET},
            {"src/Vault.sol": PLAIN_TARGET},
            _layout("t_userDefinedValueType(Id)10", "marketId"),
            _layout("t_userDefinedValueType(Id)20", "marketId"),
        )
        self.assertEqual(diff.storage_status, UNKNOWN)

    def test_conflicting_declarations_are_unknown(self) -> None:
        sources = {**self.ID_BYTES32, "src/Other.sol": "type Id is uint256;"}
        diff = self.diff(
            sources,
            sources,
            _layout("t_userDefinedValueType(Id)10", "marketId"),
            _layout("t_userDefinedValueType(Id)20", "marketId"),
        )
        self.assertEqual(diff.storage_status, UNKNOWN)


class TestStorageAccessCoverage(CoverageTestCase):
    def test_used_library_accessor_is_a_coverage_gap(self) -> None:
        """Row 3: the target writes storage through a library accessor it calls."""
        library = """
        library StateLib {
            struct State { uint256 total; }
            function state() internal pure returns (State storage s) {
                bytes32 slot = keccak256("app.state");
                assembly { s.slot := slot }
            }
        }
        """
        target = "contract Vault { uint256 cap; function bump() external { StateLib.state().total += 1; } }"
        sources = {"src/Vault.sol": target, "src/StateLib.sol": library}
        diff = self.diff(sources, sources)
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertEqual(diff.storage.status, COMPATIBLE, "positional result stays visible")
        self.assertIn("StateLib", format_impl_diff(diff))

    def test_merely_imported_library_does_not_count(self) -> None:
        """The original #367 failure: an imported helper is not this contract's storage."""
        library = """
        library UnusedLib {
            struct S { uint256 x; }
            function s() internal pure returns (S storage r) { assembly { r.slot := 0x01 } }
        }
        """
        sources = {"src/Vault.sol": PLAIN_TARGET, "src/UnusedLib.sol": library}
        self.assertEqual(self.diff(sources, sources).storage_status, COMPATIBLE)

    def test_unannotated_accessor_root_change_is_incompatible(self) -> None:
        """Row 4: same struct, but the accessor now reads a different root."""
        diff = self.diff(
            {"src/Vault.sol": _accessor_contract('keccak256("app.main.v1")')},
            {"src/Vault.sol": _accessor_contract('keccak256("app.main.v2")')},
        )
        self.assertEqual(diff.storage_status, INCOMPATIBLE)
        self.assertIn("root changed", format_impl_diff(diff))

    def test_unannotated_accessor_with_the_same_root_is_unknown(self) -> None:
        """A stable root proves nothing about the struct behind it without a layout."""
        source = {"src/Vault.sol": _accessor_contract('keccak256("app.main")')}
        self.assertEqual(self.diff(source, source).storage_status, UNKNOWN)

    def test_raw_sload_or_sstore_is_a_coverage_gap(self) -> None:
        target = "contract Vault { uint256 cap; function f(bytes32 k) external { assembly { sstore(k, 1) } } }"
        diff = self.diff({"src/Vault.sol": target}, {"src/Vault.sol": target})
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("sstore", format_impl_diff(diff))

    def test_delegatecall_in_a_base_is_a_coverage_gap(self) -> None:
        sources = {
            "src/Vault.sol": "contract Vault is Base { uint256 cap; }",
            "src/Base.sol": "abstract contract Base { function f(address t) external { t.delegatecall(msg.data); } }",
        }
        diff = self.diff(sources, sources)
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("delegatecall", format_impl_diff(diff))

    def test_unrelated_omissions_do_not_downgrade_storage(self) -> None:
        """Unchecked modifier or inherited bodies are not storage coverage gaps."""
        sources = {
            "src/Vault.sol": "contract Vault is Base { uint256 cap; function f() external onlyOwner {} }",
            "src/Base.sol": "abstract contract Base { modifier onlyOwner() { _; } }",
        }
        self.assertEqual(self.diff(sources, sources).storage_status, COMPATIBLE)


class TestNamespaceRoots(CoverageTestCase):
    def test_changed_root_with_identical_struct_is_incompatible(self) -> None:
        """Row 5: same annotation, same struct text, different root."""
        diff = self.diff(
            {"src/Vault.sol": _accessor_contract("0x01", "erc7201:app.main")},
            {"src/Vault.sol": _accessor_contract("0x02", "erc7201:app.main")},
        )
        self.assertEqual(diff.storage_status, INCOMPATIBLE)
        self.assertEqual(diff.namespaces.unchanged, [])

    def test_root_not_matching_its_annotation_is_unknown(self) -> None:
        """ERC-7201 leaves enforcement to the developer, so the annotation alone proves nothing."""
        source = {"src/Vault.sol": _accessor_contract("0x01", "erc7201:app.main")}
        diff = self.diff(source, source)
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("does not match", format_impl_diff(diff))

    def test_verified_root_through_a_getter_is_compatible(self) -> None:
        """The OpenZeppelin v5 form: a local assigned from a virtual getter returning the constant."""
        initializable = f"""
        abstract contract Initializable {{
            /// @custom:storage-location erc7201:openzeppelin.storage.Initializable
            struct InitializableStorage {{ uint64 _initialized; bool _initializing; }}

            bytes32 private constant INITIALIZABLE_STORAGE = {OZ_INITIALIZABLE_ROOT};

            function _getInitializableStorage() private pure returns (InitializableStorage storage $) {{
                bytes32 slot = _initializableStorageSlot();
                assembly {{ $.slot := slot }}
            }}

            function _initializableStorageSlot() internal pure virtual returns (bytes32) {{
                return INITIALIZABLE_STORAGE;
            }}
        }}
        """
        sources = {"src/Vault.sol": "contract Vault is Initializable { uint256 cap; }", "lib/I.sol": initializable}
        diff = self.diff(sources, sources)
        self.assertEqual(diff.storage_status, COMPATIBLE)
        self.assertEqual(diff.namespaces.unchanged, ["erc7201:openzeppelin.storage.Initializable"])

    def test_differently_shaped_structs_on_one_root_are_unresolved_not_a_conflict(self) -> None:
        """Sharing a root isn't a proven conflict: without knowing how each struct is
        used, a different shape is an unresolved overlap."""
        target = """
        contract Vault {
            uint256 cap;
            struct A { uint256 x; }
            struct B { address y; }
            function _a() private pure returns (A storage $) { assembly { $.slot := 0x05 } }
            function _b() private pure returns (B storage $) { assembly { $.slot := 0x05 } }
        }
        """
        diff = self.diff({"src/Vault.sol": PLAIN_TARGET}, {"src/Vault.sol": target})
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("different shapes", format_impl_diff(diff))


RAW_WRITE_LIBRARY = "library Lib { function write() internal { assembly { sstore(0x10, 1) } } }"


class TestScopeResolution(CoverageTestCase):
    """Code reached under another name, or outside any contract, still runs against
    the proxy's storage (review of #368, second round)."""

    def test_library_imported_under_an_alias_is_followed(self) -> None:
        target = 'import {Lib as State} from "./Lib.sol"; contract Vault { uint256 cap; function f() external { State.write(); } }'
        sources = {"src/Vault.sol": target, "src/Lib.sol": RAW_WRITE_LIBRARY}
        diff = self.diff(sources, sources)
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("Lib.write", format_impl_diff(diff))

    def test_aliased_using_for_is_followed(self) -> None:
        target = (
            'import {Lib as State} from "./Lib.sol"; '
            "contract Vault { using State for uint256; uint256 cap; function f() external { cap.write(); } }"
        )
        sources = {"src/Vault.sol": target, "src/Lib.sol": RAW_WRITE_LIBRARY}
        self.assertEqual(self.diff(sources, sources).storage_status, UNKNOWN)

    def test_base_contract_imported_under_an_alias_is_inherited(self) -> None:
        base = "abstract contract Base { function g() external { assembly { sstore(0x10, 1) } } }"
        target = 'import {Base as B} from "./Base.sol"; contract Vault is B { uint256 cap; }'
        sources = {"src/Vault.sol": target, "src/Base.sol": base}
        self.assertEqual(self.diff(sources, sources).storage_status, UNKNOWN)

    def test_reachable_free_function_is_inspected(self) -> None:
        target = (
            "function writeRaw() { assembly { sstore(0x10, 1) } } "
            "contract Vault { uint256 cap; function f() external { writeRaw(); } }"
        )
        diff = self.diff({"src/Vault.sol": target}, {"src/Vault.sol": target})
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("writeRaw", format_impl_diff(diff))

    def test_free_function_imported_under_an_alias_is_followed(self) -> None:
        helpers = "function writeRaw() { assembly { sstore(0x10, 1) } }"
        target = 'import {writeRaw as w} from "./Helpers.sol"; contract Vault { uint256 cap; function f() external { w(); } }'
        sources = {"src/Vault.sol": target, "src/Helpers.sol": helpers}
        self.assertEqual(self.diff(sources, sources).storage_status, UNKNOWN)

    def test_function_list_using_directive_is_followed(self) -> None:
        """`using {Lib.write} for T` attaches one library function; `v.write()` calls it."""
        library = "library Lib { function write(uint256 v) internal { assembly { sstore(0x10, v) } } }"
        target = "contract Vault { using {Lib.write} for uint256; uint256 cap; function f() external { cap.write(); } }"
        sources = {"src/Vault.sol": target, "src/Lib.sol": library}
        diff = self.diff(sources, sources)
        self.assertEqual(diff.storage_status, UNKNOWN)
        self.assertIn("Lib.write", format_impl_diff(diff))

    def test_function_list_using_free_function_and_operator_is_followed(self) -> None:
        """A bound free function may only ever be invoked through an operator — no call text."""
        target = (
            "type Amount is uint256; "
            "function add(Amount a, Amount b) returns (Amount) { assembly { sstore(0x10, 1) } return a; } "
            "using {add as +} for Amount global; "
            "contract Vault { uint256 cap; function f(Amount a) external { a + a; } }"
        )
        diff = self.diff({"src/Vault.sol": target}, {"src/Vault.sol": target})
        self.assertEqual(diff.storage_status, UNKNOWN)

    def test_same_named_libraries_in_different_files_are_both_followed(self) -> None:
        """Without knowing which `Lib` a call binds to, both are in scope — a harmless
        definition must not mask the one that writes storage."""
        harmless = "library Lib { function write() internal pure {} }"
        harmful = "library Lib { function write() internal { assembly { sstore(0x10, 1) } } }"
        target = "contract Vault { uint256 cap; function f() external { Lib.write(); } }"
        for first, second in ((harmless, harmful), (harmful, harmless)):
            with self.subTest(harmful_first=first is harmful):
                sources = {"src/Vault.sol": target, "src/a/Lib.sol": first, "src/b/Lib.sol": second}
                self.assertEqual(self.diff(sources, sources).storage_status, UNKNOWN)

    def test_unreached_free_function_is_ignored(self) -> None:
        target = "function writeRaw() { assembly { sstore(0x10, 1) } } contract Vault { uint256 cap; }"
        self.assertEqual(self.diff({"src/Vault.sol": target}, {"src/Vault.sol": target}).storage_status, COMPATIBLE)


class TestRootAndStructIdentity(CoverageTestCase):
    @staticmethod
    def _namespaced(extra: str) -> dict[str, str]:
        from utils.storage_access import erc7201_root

        root = f"0x{erc7201_root('app.main'):064x}"
        return {
            "src/Vault.sol": f"""
            contract Vault {{
                uint256 cap;
                /// @custom:storage-location erc7201:app.main
                struct Main {{ uint256 a; }}
                function _m() private pure returns (Main storage $) {{
                    bytes32 root = {root};
                    {extra}
                    assembly {{ $.slot := root }}
                }}
            }}
            """
        }

    def test_reassigned_local_root_is_unresolved(self) -> None:
        """The initializer no longer holds at the assignment, so the root is unknown."""
        diff = self.diff(self._namespaced(""), self._namespaced("root = bytes32(uint256(root) + 256);"))
        self.assertEqual(diff.namespaces.unchanged, [])
        self.assertEqual(diff.storage_status, UNKNOWN)

    def test_deleted_local_root_is_unresolved(self) -> None:
        """`delete root` zeroes it, so the initializer no longer holds at the assignment."""
        diff = self.diff(self._namespaced(""), self._namespaced("delete root;"))
        self.assertEqual(diff.namespaces.unchanged, [])
        self.assertEqual(diff.storage_status, UNKNOWN)

    def test_reassigned_in_assembly_is_unresolved(self) -> None:
        source = self._namespaced("assembly { root := add(root, 1) }")
        diff = self.diff(source, source)
        self.assertEqual(diff.namespaces.unchanged, [])

    def test_unreassigned_local_root_still_verifies(self) -> None:
        diff = self.diff(self._namespaced(""), self._namespaced(""))
        self.assertEqual(diff.namespaces.unchanged, ["erc7201:app.main"])
        self.assertEqual(diff.storage_status, COMPATIBLE)

    def test_same_named_struct_in_a_library_does_not_borrow_validation(self) -> None:
        """`Lib.Main` is not `Vault.Main`: matching bare names let an unannotated library
        struct at the same root ride on the namespace's validation."""
        from utils.storage_access import erc7201_root

        root = f"0x{erc7201_root('app.main'):064x}"

        def sources(member: str) -> dict[str, str]:
            return {
                "src/Vault.sol": f"""
                contract Vault {{
                    uint256 cap;
                    /// @custom:storage-location erc7201:app.main
                    struct Main {{ uint256 a; }}
                    function _m() private pure returns (Main storage $) {{ assembly {{ $.slot := {root} }} }}
                    function f() external {{ Lib.m().x = 1; }}
                }}
                """,
                "src/Lib.sol": f"""
                library Lib {{
                    struct Main {{ {member} x; }}
                    function m() internal pure returns (Main storage $) {{ assembly {{ $.slot := {root} }} }}
                }}
                """,
            }

        diff = self.diff(sources("uint256"), sources("bytes32"))
        self.assertNotEqual(diff.storage_status, COMPATIBLE)
        self.assertIn("Lib.m", format_impl_diff(diff))

    def test_identically_shaped_structs_sharing_a_root_are_not_a_conflict(self) -> None:
        """Two views of the same data at one root is a legitimate pattern."""
        target = """
        contract Vault {
            uint256 cap;
            struct A { uint256 x; }
            struct B { uint256 y; }
            function _a() private pure returns (A storage $) { assembly { $.slot := 0x05 } }
            function _b() private pure returns (B storage $) { assembly { $.slot := 0x05 } }
        }
        """
        diff = self.diff({"src/Vault.sol": target}, {"src/Vault.sol": target})
        self.assertNotEqual(diff.storage_status, INCOMPATIBLE)
        self.assertEqual(diff.namespaces.conflicts, [])
        self.assertNotIn("different shapes", format_impl_diff(diff))


class TestVerdictPrecedence(CoverageTestCase):
    def test_proven_conflict_wins_over_a_coverage_gap(self) -> None:
        target = "contract Vault { uint256 cap; function f(bytes32 k) external { assembly { sstore(k, 1) } } }"
        diff = self.diff(
            {"src/Vault.sol": target},
            {"src/Vault.sol": target},
            _layout("t_uint256", "cap"),
            _layout("t_address", "cap"),
        )
        self.assertEqual(diff.storage_status, INCOMPATIBLE)

    def test_positional_result_is_reported_separately(self) -> None:
        target = "contract Vault { uint256 cap; function f(bytes32 k) external { assembly { sstore(k, 1) } } }"
        diff = self.diff({"src/Vault.sol": target}, {"src/Vault.sol": target})
        rendered = format_impl_diff(diff)
        self.assertIn("Storage compatibility: UNKNOWN", rendered)
        self.assertIn("Positional layout (compiler): COMPATIBLE", rendered)


if __name__ == "__main__":
    unittest.main()
