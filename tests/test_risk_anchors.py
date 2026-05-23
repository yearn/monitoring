"""Tests for utils/risk_anchors.py."""

import unittest

from utils.risk_anchors import RiskAnchor, format_anchors_block, lookup


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


if __name__ == "__main__":
    unittest.main()
