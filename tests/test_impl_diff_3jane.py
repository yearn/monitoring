"""Regression tests over the real 3Jane USD3 / sUSD3 upgrade (yearn/monitoring#367).

The previous extractor ran regexes over the concatenated Etherscan bundle and
produced the *same eight additions* for both upgrades — six of them USD3
functions, two of them `IMorpho` interface declarations — while missing sUSD3's
only real change and skipping the storage check because an imported library uses
namespaced storage. These tests pin the behavior against the frozen verified
sources for all four implementations (see `tests/fixtures/etherscan/README.md`).
"""

import gzip
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.impl_diff import diff_implementations, format_impl_diff, reset_provenance_registry
from utils.sourcify_layout import StorageLayout
from utils.storage_layout import StorageCompatibility
from utils.verified_contract import parse_etherscan_entry

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "etherscan"
LAYOUT_DIR = Path(__file__).parent / "fixtures" / "sourcify"

USD3_OLD = "0xB606fB370Eaaad03d71B49aE5E42AA4aEC7458D9"
USD3_NEW = "0xd1F1c3F485063712873285BF4ef25ab068f13893"
SUSD3_OLD = "0x529cbf11fFbC272D63858ca40A2C7F2695712073"
SUSD3_NEW = "0x6093d95f6C102163D19F5681E7D84997060BBAed"

_FIXTURES = {
    USD3_OLD: "usd3_old",
    USD3_NEW: "usd3_new",
    SUSD3_OLD: "susd3_old",
    SUSD3_NEW: "susd3_new",
}

# The authoritative surface delta, taken from the two verified USD3 ABIs.
USD3_EXPECTED_ADDED = [
    "releaseRingFence(uint256)",
    "ringFenceConduit(address)",
    "ringFencedLiquidity()",
    "setProfitMaxUnlockTime(uint256)",
    "setRingFenceConduit(address,bool)",
    "setSupplyCapExempt(address,bool)",
    "supplyCapExempt(address)",
]
USD3_EXPECTED_REMOVED = [
    "depositTimestamp(address)",
    "depositorWhitelist(address)",
    "initialize(address,bytes32,address,address)",
    "minCommitmentTime()",
    "reinitialize()",
    "setDepositorWhitelist(address,bool)",
    "setWhitelist(address,bool)",
    "setWhitelistEnabled(bool)",
    "whitelist(address)",
    "whitelistEnabled()",
]


def _section(rendered: str, title: str) -> str:
    """The lines of one rendered section, so a claim is checked where it's made."""
    out: list[str] = []
    for block in rendered.split("\n\n"):
        if block.startswith(title):
            out.append(block)
    return "\n".join(out)


def _read_fixture(directory: Path, address: str) -> dict:
    path = next(directory.glob(f"{_FIXTURES[address]}_*.json.gz"))
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _load(address: str):
    return parse_etherscan_entry(_read_fixture(FIXTURE_DIR, address)["result"][0])


def _fake_fetch(_chain_id: int, address: str):
    return _load(address)


def _fake_layout(_chain_id: int, address: str) -> StorageLayout | None:
    """Stands in for Sourcify, including its `match: null` no-coverage response."""
    payload = _read_fixture(LAYOUT_DIR, address)
    match, layout = payload.get("match"), payload.get("storageLayout")
    if not match or not layout:
        return None
    return StorageLayout(address=address, match=match, storage=layout["storage"], types=layout["types"])


class ThreeJaneDiffTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_provenance_registry()
        for target, fake in (
            ("utils.impl_diff.fetch_verified_contract", _fake_fetch),
            ("utils.impl_diff.fetch_storage_layout", _fake_layout),
        ):
            patcher = patch(target, side_effect=fake)
            self.addCleanup(patcher.stop)
            patcher.start()
        self.usd3 = diff_implementations(USD3_OLD, USD3_NEW, 1)
        self.susd3 = diff_implementations(SUSD3_OLD, SUSD3_NEW, 1)
        assert self.usd3 is not None and self.susd3 is not None
        assert self.usd3.surface is not None and self.susd3.surface is not None

    def test_usd3_surface_matches_target_abi(self) -> None:
        assert self.usd3 is not None and self.usd3.surface is not None
        self.assertEqual([f.signature for f in self.usd3.surface.added], USD3_EXPECTED_ADDED)
        self.assertEqual([f.signature for f in self.usd3.surface.removed], USD3_EXPECTED_REMOVED)

    def test_set_profit_max_unlock_time_not_masked_by_duplicate_signature(self) -> None:
        """The bundle declares this signature elsewhere; the ABI still shows the addition."""
        assert self.usd3 is not None and self.usd3.surface is not None
        self.assertIn("setProfitMaxUnlockTime(uint256)", [f.signature for f in self.usd3.surface.added])

    def test_susd3_has_no_external_surface_change(self) -> None:
        assert self.susd3 is not None and self.susd3.surface is not None
        self.assertTrue(self.susd3.surface.is_empty, f"unexpected sUSD3 surface delta: {self.susd3.surface}")

    def test_susd3_body_change_is_surfaced(self) -> None:
        """sUSD3's only material change lives inside availableDepositLimit's body."""
        assert self.susd3 is not None
        self.assertEqual([c.signature for c in self.susd3.changed_bodies], ["availableDepositLimit(address)"])
        self.assertEqual(self.susd3.body_scope, "sUSD3 @ src/usd3/sUSD3.sol")

    def test_morpho_interface_declarations_never_appear(self) -> None:
        """`clearMarketWindDown` is an IMorpho declaration, not a USD3/sUSD3 entry point."""
        for diff in (self.usd3, self.susd3):
            assert diff is not None
            rendered = format_impl_diff(diff)
            self.assertNotIn("clearMarketWindDown", rendered)
            self.assertNotIn("marketInWindDown", rendered)

    def test_internal_usd3_helpers_are_not_in_the_external_surface(self) -> None:
        """Private/internal helpers were reported as new entry points; they are not.

        They may still show up as changed bodies — that's a different, correctly
        labeled claim — so this is scoped to the ABI section.
        """
        for diff in (self.usd3, self.susd3):
            assert diff is not None
            section = _section(format_impl_diff(diff), "External ABI changes")
            for name in ("_pendingLoss", "_wrapUSDC", "_deployDepositedFunds"):
                self.assertNotIn(name, section)

    def test_commented_out_function_is_not_reported_as_removed(self) -> None:
        """`restartStrategy` is commented out in the old USD3 source."""
        assert self.usd3 is not None
        self.assertNotIn("restartStrategy", format_impl_diff(self.usd3))

    def test_usd3_changes_are_not_attributed_to_susd3(self) -> None:
        """The two upgrades must not produce the same additions (the original bug)."""
        assert self.usd3 is not None and self.susd3 is not None
        assert self.usd3.surface is not None and self.susd3.surface is not None
        usd3_added = {f.signature for f in self.usd3.surface.added}
        susd3_added = {f.signature for f in self.susd3.surface.added}
        self.assertEqual(usd3_added & susd3_added, set())
        for signature in ("setSupplyCapExempt(address,bool)", "setRingFenceConduit(address,bool)"):
            self.assertNotIn(signature, format_impl_diff(self.susd3))

    def test_usd3_storage_is_compatible_from_compiler_layouts(self) -> None:
        """Slots 0–62 hold; three new vars take 63–65; the gap shrinks 40 → 37 at slot 66."""
        assert self.usd3 is not None
        self.assertEqual(self.usd3.storage_status, StorageCompatibility.COMPATIBLE)
        self.assertEqual(self.usd3.storage.conflicts, [])
        self.assertEqual(
            [e.label for e in self.usd3.storage.added],
            ["supplyCapExempt", "ringFenceConduit", "ringFencedLiquidity", "__gap"],
        )

    def test_renames_at_the_same_slot_are_not_incompatible(self) -> None:
        """USD3 renamed four variables to `__deprecated_*` without moving them."""
        assert self.usd3 is not None
        renames = {before.label: after.label for before, after in self.usd3.storage.renamed}
        self.assertEqual(renames["whitelistEnabled"], "__deprecated_whitelistEnabled")
        self.assertEqual(renames["depositTimestamp"], "__deprecated_depositTimestamp")
        self.assertEqual(self.usd3.storage_status, StorageCompatibility.COMPATIBLE)

    def test_changed_compiler_type_id_for_the_same_type_is_not_a_conflict(self) -> None:
        """`morphoCredit` is t_contract(IMorpho)6874 in one build and …6876 in the other."""
        assert self.usd3 is not None
        self.assertNotIn("morphoCredit", " ".join(self.usd3.storage.conflicts))

    def test_one_sided_sourcify_coverage_is_unknown(self) -> None:
        """The new sUSD3 impl is not on Sourcify — that can never read as compatible."""
        assert self.susd3 is not None
        self.assertEqual(self.susd3.storage_status, StorageCompatibility.UNKNOWN)
        rendered = format_impl_diff(self.susd3)
        self.assertIn("Storage compatibility: UNKNOWN", rendered)
        self.assertIn("new implementation", self.susd3.storage.reason)
        self.assertNotIn("COMPATIBLE —", rendered)

    def test_imported_namespaced_helper_does_not_suppress_positional_analysis(self) -> None:
        """TokenizedStrategyStorageLib is namespaced; USD3's own storage is positional."""
        assert self.usd3 is not None
        self.assertEqual(self.usd3.storage_status, StorageCompatibility.COMPATIBLE)

    def test_every_line_carries_target_provenance(self) -> None:
        assert self.usd3 is not None and self.susd3 is not None
        usd3_text = format_impl_diff(self.usd3)
        self.assertIn("External ABI changes (USD3):", usd3_text)
        self.assertIn("src/usd3/USD3.sol", usd3_text)
        susd3_text = format_impl_diff(self.susd3)
        self.assertIn("External ABI changes (sUSD3):", susd3_text)
        self.assertIn("src/usd3/sUSD3.sol", susd3_text)

    def test_compilation_targets_resolve_without_a_compilation_target_setting(self) -> None:
        """Foundry verification drops settings.compilationTarget; the unique declaring file wins."""
        usd3 = _load(USD3_NEW)
        susd3 = _load(SUSD3_NEW)
        assert usd3 is not None and susd3 is not None
        self.assertNotIn("compilationTarget", usd3.settings)
        self.assertEqual(usd3.compilation_target, ("src/usd3/USD3.sol", "USD3"))
        self.assertEqual(susd3.compilation_target, ("src/usd3/sUSD3.sol", "sUSD3"))
        # The sUSD3 bundle also contains USD3.sol — resolution must not follow the import.
        self.assertIn("src/usd3/USD3.sol", susd3.sources)


if __name__ == "__main__":
    unittest.main()
