"""Tests for utils/tenderly/simulation.py.

Only our own logic is tested here. Request and response shapes belong to
Tenderly's API and change on their side, so mocking them proves nothing —
simulation is verified by running a real report instead.
"""

import unittest

from utils.tenderly.simulation import _merge_balance_override


class TestMergeBalanceOverride(unittest.TestCase):
    """Tests for _merge_balance_override."""

    def test_value_adds_balance_for_sender(self) -> None:
        out = _merge_balance_override(None, "0xExec", 10**18)
        self.assertEqual(out, {"0xExec": {"balance": hex(10**18)}})

    def test_caller_override_wins(self) -> None:
        existing = {"0xExec": {"balance": "0xdead", "storage": {"0x1": "0x2"}}}
        out = _merge_balance_override(existing, "0xExec", 10**18)
        # Caller-supplied balance is preserved; storage carried through.
        self.assertEqual(out["0xExec"]["balance"], "0xdead")
        self.assertEqual(out["0xExec"]["storage"], {"0x1": "0x2"})


if __name__ == "__main__":
    unittest.main()
