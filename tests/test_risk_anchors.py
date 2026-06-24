"""Tests for utils/risk_anchors.py."""

import unittest

from eth_utils import function_signature_to_4byte_selector

from utils.risk_anchors import _ANCHORS, _ANCHORS_BY_SIGNATURE, RiskAnchor, format_anchors_block, lookup


class TestLookup(unittest.TestCase):
    def test_known_high_risk_selector(self) -> None:
        anchor = lookup("0xf2fde38b")  # transferOwnership(address)
        assert anchor is not None
        self.assertEqual(anchor.level, "HIGH")

    def test_known_low_risk_selector(self) -> None:
        anchor = lookup("0x8456cb59")  # pause()
        assert anchor is not None
        self.assertEqual(anchor.level, "LOW")

    def test_case_insensitive(self) -> None:
        self.assertIsNotNone(lookup("0xF2FDE38B"))

    def test_unknown_selector_returns_none(self) -> None:
        self.assertIsNone(lookup("0xdeadbeef"))

    def test_empty_input(self) -> None:
        self.assertIsNone(lookup(""))
        self.assertIsNone(lookup("not-hex"))


class TestFormatAnchorsBlock(unittest.TestCase):
    def test_empty_input_returns_empty_string(self) -> None:
        self.assertEqual(format_anchors_block([]), "")

    def test_renders_signature_with_level_and_rationale(self) -> None:
        anchor = RiskAnchor("HIGH", "replaces all code")
        block = format_anchors_block([("upgradeTo(address)", anchor)])
        self.assertIn("upgradeTo(address)", block)
        self.assertIn("HIGH", block)
        self.assertIn("replaces all code", block)

    def test_renders_multiple_anchors(self) -> None:
        block = format_anchors_block(
            [
                ("pause()", RiskAnchor("LOW", "defensive")),
                ("transferOwnership(address)", RiskAnchor("HIGH", "full admin")),
            ]
        )
        self.assertIn("pause()", block)
        self.assertIn("transferOwnership(address)", block)


class TestAnchorRegistryIntegrity(unittest.TestCase):
    """Guards over the whole _ANCHORS table."""

    _VALID_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

    def test_all_keys_wellformed(self) -> None:
        for selector in _ANCHORS:
            self.assertTrue(selector.startswith("0x") and len(selector) == 10, f"bad selector {selector}")
            self.assertEqual(selector, selector.lower(), f"selector not lowercase: {selector}")

    def test_all_levels_valid(self) -> None:
        for selector, anchor in _ANCHORS.items():
            self.assertIn(anchor.level, self._VALID_LEVELS, f"{selector} has invalid level {anchor.level}")
            self.assertTrue(anchor.rationale, f"{selector} missing rationale")

    def test_selectors_are_derived_from_signature_table(self) -> None:
        expected = {
            "0x" + function_signature_to_4byte_selector(signature).hex(): anchor
            for signature, anchor in _ANCHORS_BY_SIGNATURE.items()
        }
        self.assertEqual(_ANCHORS, expected)

    def test_selectors_match_signatures(self) -> None:
        # Recompute the selector for each anchored signature to catch typos.
        expected = {
            "acceptOwnership()": "0x79ba5097",
            "mint(address,uint256)": "0x40c10f19",
            "addOwnerWithThreshold(address,uint256)": "0x0d582f13",
            "removeOwner(address,address,uint256)": "0xf8dc5dd9",
            "swapOwner(address,address,address)": "0xe318b52b",
            "changeThreshold(uint256)": "0x694e80c3",
            "enableModule(address)": "0x610b5925",
            "disableModule(address,address)": "0xe009cfde",
            "setGuard(address)": "0xe19a9dd9",
            "setFallbackHandler(address)": "0xf08a0323",
        }
        for sig, selector in expected.items():
            self.assertEqual("0x" + function_signature_to_4byte_selector(sig).hex(), selector, sig)
            self.assertIn(selector, _ANCHORS, f"{sig} ({selector}) not registered")

    def test_safe_module_is_critical(self) -> None:
        # enableModule can move funds with no owner signatures — highest band.
        anchor = lookup("0x610b5925")
        assert anchor is not None
        self.assertEqual(anchor.level, "CRITICAL")


if __name__ == "__main__":
    unittest.main()
