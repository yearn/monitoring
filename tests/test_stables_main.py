"""Tests for protocols/stables/main.py depeg confirmation."""

import os
import tempfile
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from protocols.stables import main as stables
from utils.alert import Alert
from utils.cache import cache_filename, cache_timestamp_key, write_last_value_to_file
from utils.defillama import PricePoint

USDE_KEY = "ethereum:0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"
SYRUP_USDC_KEY = "ethereum:0x80ac24aa929eaf5013f6436cda2a7ba190f5cc0b"
USDE_CACHE_KEY = stables.CACHE_KEY_BREACH_PREFIX + USDE_KEY.replace(":", "_")
# Default for _run: DeFiLlama recorded a fresh datapoint shortly before this run.
NEW_SAMPLE: Any = object()


class TestDepegConfirmation(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_800_000_000

    def _points(self, usde: str, sample_ts: int | None) -> dict[str, PricePoint]:
        points = {key: PricePoint(threshold * 2, sample_ts) for _, key, _, threshold in stables.MONITORED_TOKENS}
        points[USDE_KEY] = PricePoint(Decimal(usde), sample_ts)
        return points

    def _run(self, usde: str, *, sample_ts: int | None = NEW_SAMPLE, advance: int = 600) -> list[Alert]:
        """Run one check ``advance`` seconds after the previous one."""
        self.now += advance
        timestamp = self.now - 30 if sample_ts is NEW_SAMPLE else sample_ts
        with (
            patch.object(stables, "fetch_price_points", return_value=self._points(usde, timestamp)),
            patch.object(stables, "send_alert") as send,
            patch.object(stables.time, "time", return_value=self.now),
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

    def test_file_backend_confirms_and_resets_breaches(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"CACHE_BACKEND": "file"}),
            patch.object(stables, "cache_filename", os.path.join(directory, "cache-id.txt")),
        ):
            self.assertFalse(self._run("0.90")[0].dispatch)
            self.assertTrue(self._run("0.90")[0].dispatch)
            self.assertEqual(self._run("1.00"), [])
            self.assertFalse(self._run("0.90")[0].dispatch)
            self.assertTrue(self._run("0.90")[0].dispatch)

    def test_missing_price_neither_confirms_nor_clears(self):
        self._run("0.90")
        points = self._points("1.00", self.now)
        del points[USDE_KEY]
        with patch.object(stables, "fetch_price_points", return_value=points), patch.object(stables, "send_alert"):
            stables.main()
        self.assertTrue(self._run("0.90")[0].dispatch)

    def test_stale_breach_does_not_confirm(self):
        self._run("0.90")
        stale = self._run("0.90", advance=stables.BREACH_CONFIRMATION_WINDOW_SECONDS + 1)
        self.assertFalse(stale[0].dispatch)
        self.assertIn("Unconfirmed", stale[0].message)
        self.assertTrue(self._run("0.90")[0].dispatch)

    def test_same_datapoint_served_twice_does_not_confirm(self):
        sample_ts = self.now - 60
        self.assertFalse(self._run("0.90", sample_ts=sample_ts)[0].dispatch)

        repeated = self._run("0.90", sample_ts=sample_ts)
        self.assertFalse(repeated[0].dispatch)
        self.assertIn("Unconfirmed", repeated[0].message)

        self.assertTrue(self._run("0.90", sample_ts=sample_ts + 600)[0].dispatch)

    def test_older_datapoint_does_not_confirm(self):
        sample_ts = self.now - 60
        self._run("0.90", sample_ts=sample_ts)
        self.assertFalse(self._run("0.90", sample_ts=sample_ts - 300)[0].dispatch)

    def test_rerun_too_soon_does_not_confirm(self):
        self._run("0.90")
        self.assertFalse(self._run("0.90", advance=stables.BREACH_MIN_SPACING_SECONDS - 1)[0].dispatch)
        # Spacing is measured from the counted sample, not from the rejected re-run.
        self.assertTrue(self._run("0.90", advance=1)[0].dispatch)

    def test_sample_without_timestamp_alerts_but_never_confirms(self):
        for _ in range(3):
            alerts = self._run("0.90", sample_ts=None)
            self.assertEqual(len(alerts), 1)
            self.assertFalse(alerts[0].dispatch)

    def test_corrupt_cache_values_do_not_crash(self):
        write_last_value_to_file(cache_filename, USDE_CACHE_KEY, "garbage")
        write_last_value_to_file(cache_filename, cache_timestamp_key(USDE_CACHE_KEY), "garbage")
        write_last_value_to_file(cache_filename, f"{USDE_CACHE_KEY}_sample_ts", "garbage")

        self.assertFalse(self._run("0.90")[0].dispatch)
        self.assertTrue(self._run("0.90")[0].dispatch)

    def test_future_breach_timestamp_is_treated_as_stale(self):
        write_last_value_to_file(cache_filename, USDE_CACHE_KEY, 5)
        write_last_value_to_file(cache_filename, cache_timestamp_key(USDE_CACHE_KEY), self.now + 100_000)

        self.assertFalse(self._run("0.90")[0].dispatch)

    def test_failed_send_does_not_skip_later_protocols(self):
        self.now += 600
        points = self._points("0.90", self.now)
        points[SYRUP_USDC_KEY] = PricePoint(Decimal("0.50"), self.now)
        with (
            patch.object(stables, "fetch_price_points", return_value=points),
            patch.object(stables, "send_alert", side_effect=[RuntimeError("telegram down"), True]) as send,
            patch.object(stables.time, "time", return_value=self.now),
            self.assertRaises(stables.DepegAlertDeliveryError) as raised,
        ):
            stables.main()

        self.assertEqual([call.args[0].protocol for call in send.call_args_list], ["ethena", "maple"])
        self.assertIn("ethena", str(raised.exception))
        self.assertNotIn("maple", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
