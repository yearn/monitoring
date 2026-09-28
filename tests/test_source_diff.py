"""Tests for utils/source_diff.py — whole-bundle file and compiler-settings diffs."""

import unittest

from utils import source_diff
from utils.source_diff import diff_compiler_settings, diff_source_files, format_source_files, linked_libraries
from utils.verified_contract import VerifiedContract

RATE_ORACLE_OLD = """contract RateOracle {
    function setRestakerRate(address _agent, uint256 _rate) external {
        restakerRate[_agent] = _rate;
    }
}
"""

RATE_ORACLE_NEW = """contract RateOracle {
    function setRestakerRate(address _agent, uint256 _rate) external {
        if (_rate > 1e27) revert RateTooHigh(_agent, _rate);
        restakerRate[_agent] = _rate;
    }
}
"""


def _contract(settings: dict, compiler: str = "v0.8.28+commit.7893614a") -> VerifiedContract:
    return VerifiedContract(
        contract_name="CapToken",
        compiler_version=compiler,
        language="Solidity",
        sources={"contracts/token/CapToken.sol": "contract CapToken {}"},
        settings=settings,
    )


class TestDiffSourceFiles(unittest.TestCase):
    def test_change_in_an_inherited_file_is_reported(self) -> None:
        old = {"contracts/Oracle.sol": "contract Oracle is RateOracle {}", "contracts/RateOracle.sol": RATE_ORACLE_OLD}
        new = {"contracts/Oracle.sol": "contract Oracle is RateOracle {}", "contracts/RateOracle.sol": RATE_ORACLE_NEW}
        diff = diff_source_files(old, new)
        self.assertEqual([c.path for c in diff.changed], ["contracts/RateOracle.sol"])
        self.assertEqual((diff.changed[0].added_lines, diff.changed[0].removed_lines), (1, 0))
        self.assertIn("+        if (_rate > 1e27) revert RateTooHigh", diff.changed[0].diff)

    def test_identical_bundles_are_empty(self) -> None:
        sources = {"a.sol": "contract A {}"}
        diff = diff_source_files(sources, dict(sources))
        self.assertTrue(diff.is_empty)
        self.assertIn("every file in the verified bundle is identical", format_source_files(diff, "Files")[0])

    def test_added_and_removed_files(self) -> None:
        diff = diff_source_files({"a.sol": "A", "gone.sol": "G"}, {"a.sol": "A", "Guard.sol": "contract Guard {}"})
        self.assertEqual([c.path for c in diff.added], ["Guard.sol"])
        self.assertEqual(diff.removed, ["gone.sol"])

    def test_moved_file_with_identical_content_is_not_a_change(self) -> None:
        diff = diff_source_files(
            {"lib/oz/Math.sol": "library Math {}"}, {"node_modules/oz/Math.sol": "library Math {}"}
        )
        self.assertEqual(diff.moved, [("lib/oz/Math.sol", "node_modules/oz/Math.sol")])
        self.assertEqual((diff.changed, diff.added, diff.removed), ([], [], []))

    def test_moved_and_edited_file_is_paired_by_basename(self) -> None:
        diff = diff_source_files({"lib/RateOracle.sol": RATE_ORACLE_OLD}, {"src/RateOracle.sol": RATE_ORACLE_NEW})
        self.assertEqual(len(diff.changed), 1)
        self.assertEqual(diff.changed[0].old_path, "lib/RateOracle.sol")
        self.assertEqual(diff.changed[0].added_lines, 1)
        self.assertEqual((diff.added, diff.removed), ([], []))

    def test_budget_prefers_project_files_over_dependencies(self) -> None:
        old = {f"node_modules/dep{i}.sol": "a" for i in range(source_diff.MAX_DIFFED_FILES)}
        old["contracts/Vault.sol"] = "a"
        new = {path: "b" for path in old}
        diff = diff_source_files(old, new)
        self.assertEqual(diff.changed[0].path, "contracts/Vault.sol")
        self.assertTrue(diff.changed[0].diff)
        self.assertEqual(diff.changed[-1].diff, "")
        self.assertIn("diff omitted", "\n".join(format_source_files(diff, "Files")))

    def test_many_small_hunks_are_cut_at_a_hunk_boundary(self) -> None:
        # Twenty separated one-line edits, the shape of `nonReentrant` added to
        # several functions: the first hunks must survive the budget intact.
        old_lines = [f"line {i}" for i in range(200)]
        new_lines = [f"guarded {i}" if i % 10 == 0 else line for i, line in enumerate(old_lines)]
        change = diff_source_files({"Vault.sol": "\n".join(old_lines)}, {"Vault.sol": "\n".join(new_lines)}).changed[0]
        self.assertIn("+guarded 0", change.diff)
        self.assertNotIn("+guarded 190", change.diff)
        self.assertRegex(change.diff, r"… \d+ more hunk\(s\) omitted — read the source$")
        self.assertLessEqual(len(change.diff.splitlines()), source_diff.MAX_FILE_DIFF_LINES + 1)
        last_shown = change.diff.splitlines()[-2]
        self.assertFalse(last_shown.startswith(("-", "@@")), "a hunk was cut mid-way")

    def test_oversized_file_diff_is_omitted_not_truncated(self) -> None:
        old = "\n".join(f"line {i}" for i in range(100))
        new = "\n".join(f"changed {i}" for i in range(100))
        change = diff_source_files({"a.sol": old}, {"a.sol": new}).changed[0]
        self.assertEqual(change.diff, "")
        self.assertEqual((change.added_lines, change.removed_lines), (100, 100))


class TestDiffCompilerSettings(unittest.TestCase):
    def test_relinked_libraries_are_reported(self) -> None:
        old = _contract({"libraries": {"contracts/token/CapToken.sol": {"VaultLogic": "0xc7ea"}}})
        new = _contract({"libraries": {"contracts/token/CapToken.sol": {"VaultLogic": "0x651e"}}})
        changes = diff_compiler_settings(old, new)
        assert changes is not None
        self.assertEqual([str(c) for c in changes], ["linked library VaultLogic: 0xc7ea → 0x651e"])

    def test_address_case_is_not_a_relink(self) -> None:
        old = _contract({"libraries": {"f.sol": {"L": "0xABCD"}}})
        new = _contract({"libraries": {"f.sol": {"L": "0xabcd"}}})
        self.assertEqual(diff_compiler_settings(old, new), [])

    def test_evm_version_change_is_reported(self) -> None:
        changes = diff_compiler_settings(
            _contract({"evmVersion": "cancun", "libraries": {}}), _contract({"evmVersion": "prague"})
        )
        assert changes is not None
        self.assertEqual([str(c) for c in changes], ["evmVersion: cancun → prague"])

    def test_remappings_are_ignored(self) -> None:
        old = _contract({"remappings": ["a/=lib/a/"], "evmVersion": "prague"})
        new = _contract({"remappings": ["a/=node_modules/a/"], "evmVersion": "prague"})
        self.assertEqual(diff_compiler_settings(old, new), [])

    def test_optimizer_and_compiler_changes(self) -> None:
        old = _contract({"optimizer": {"enabled": True, "runs": 200}})
        new = _contract({"optimizer": {"enabled": True, "runs": 1000}}, compiler="v0.8.30+commit.73712a01")
        changes = diff_compiler_settings(old, new)
        assert changes is not None
        self.assertEqual([c.name for c in changes], ["compiler", "optimizer"])

    def test_missing_settings_is_unknown_not_unchanged(self) -> None:
        self.assertIsNone(diff_compiler_settings(_contract({}), _contract({"evmVersion": "prague"})))

    def test_linked_libraries_skips_malformed_entries(self) -> None:
        contract = _contract({"libraries": {"f.sol": {"L": "0x1", "Empty": ""}, "bad.sol": "0x2"}})
        self.assertEqual([(lib.name, lib.address) for lib in linked_libraries(contract)], [("L", "0x1")])


if __name__ == "__main__":
    unittest.main()
