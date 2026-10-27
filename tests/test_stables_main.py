"""Tests for protocols/stables/main.py depeg confirmation."""

import unittest
from decimal import Decimal
from unittest.mock import patch

from protocols.stables import main as stables

USDE_KEY = "ethereum:0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"


def _prices(usde: str) -> dict[str, Decimal]:
    prices = {key: threshold * 2 for _, key, _, threshold in stables.MONITORED_TOKENS}
    prices[USDE_KEY] = Decimal(usde)
    return prices


class TestDepegConfirmation(unittest.TestCase):
    def _run(self, usde: str):
        with (
            patch.object(stables, "fetch_prices", return_value=_prices(usde)),
            patch.object(stables, "send_alert") as send,
        ):
            stables.main()
        return [call.args[0] for call in send.call_args_list]

    def test_first_breach_alerts_without_dispatch_then_second_confirms(self):
        first = self._run("0.90")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].protocol, "ethena")
        self.assertFalse(first[0].dispatch)
        self.assertIn("Unconfirmed", first[0].message)

        second = self._run("0.90")
        self.assertTrue(second[0].dispatch)
        self.assertNotIn("Unconfirmed", second[0].message)

    def test_recovery_resets_confirmation(self):
        self._run("0.90")
        self.assertEqual(self._run("1.00"), [])
        self.assertFalse(self._run("0.90")[0].dispatch)

    def test_missing_price_neither_confirms_nor_clears(self):
        self._run("0.90")
        prices = _prices("1.00")
        del prices[USDE_KEY]
        with patch.object(stables, "fetch_prices", return_value=prices), patch.object(stables, "send_alert"):
            stables.main()
        self.assertTrue(self._run("0.90")[0].dispatch)


if __name__ == "__main__":
    unittest.main()
