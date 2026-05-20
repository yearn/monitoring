"""Tests for prices/main.py — DefiLlama market-ratio depeg checks.

Focuses on the fair-value normalization math: a 2% deviation against an asset's
fair_value should fire, but the same absolute price against a 1.0 baseline
should not. Network and Telegram calls are stubbed.
"""

import importlib
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch


def _import_prices_main():
    """Fresh import of prices.main with a stubbed defillama_sdk."""
    fake_sdk = types.ModuleType("defillama_sdk")
    fake_sdk.DefiLlama = MagicMock()
    sys.modules["defillama_sdk"] = fake_sdk
    sys.modules.pop("utils.defillama", None)
    sys.modules.pop("prices.main", None)
    return importlib.import_module("prices.main")


class TestDefiLlamaFairValue(unittest.TestCase):
    """check_defillama_assets normalizes market_ratio by per-asset fair_value."""

    def setUp(self):
        self.prices = _import_prices_main()
        # Patch send_alert + fetch_prices on the module under test.
        self.send_alert = patch.object(self.prices, "send_alert").start()
        self.fetch_prices = patch.object(self.prices, "fetch_prices").start()
        self.addCleanup(patch.stopall)

    def _set_assets(self, assets):
        patch.object(self.prices, "DEFILLAMA_ASSETS", assets).start()

    def test_lrt_above_floor_does_not_alert(self):
        # weETH @ 1.07 ETH = exactly fair_value; deviation = 1.0 > 0.98 → no alert
        asset = self.prices.DefiLlamaAsset("weETH", "ethereum:0xweeth", "ETH", "lrt", Decimal("1.07"))
        self._set_assets([asset])
        self.fetch_prices.return_value = {
            self.prices.WETH_KEY: Decimal("2000"),
            "ethereum:0xweeth": Decimal("2140"),  # 2140 / 2000 = 1.07
        }
        self.prices.check_defillama_assets()
        self.send_alert.assert_not_called()

    def test_lrt_below_floor_alerts_critical(self):
        # weETH at 1.04 ETH against fair_value 1.07: deviation = 1.04/1.07 ≈ 0.972 < 0.98
        asset = self.prices.DefiLlamaAsset("weETH", "ethereum:0xweeth", "ETH", "lrt", Decimal("1.07"))
        self._set_assets([asset])
        self.fetch_prices.return_value = {
            self.prices.WETH_KEY: Decimal("2000"),
            "ethereum:0xweeth": Decimal("2080"),  # 2080 / 2000 = 1.04
        }
        self.prices.check_defillama_assets()
        self.send_alert.assert_called_once()
        alert = self.send_alert.call_args.args[0]
        self.assertEqual(alert.severity, self.prices.AlertSeverity.CRITICAL)
        self.assertEqual(alert.protocol, "lrt")
        self.assertIn("weETH", alert.message)

    def test_flat_baseline_would_miss_lrt_depeg(self):
        # Same 1.04 ETH price, but fair_value=1.0 — deviation = 1.04 > 0.98, no alert.
        # This is the bug the fair_value design prevents.
        asset = self.prices.DefiLlamaAsset("weETH-flat", "ethereum:0xweeth", "ETH", "lrt", Decimal("1.0"))
        self._set_assets([asset])
        self.fetch_prices.return_value = {
            self.prices.WETH_KEY: Decimal("2000"),
            "ethereum:0xweeth": Decimal("2080"),
        }
        self.prices.check_defillama_assets()
        self.send_alert.assert_not_called()

    def test_stable_below_threshold_alerts(self):
        # FDUSD at $0.97 vs 1.0 fair_value: deviation = 0.97 < 0.98 → alert
        asset = self.prices.DefiLlamaAsset("FDUSD", "ethereum:0xfdusd", "USD", "stables")
        self._set_assets([asset])
        self.fetch_prices.return_value = {"ethereum:0xfdusd": Decimal("0.97")}
        self.prices.check_defillama_assets()
        self.send_alert.assert_called_once()
        alert = self.send_alert.call_args.args[0]
        self.assertEqual(alert.severity, self.prices.AlertSeverity.CRITICAL)
        self.assertEqual(alert.protocol, "stables")

    def test_missing_price_emits_medium_coverage_alert(self):
        asset = self.prices.DefiLlamaAsset("FDUSD", "ethereum:0xfdusd", "USD", "stables")
        self._set_assets([asset])
        self.fetch_prices.return_value = {}  # no price returned
        self.prices.check_defillama_assets()
        self.send_alert.assert_called_once()
        alert = self.send_alert.call_args.args[0]
        self.assertEqual(alert.severity, self.prices.AlertSeverity.MEDIUM)
        self.assertEqual(alert.protocol, "stables")
        self.assertIn("FDUSD", alert.message)

    def test_fetch_failure_notifies_each_affected_protocol(self):
        assets = [
            self.prices.DefiLlamaAsset("weETH", "ethereum:0xweeth", "ETH", "lrt", Decimal("1.07")),
            self.prices.DefiLlamaAsset("FDUSD", "ethereum:0xfdusd", "USD", "stables"),
        ]
        self._set_assets(assets)
        self.fetch_prices.side_effect = RuntimeError("upstream timeout")
        self.prices.check_defillama_assets()
        # One LOW alert per affected protocol, not just the first.
        protocols = {call.args[0].protocol for call in self.send_alert.call_args_list}
        self.assertEqual(protocols, {"lrt", "stables"})
        for call in self.send_alert.call_args_list:
            self.assertEqual(call.args[0].severity, self.prices.AlertSeverity.LOW)


if __name__ == "__main__":
    unittest.main()
