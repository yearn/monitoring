"""Tests for utils/impl_diff.py.

Focused fixtures for the failure modes in yearn/monitoring#367; the real 3Jane
upgrade is pinned separately in `test_impl_diff_3jane.py`.
"""

import json
import unittest
from unittest.mock import patch

from utils.impl_diff import (
    diff_implementations,
    format_impl_diff,
    reset_provenance_registry,
)
from utils.sourcify_layout import StorageLayout
from utils.storage_access import erc7201_root
from utils.storage_layout import StorageCompatibility
from utils.verified_contract import parse_etherscan_entry

TARGET_OLD = """
// SPDX-License-Identifier: MIT
pragma solidity 0.8.22;

contract Vault is Base {
    // function restartStrategy() external onlyOwner { paused = false; }

    function setCap(uint256 newCap) external onlyOwner { cap = newCap; }

    function totalAssets() public view returns (uint256) {
        return asset.balanceOf(address(this));
    }
}
"""

TARGET_NEW = """
// SPDX-License-Identifier: MIT
pragma solidity 0.8.22;

contract Vault is Base {
    function setCap(uint256 newCap) external onlyOwner { cap = newCap; }

    function totalAssets() public view returns (uint256) {
        // Exclude the buffer from reported assets.
        return asset.balanceOf(address(this)) - buffer;
    }

    function sweep(address token) external onlyOwner { _sweep(token); }
}
"""

# An imported interface that declares functions the deployed contract does not have.
IMPORTED_INTERFACE = """
interface IMorpho {
    function clearMarketWindDown(Id id) external;
    function setCap(uint256 newCap) external;
}
"""

BASE_CONTRACT = "abstract contract Base { function setProfitMaxUnlockTime(uint256 t) external virtual; }"


def _abi(*signatures: tuple[str, str, str]) -> str:
    """Build an ABI JSON string from (name, solidity types, mutability) triples."""
    entries = []
    for name, types, mutability in signatures:
        inputs = [{"name": f"a{i}", "type": t} for i, t in enumerate(types.split(",")) if t]
        entries.append(
            {"type": "function", "name": name, "inputs": inputs, "outputs": [], "stateMutability": mutability}
        )
    return json.dumps(entries)


OLD_ABI = _abi(("setCap", "uint256", "nonpayable"), ("totalAssets", "", "view"))
NEW_ABI = _abi(
    ("setCap", "uint256", "nonpayable"),
    ("totalAssets", "", "view"),
    ("sweep", "address", "nonpayable"),
)


def _bundle(
    target_source: str, abi: str, name: str = "Vault", base: str = BASE_CONTRACT, extra: dict[str, str] | None = None
) -> dict:
    """An Etherscan entry whose bundle mixes the target with an interface and a base."""
    sources = {
        "src/Vault.sol": {"content": target_source},
        "src/interfaces/IMorpho.sol": {"content": IMPORTED_INTERFACE},
        "src/Base.sol": {"content": base},
    }
    for path, content in (extra or {}).items():
        sources[path] = {"content": content}
    return {
        "SourceCode": "{" + json.dumps({"language": "Solidity", "sources": sources, "settings": {}}) + "}",
        "ABI": abi,
        "ContractName": name,
        "CompilerVersion": "v0.8.22+commit.4fc1097e",
    }


def _contract(entry: dict):
    return parse_etherscan_entry(entry)


def _layout(entries: list[tuple[int, int, str, str]]) -> StorageLayout:
    """Build a layout from (slot, offset, label, type-id) tuples."""
    types = {
        "t_uint256": {"label": "uint256", "encoding": "inplace", "numberOfBytes": "32"},
        "t_address": {"label": "address", "encoding": "inplace", "numberOfBytes": "20"},
        "t_bool": {"label": "bool", "encoding": "inplace", "numberOfBytes": "1"},
        "t_array(t_uint256)40_storage": {
            "base": "t_uint256",
            "label": "uint256[40]",
            "encoding": "inplace",
            "numberOfBytes": "1280",
        },
        "t_array(t_uint256)39_storage": {
            "base": "t_uint256",
            "label": "uint256[39]",
            "encoding": "inplace",
            "numberOfBytes": "1248",
        },
    }
    storage = [
        {"slot": str(slot), "offset": offset, "label": label, "type": type_id, "astId": 1}
        for slot, offset, label, type_id in entries
    ]
    return StorageLayout(address="0xtest", match="match", storage=storage, types=types)


OLD_LAYOUT = _layout([(0, 0, "cap", "t_uint256"), (1, 0, "__gap", "t_array(t_uint256)40_storage")])
NEW_LAYOUT = _layout(
    [(0, 0, "cap", "t_uint256"), (1, 0, "buffer", "t_uint256"), (2, 0, "__gap", "t_array(t_uint256)39_storage")]
)


class ImplDiffTestCase(unittest.TestCase):
    """Wires the fetchers to in-memory fixtures instead of Etherscan/Sourcify.

    Subclasses adjust `self.old_entry` / `self.new_entry` / the layouts in their
    own `setUp` (after `super().setUp()`) to model one failure mode each.
    """

    def setUp(self) -> None:
        reset_provenance_registry()
        self.old_entry = _bundle(TARGET_OLD, OLD_ABI)
        self.new_entry = _bundle(TARGET_NEW, NEW_ABI)
        self.old_layout: StorageLayout | None = OLD_LAYOUT
        self.new_layout: StorageLayout | None = NEW_LAYOUT

    def run_diff(self):
        contracts = [_contract(self.old_entry), _contract(self.new_entry)]
        layouts = [self.old_layout, self.new_layout]
        with (
            patch("utils.impl_diff.fetch_verified_contract", side_effect=contracts),
            patch("utils.impl_diff.fetch_storage_layout", side_effect=layouts),
        ):
            return diff_implementations("0xOld", "0xNew", 1)


class TestSurfaceIsAbiDerived(ImplDiffTestCase):
    def test_addition_comes_from_the_abi(self) -> None:
        diff = self.run_diff()
        assert diff is not None and diff.surface is not None
        self.assertEqual([f.signature for f in diff.surface.added], ["sweep(address)"])
        self.assertEqual(diff.surface.removed, [])

    def test_imported_interface_declarations_are_not_reported(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertNotIn("clearMarketWindDown", format_impl_diff(diff))

    def test_commented_out_function_is_not_a_removal(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertNotIn("restartStrategy", format_impl_diff(diff))

    def test_duplicate_signature_in_interface_does_not_mask_the_target(self) -> None:
        """`setCap` is declared by both the target and the imported interface."""
        diff = self.run_diff()
        assert diff is not None and diff.surface is not None
        self.assertNotIn("setCap(uint256)", [f.signature for f in diff.surface.added])
        self.assertNotIn("setCap(uint256)", [f.signature for f in diff.surface.removed])

    def test_base_declaration_does_not_become_an_addition(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertNotIn("setProfitMaxUnlockTime", format_impl_diff(diff))


class TestBodyChanges(ImplDiffTestCase):
    def test_body_only_change_is_reported_with_a_diff(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual([c.signature for c in diff.bodies.changed], ["totalAssets()"])
        change = diff.bodies.changed[0]
        self.assertEqual(change.visibility, "public")
        self.assertIn("- buffer", change.diff)
        self.assertIn("+++ new totalAssets()", change.diff)

    def test_unchanged_body_is_not_reported(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertNotIn("setCap(uint256)", [c.signature for c in diff.bodies.changed])

    def test_scope_names_the_contract_and_file(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.bodies.scope, "Vault @ src/Vault.sol")


class TestAddedAndRemovedFunctions(ImplDiffTestCase):
    """Comparing only the intersection loses whole functions.

    Regression: an upgrade that moves logic out of a function into a new internal
    helper showed the emptied function and nothing else — the helper that now
    holds the behavior, and any deleted hook, were both invisible.
    """

    OLD = """
    contract Vault is Base {
        function deposit(uint256 amount) external {
            _accrue();
            _deploy(amount);
        }

        function _deploy(uint256 amount) internal { strategy.push(amount); }

        function _preTransferHook(address from, address to) internal { _check(from, to); }
    }
    """
    NEW = """
    contract Vault is Base {
        function deposit(uint256 amount) external {
            _accrue();
            _postDepositHook(amount);
        }

        function _deploy(uint256 amount) internal { strategy.push(amount); }

        function _postDepositHook(uint256 amount) internal {
            fenced += amount;
            _deploy(amount);
        }
    }
    """

    def setUp(self) -> None:
        super().setUp()
        self.old_entry = _bundle(self.OLD, OLD_ABI)
        self.new_entry = _bundle(self.NEW, OLD_ABI)

    def test_added_internal_function_is_reported(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual([c.signature for c in diff.bodies.added], ["_postDepositHook(uint256)"])
        self.assertEqual(diff.bodies.added[0].visibility, "internal")

    def test_added_function_carries_its_body(self) -> None:
        """A signature alone doesn't explain behavior moved into a new helper."""
        diff = self.run_diff()
        assert diff is not None
        self.assertIn("fenced += amount;", diff.bodies.added[0].diff)

    def test_removed_internal_function_is_reported(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual([c.signature for c in diff.bodies.removed], ["_preTransferHook(address,address)"])

    def test_unchanged_internal_function_is_not_reported(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        for entries in (diff.bodies.added, diff.bodies.removed, diff.bodies.changed):
            self.assertNotIn("_deploy(uint256)", [c.signature for c in entries])

    def test_rendered_sections_are_labeled(self) -> None:
        rendered = format_impl_diff(self.run_diff())
        self.assertIn("Added (internal/private", rendered)
        self.assertIn("+ _postDepositHook(uint256) internal", rendered)
        self.assertIn("No longer defined here", rendered)
        self.assertIn("- _preTransferHook(address,address) internal", rendered)

    def test_external_functions_are_left_to_the_abi_section(self) -> None:
        """`sweep` is a genuine external addition; it must not be listed twice."""
        self.old_entry = _bundle(TARGET_OLD, OLD_ABI)
        self.new_entry = _bundle(TARGET_NEW, NEW_ABI)
        diff = self.run_diff()
        assert diff is not None and diff.surface is not None
        self.assertIn("sweep(address)", [f.signature for f in diff.surface.added])
        self.assertEqual(diff.bodies.added, [])
        self.assertEqual(format_impl_diff(diff).count("sweep(address)"), 1)


class TestStringLiteralChanges(ImplDiffTestCase):
    """A changed string is a behavior change, and must reach the prompt.

    Regression: string literals were blanked before bodies were compared, so an
    upgrade that only changed a revert message, a role identifier or a token
    name reported no body change at all.
    """

    OLD = """
    contract Vault is Base {
        function setCap(uint256 newCap) external onlyOwner { cap = newCap; }

        function totalAssets() public view returns (uint256) {
            require(!paused, "Vault: paused");
            return asset.balanceOf(address(this));
        }
    }
    """
    NEW = OLD.replace('"Vault: paused"', '"Vault: halted by guardian"')

    def setUp(self) -> None:
        super().setUp()
        self.old_entry = _bundle(self.OLD, OLD_ABI)
        self.new_entry = _bundle(self.NEW, OLD_ABI)

    def test_changed_revert_message_is_reported_as_a_body_change(self) -> None:
        diff = self.run_diff()
        assert diff is not None and diff.surface is not None
        self.assertTrue(diff.surface.is_empty, "the ABI is identical; only the body changed")
        self.assertEqual([c.signature for c in diff.bodies.changed], ["totalAssets()"])

    def test_both_message_versions_appear_in_the_rendered_diff(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        rendered = format_impl_diff(diff)
        self.assertIn("Vault: paused", rendered)
        self.assertIn("Vault: halted by guardian", rendered)


class TestAmbiguousTarget(ImplDiffTestCase):
    """Two files declare the contract: report body analysis unavailable, don't guess."""

    def setUp(self) -> None:
        super().setUp()
        payload = json.loads(self.new_entry["SourceCode"][1:-1])
        payload["sources"]["src/copies/Vault.sol"] = {"content": TARGET_OLD}
        self.new_entry["SourceCode"] = "{" + json.dumps(payload) + "}"

    def test_body_analysis_unavailable_but_abi_still_diffed(self) -> None:
        diff = self.run_diff()
        assert diff is not None and diff.surface is not None
        self.assertIsNone(diff.bodies.scope)
        self.assertEqual(diff.bodies.changed, [])
        self.assertEqual([f.signature for f in diff.surface.added], ["sweep(address)"])
        rendered = format_impl_diff(diff)
        self.assertIn("NOT COMPARED", rendered)
        self.assertIn("could not resolve the deployed contract", rendered)


class TestStorageVerdicts(ImplDiffTestCase):
    def test_gap_consumption_is_compatible(self) -> None:
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.COMPATIBLE)
        self.assertEqual([e.label for e in diff.storage.added], ["buffer", "__gap"])

    def test_missing_one_side_is_unknown(self) -> None:
        self.new_layout = None
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.UNKNOWN)
        self.assertIn("new implementation", diff.storage.reason)

    def test_reordered_slot_is_incompatible(self) -> None:
        self.new_layout = _layout([(0, 0, "buffer", "t_address"), (1, 0, "cap", "t_uint256")])
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.INCOMPATIBLE)
        self.assertIn("slot 0+0", format_impl_diff(diff))

    def test_unknown_storage_never_renders_as_compatible(self) -> None:
        self.old_layout = None
        self.new_layout = None
        diff = self.run_diff()
        assert diff is not None
        rendered = format_impl_diff(diff)
        self.assertIn("Storage compatibility: UNKNOWN", rendered)
        self.assertIn("inspect compiler layouts manually", rendered)
        self.assertNotIn("COMPATIBLE (", rendered)


def _namespaced_base(members: str, namespace: str = "yearn.storage.Base") -> str:
    """A base declaring an ERC-7201 namespace the standard way: annotated struct plus an
    accessor at the root the annotation defines."""
    return f"""
    abstract contract Base {{
        /// @custom:storage-location erc7201:{namespace}
        struct BaseStorage {{ {members} }}

        function _getBaseStorage() private pure returns (BaseStorage storage $) {{
            assembly {{ $.slot := 0x{erc7201_root(namespace):064x} }}
        }}

        function setProfitMaxUnlockTime(uint256 t) external virtual;
    }}
    """


class TestNamespacedStorage(ImplDiffTestCase):
    """ERC-7201 namespaces never appear in the positional layout, so a positional
    match must not clear them.

    Regression: only the natspec on the contract declaration was checked, but the
    ERC puts the annotation on the struct *inside* the contract — usually in an
    inherited base like OpenZeppelin's Initializable — so real namespaces were
    never seen and a changed one could still read as COMPATIBLE.
    """

    def _with_bases(self, old_base: str, new_base: str) -> None:
        self.old_entry = _bundle(TARGET_OLD, OLD_ABI, base=old_base)
        self.new_entry = _bundle(TARGET_NEW, NEW_ABI, base=new_base)

    def test_changed_inherited_namespace_downgrades_to_unknown(self) -> None:
        self._with_bases(_namespaced_base("uint64 a; bool b;"), _namespaced_base("bool b; uint64 a;"))
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.UNKNOWN)
        self.assertEqual(diff.storage.status, StorageCompatibility.COMPATIBLE, "positional result kept")
        self.assertIn("struct definition changed", format_impl_diff(diff))

    def test_positional_detail_survives_the_downgrade(self) -> None:
        self._with_bases(_namespaced_base("uint64 a;"), _namespaced_base("uint128 a;"))
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual([e.label for e in diff.storage.added], ["buffer", "__gap"])

    def test_unchanged_inherited_namespace_keeps_compatible(self) -> None:
        """OpenZeppelin v5 bases are namespaced; an untouched one must not block the verdict."""
        base = _namespaced_base("uint64 _initialized; bool _initializing;", "openzeppelin.storage.Initializable")
        self._with_bases(base, base)
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.COMPATIBLE)
        self.assertEqual(diff.namespaces.unchanged, ["erc7201:openzeppelin.storage.Initializable"])
        self.assertIn("Namespaced storage unchanged (ERC-7201, root verified)", format_impl_diff(diff))

    def test_namespace_in_the_target_body_is_found(self) -> None:
        body = "/// @custom:storage-location erc7201:yearn.storage.Vault\n    struct VaultStorage { uint256 x; }\n"
        old = TARGET_OLD.replace("contract Vault is Base {", "contract Vault is Base {\n    " + body)
        new = TARGET_NEW.replace(
            "contract Vault is Base {", "contract Vault is Base {\n    " + body.replace("uint256 x", "int256 x")
        )
        self.old_entry = _bundle(old, OLD_ABI)
        self.new_entry = _bundle(new, NEW_ABI)
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.UNKNOWN)

    def test_added_namespace_is_not_validated(self) -> None:
        self._with_bases(BASE_CONTRACT, _namespaced_base("uint256 x;"))
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.UNKNOWN)
        self.assertIn("added in the new implementation", format_impl_diff(diff))

    def test_user_defined_member_type_cannot_be_proven_unchanged(self) -> None:
        """Identical text can hide a changed referenced type, so it isn't accepted."""
        base = _namespaced_base("Status s; uint256 x;")
        self._with_bases(base, base)
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.UNKNOWN)
        self.assertIn("user-defined types", format_impl_diff(diff))

    def test_imported_library_namespace_is_ignored(self) -> None:
        """An imported helper is not the contract's storage — the original #367 failure."""
        library = """
        library StrategyStorageLib {
            /// @custom:storage-location erc7201:yearn.storage.Strategy
            struct StrategyData { uint256 totalAssets; }
        }
        """
        changed = library.replace("uint256 totalAssets", "int256 totalAssets")
        self.old_entry = _bundle(TARGET_OLD, OLD_ABI, extra={"src/lib/StrategyStorageLib.sol": library})
        self.new_entry = _bundle(TARGET_NEW, NEW_ABI, extra={"src/lib/StrategyStorageLib.sol": changed})
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.COMPATIBLE)
        self.assertEqual(diff.namespaces.unchanged, [])

    def test_positional_conflict_still_reported(self) -> None:
        """An unvalidated namespace never hides a real positional conflict."""
        self._with_bases(_namespaced_base("uint64 a;"), _namespaced_base("uint128 a;"))
        self.new_layout = _layout([(0, 0, "buffer", "t_address")])
        diff = self.run_diff()
        assert diff is not None
        self.assertEqual(diff.storage_status, StorageCompatibility.INCOMPATIBLE)


class TestConsistencyGate(ImplDiffTestCase):
    def test_identical_additions_for_a_second_contract_are_flagged_not_withheld(self) -> None:
        """The original incident produced identical additions for two contracts.

        Each set is now proven against its own ABI, so the result stands — but
        the coincidence is named so a reviewer can check provenance.
        """
        first = self.run_diff()
        assert first is not None and first.surface is not None
        self.assertEqual(first.surface_note, "")

        self.old_entry = _bundle(TARGET_OLD, OLD_ABI, name="OtherVault")
        self.new_entry = _bundle(TARGET_NEW, NEW_ABI, name="OtherVault")
        second = self.run_diff()
        assert second is not None and second.surface is not None
        self.assertIn("also reported for Vault", second.surface_note)
        rendered = format_impl_diff(second)
        self.assertIn("+ sweep(address)", rendered)
        self.assertIn("verified against its own contract's ABI", rendered)

    def test_same_contract_diffed_twice_is_not_flagged(self) -> None:
        self.assertIsNotNone(self.run_diff())
        again = self.run_diff()
        assert again is not None
        self.assertIsNotNone(again.surface)
        self.assertEqual(again.surface_note, "")

    def test_addition_missing_from_the_new_abi_withholds_the_section(self) -> None:
        """A surface claim that can't be re-derived from the ABI is dropped, not shown."""
        diff = None
        with patch("utils.impl_diff.abi_functions", return_value={}):
            diff = self.run_diff()
        assert diff is not None
        self.assertIsNone(diff.surface)
        rendered = format_impl_diff(diff)
        self.assertIn("NOT AVAILABLE", rendered)
        self.assertIn("the ABIs disagree", rendered)
        self.assertNotIn("+ sweep(address)", rendered)


class TestUnavailableInputs(ImplDiffTestCase):
    def test_unverified_implementation_returns_none(self) -> None:
        with patch("utils.impl_diff.fetch_verified_contract", return_value=None):
            self.assertIsNone(diff_implementations("0xOld", "0xNew", 1))

    def test_missing_abi_reports_the_surface_as_unavailable(self) -> None:
        self.new_entry = _bundle(TARGET_NEW, "Contract source code not verified")
        diff = self.run_diff()
        assert diff is not None
        self.assertIsNone(diff.surface)
        self.assertIn("no ABI available", format_impl_diff(diff))


if __name__ == "__main__":
    unittest.main()
