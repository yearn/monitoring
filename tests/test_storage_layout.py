"""Tests for utils/storage_layout.py."""

import unittest

from utils.sourcify_layout import StorageLayout
from utils.storage_layout import StorageCompatibility, compare_storage_layouts

TYPES = {
    "t_uint256": {"label": "uint256", "encoding": "inplace", "numberOfBytes": "32"},
    "t_uint128": {"label": "uint128", "encoding": "inplace", "numberOfBytes": "16"},
    "t_address": {"label": "address", "encoding": "inplace", "numberOfBytes": "20"},
    "t_bool": {"label": "bool", "encoding": "inplace", "numberOfBytes": "1"},
    "t_mapping(t_address,t_bool)": {
        "key": "t_address",
        "value": "t_bool",
        "label": "mapping(address => bool)",
        "encoding": "mapping",
        "numberOfBytes": "32",
    },
    "t_mapping(t_address,t_uint256)": {
        "key": "t_address",
        "value": "t_uint256",
        "label": "mapping(address => uint256)",
        "encoding": "mapping",
        "numberOfBytes": "32",
    },
    # Same shape, different compiler-generated id — must compare equal.
    "t_contract(IMorpho)6874": {"label": "contract IMorpho", "encoding": "inplace", "numberOfBytes": "20"},
    "t_contract(IMorpho)6876": {"label": "contract IMorpho", "encoding": "inplace", "numberOfBytes": "20"},
    "t_struct(Params)10_storage": {
        "label": "struct Params",
        "encoding": "inplace",
        "numberOfBytes": "96",
        "members": [
            {"slot": "0", "offset": 0, "label": "loanToken", "type": "t_address", "astId": 1},
            {"slot": "1", "offset": 0, "label": "oracle", "type": "t_address", "astId": 2},
            {"slot": "2", "offset": 0, "label": "lltv", "type": "t_uint256", "astId": 3},
        ],
    },
    "t_struct(Params)99_storage": {
        "label": "struct Params",
        "encoding": "inplace",
        "numberOfBytes": "96",
        "members": [
            # Same positions and types, different member names and ast ids.
            {"slot": "0", "offset": 0, "label": "loan", "type": "t_address", "astId": 7},
            {"slot": "1", "offset": 0, "label": "priceFeed", "type": "t_address", "astId": 8},
            {"slot": "2", "offset": 0, "label": "ltv", "type": "t_uint256", "astId": 9},
        ],
    },
    "t_struct(Params)Reordered_storage": {
        "label": "struct Params",
        "encoding": "inplace",
        "numberOfBytes": "96",
        "members": [
            {"slot": "0", "offset": 0, "label": "lltv", "type": "t_uint256", "astId": 1},
            {"slot": "1", "offset": 0, "label": "loanToken", "type": "t_address", "astId": 2},
            {"slot": "2", "offset": 0, "label": "oracle", "type": "t_address", "astId": 3},
        ],
    },
    "t_array(t_uint256)50_storage": {
        "base": "t_uint256",
        "label": "uint256[50]",
        "encoding": "inplace",
        "numberOfBytes": "1600",
    },
    "t_array(t_uint256)47_storage": {
        "base": "t_uint256",
        "label": "uint256[47]",
        "encoding": "inplace",
        "numberOfBytes": "1504",
    },
}


def layout(*entries: tuple[int, int, str, str]) -> StorageLayout:
    """Build a layout from (slot, offset, label, type id) tuples."""
    return StorageLayout(
        address="0xtest",
        match="match",
        storage=[
            {"slot": str(slot), "offset": offset, "label": label, "type": type_id, "astId": i}
            for i, (slot, offset, label, type_id) in enumerate(entries)
        ],
        types=TYPES,
    )


class TestCoverage(unittest.TestCase):
    def test_missing_old_side_is_unknown(self) -> None:
        result = compare_storage_layouts(None, layout((0, 0, "cap", "t_uint256")))
        self.assertEqual(result.status, StorageCompatibility.UNKNOWN)
        self.assertIn("old implementation", result.reason)

    def test_missing_new_side_is_unknown(self) -> None:
        result = compare_storage_layouts(layout((0, 0, "cap", "t_uint256")), None)
        self.assertEqual(result.status, StorageCompatibility.UNKNOWN)
        self.assertIn("new implementation", result.reason)

    def test_missing_both_is_unknown(self) -> None:
        self.assertEqual(compare_storage_layouts(None, None).status, StorageCompatibility.UNKNOWN)

    def test_empty_layout_is_unknown(self) -> None:
        empty = StorageLayout(address="0x0", match="match", storage=[], types=TYPES)
        result = compare_storage_layouts(empty, layout((0, 0, "cap", "t_uint256")))
        self.assertEqual(result.status, StorageCompatibility.UNKNOWN)


class TestCompatibleChanges(unittest.TestCase):
    def test_identical_layouts(self) -> None:
        one = layout((0, 0, "cap", "t_uint256"))
        result = compare_storage_layouts(one, layout((0, 0, "cap", "t_uint256")))
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)
        self.assertEqual(result.added, [])
        self.assertEqual(result.renamed, [])

    def test_append_at_the_end(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "cap", "t_uint256")),
            layout((0, 0, "cap", "t_uint256"), (1, 0, "buffer", "t_uint256")),
        )
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)
        self.assertEqual([e.label for e in result.added], ["buffer"])

    def test_rename_at_the_same_slot(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "whitelist", "t_mapping(t_address,t_bool)")),
            layout((0, 0, "__deprecated_whitelist", "t_mapping(t_address,t_bool)")),
        )
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)
        self.assertEqual(result.renamed[0][1].label, "__deprecated_whitelist")

    def test_compiler_type_id_change_for_same_shape(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "morpho", "t_contract(IMorpho)6874")),
            layout((0, 0, "morpho", "t_contract(IMorpho)6876")),
        )
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)

    def test_struct_member_rename_is_not_a_conflict(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "params", "t_struct(Params)10_storage")),
            layout((0, 0, "params", "t_struct(Params)99_storage")),
        )
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)

    def test_gap_consumption_with_correct_shrink(self) -> None:
        old = layout((0, 0, "cap", "t_uint256"), (1, 0, "__gap", "t_array(t_uint256)50_storage"))
        new = layout(
            (0, 0, "cap", "t_uint256"),
            (1, 0, "a", "t_uint256"),
            (2, 0, "b", "t_uint256"),
            (3, 0, "c", "t_uint256"),
            (4, 0, "__gap", "t_array(t_uint256)47_storage"),
        )
        result = compare_storage_layouts(old, new)
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)
        self.assertEqual([e.label for e in result.added], ["a", "b", "c", "__gap"])
        self.assertTrue(result.consumed_gaps)

    def test_packed_variables_preserved(self) -> None:
        packed = ((0, 0, "owner", "t_address"), (0, 20, "paused", "t_bool"))
        result = compare_storage_layouts(layout(*packed), layout(*packed))
        self.assertEqual(result.status, StorageCompatibility.COMPATIBLE)


class TestIncompatibleChanges(unittest.TestCase):
    def test_reordered_variables(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "cap", "t_uint256"), (1, 0, "owner", "t_address")),
            layout((0, 0, "owner", "t_address"), (1, 0, "cap", "t_uint256")),
        )
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)
        self.assertEqual(len(result.conflicts), 2)

    def test_removed_variable(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "cap", "t_uint256"), (1, 0, "owner", "t_address")),
            layout((0, 0, "cap", "t_uint256")),
        )
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)
        self.assertIn("owner", result.conflicts[0])

    def test_type_width_change_at_same_slot(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "cap", "t_uint256")),
            layout((0, 0, "cap", "t_uint128")),
        )
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)

    def test_mapping_value_type_change(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "balances", "t_mapping(t_address,t_bool)")),
            layout((0, 0, "balances", "t_mapping(t_address,t_uint256)")),
        )
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)

    def test_struct_member_reorder_is_a_conflict(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "params", "t_struct(Params)10_storage")),
            layout((0, 0, "params", "t_struct(Params)Reordered_storage")),
        )
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)

    def test_inserted_variable_shifts_everything_after_it(self) -> None:
        """Inheriting a new base variable moves every following slot."""
        old = layout((0, 0, "cap", "t_uint256"), (1, 0, "owner", "t_address"))
        new = layout(
            (0, 0, "inserted", "t_uint256"),
            (1, 0, "cap", "t_uint256"),
            (2, 0, "owner", "t_address"),
        )
        result = compare_storage_layouts(old, new)
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)

    def test_packing_change_is_a_conflict(self) -> None:
        result = compare_storage_layouts(
            layout((0, 0, "owner", "t_address"), (0, 20, "paused", "t_bool")),
            layout((0, 0, "owner", "t_address"), (1, 0, "paused", "t_bool")),
        )
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)
        self.assertIn("paused", result.conflicts[0])

    def test_gap_overconsumed_into_following_variable(self) -> None:
        """A base gap shrank too far, so a derived variable moved."""
        old = layout(
            (0, 0, "__gap", "t_array(t_uint256)50_storage"),
            (50, 0, "derived", "t_uint256"),
        )
        new = layout(
            (0, 0, "__gap", "t_array(t_uint256)47_storage"),
            (47, 0, "derived", "t_uint256"),
        )
        result = compare_storage_layouts(old, new)
        self.assertEqual(result.status, StorageCompatibility.INCOMPATIBLE)
        self.assertIn("derived", result.conflicts[0])


if __name__ == "__main__":
    unittest.main()
