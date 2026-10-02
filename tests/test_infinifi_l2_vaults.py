import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from protocols.infinifi import l2_vaults
from protocols.infinifi.l2_vaults import (
    VaultReport,
    check_report,
    drop_message,
    fetch_l2_farm_values,
    fetch_reports,
    mismatch_message,
    stale_message,
)

VAULT = "0x4197F2ADFCe9fDeB36B02e96737Ddf646C841e45"
NOW = 1_791_000_000
HOUR = 3600


def _report(value: str, hours_ago: float = 1, chain_id: int = 8453) -> VaultReport:
    return VaultReport(chain_id, VAULT, Decimal(value), NOW - int(hours_ago * HOUR))


class TestDropMessage(unittest.TestCase):
    def test_write_down_to_zero_alerts(self) -> None:
        text = drop_message(_report("0"), Decimal("33355.45"))
        assert text is not None
        self.assertIn("base (8453)", text)
        self.assertIn("$33,355.45 → $0.00 (−100.0%)", text)
        self.assertIn(f"[{VAULT}](https://etherscan.io/address/{VAULT})", text)

    def test_normal_move_is_quiet(self) -> None:
        # Base moved 72.8k → 69.5k → 66.0k between reports in late September.
        self.assertIsNone(drop_message(_report("65954.23"), Decimal("69537.16")))

    def test_small_absolute_fall_is_quiet(self) -> None:
        self.assertIsNone(drop_message(_report("0", chain_id=143), Decimal("14.00")))

    def test_first_run_has_no_baseline(self) -> None:
        self.assertIsNone(drop_message(_report("0"), Decimal(0)))

    def test_unknown_chain_is_named_by_id(self) -> None:
        text = drop_message(_report("0", chain_id=143), Decimal("50000"))
        assert text is not None
        self.assertIn("chain 143", text)


class TestStaleMessage(unittest.TestCase):
    def test_old_report_with_value_alerts(self) -> None:
        text = stale_message(_report("76368.80", hours_ago=50), NOW)
        assert text is not None
        self.assertIn("(50h ago, threshold 48h)", text)

    def test_recent_report_is_quiet(self) -> None:
        self.assertIsNone(stale_message(_report("76368.80", hours_ago=22), NOW))

    def test_dust_vault_is_quiet(self) -> None:
        self.assertIsNone(stale_message(_report("14.00", hours_ago=500, chain_id=143), NOW))


class TestMismatchMessage(unittest.TestCase):
    def test_injected_zero_against_funded_l2_alerts(self) -> None:
        text = mismatch_message(_report("0"), Decimal("76371.81"))
        assert text is not None
        self.assertIn("Booked on mainnet: $0.00", text)
        self.assertIn("On the L2 per infiniFi's API: $76,371.81", text)
        self.assertIn("(100.0%)", text)

    def test_report_lag_is_quiet(self) -> None:
        # 02/10: mainnet 76,368.80 vs API 76,371.81.
        self.assertIsNone(mismatch_message(_report("76368.80"), Decimal("76371.81")))

    def test_dust_difference_is_quiet(self) -> None:
        self.assertIsNone(mismatch_message(_report("14.00", chain_id=143), Decimal("23.01")))

    def test_overstated_mainnet_alerts(self) -> None:
        self.assertIsNotNone(mismatch_message(_report("100000"), Decimal("50000")))


class TestFetchL2FarmValues(unittest.TestCase):
    @patch.object(l2_vaults, "fetch_json")
    def test_sums_l2_farms_per_chain(self, fetch: MagicMock) -> None:
        fetch.return_value = {
            "data": {
                "farms": [
                    {"chain": "MAINNET", "isL2LiquidityFarm": False, "assetsNormalized": 2_000_000},
                    {"chain": "BASE", "isL2LiquidityFarm": True, "assetsNormalized": 76371.813053},
                    {"chain": "BASE", "isL2LiquidityFarm": True, "assetsNormalized": 10},
                    {"chain": "MONAD", "isL2LiquidityFarm": True, "assetsNormalized": 23.006463},
                    {"chain": "NEWCHAIN", "isL2LiquidityFarm": True, "assetsNormalized": 5},
                ]
            }
        }
        self.assertEqual(fetch_l2_farm_values(), {8453: Decimal("76381.813053"), 143: Decimal("23.006463")})

    @patch.object(l2_vaults, "fetch_json", return_value=None)
    def test_api_failure_skips_comparison(self, _fetch: MagicMock) -> None:
        self.assertIsNone(fetch_l2_farm_values())


class _Cache:
    """In-memory stand-in for utils.cache with the real freshness rule."""

    def __init__(self, now: int) -> None:
        self.now = now
        self.values: dict[str, object] = {}

    def set(self, key: str, value: object, age_seconds: int) -> None:
        self.values[key] = value
        self.values[f"{key}_timestamp"] = self.now - age_seconds

    def last(self, _file: str, key: str) -> object:
        return self.values.get(key, 0)

    def fresh(self, _file: str, key: str, stale_after: int) -> object:
        written = int(self.values.get(f"{key}_timestamp", 0))
        return 0 if written == 0 or self.now - written > stale_after else self.values.get(key, 0)

    def write(self, _file: str, key: str, value: object) -> None:
        self.set(key, value, 0)


class TestCheckReport(unittest.TestCase):
    VALUE_KEY = f"infinifi_l2_vault_value_{VAULT.lower()}"
    STALE_KEY = f"infinifi_l2_vault_stale_{VAULT.lower()}"

    def _run(self, cache: _Cache, report: VaultReport, l2_value: Decimal | None = None) -> MagicMock:
        with (
            patch.object(l2_vaults, "cache_timestamp_key", side_effect=lambda key: f"{key}_timestamp"),
            patch.object(l2_vaults, "get_last_value_for_key_from_file", side_effect=cache.last),
            patch.object(l2_vaults, "get_fresh_last_value_for_key_from_file", side_effect=cache.fresh),
            patch.object(l2_vaults, "write_last_value_with_timestamp_to_file", side_effect=cache.write),
            patch.object(l2_vaults, "send_alert") as send,
        ):
            check_report(report, NOW, l2_value)
        return send

    def test_drop_alerts_and_updates_baseline(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.VALUE_KEY, "33355.45", HOUR)
        send = self._run(cache, _report("0"))
        send.assert_called_once()
        self.assertIn("Value Drop", send.call_args.args[0].message)
        self.assertEqual(cache.values[self.VALUE_KEY], "0")
        self.assertEqual(cache.values[self.STALE_KEY], 0)

    def test_drop_during_monitoring_gap_still_alerts(self) -> None:
        # The baseline is 5h old, past the 3h fresh window: the drop must still be caught.
        cache = _Cache(NOW)
        cache.set(self.VALUE_KEY, "76368.80", 5 * HOUR)
        send = self._run(cache, _report("0"))
        send.assert_called_once()
        self.assertIn("Previous reading:", send.call_args.args[0].message)

    def test_first_run_does_not_alert(self) -> None:
        send = self._run(_Cache(NOW), _report("0"))
        send.assert_not_called()

    def test_mismatch_alerts_with_l2_value(self) -> None:
        send = self._run(_Cache(NOW), _report("0"), Decimal("76371.81"))
        send.assert_called_once()
        self.assertIn("L2 Vault Mismatch", send.call_args.args[0].message)

    def test_stale_alert_is_sent_once(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.VALUE_KEY, "76368.80", HOUR)
        cache.set(self.STALE_KEY, 1, HOUR)
        send = self._run(cache, _report("76368.80", hours_ago=60))
        send.assert_not_called()

    def test_stale_alert_rearms_after_a_gap(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.VALUE_KEY, "76368.80", 5 * HOUR)
        cache.set(self.STALE_KEY, 1, 5 * HOUR)
        send = self._run(cache, _report("76368.80", hours_ago=60))
        send.assert_called_once()
        self.assertIn("Report Stale", send.call_args.args[0].message)


class TestFetchReports(unittest.TestCase):
    def test_reads_every_hub_chain(self) -> None:
        client = MagicMock()
        functions = client.get_contract.return_value.functions
        functions.getVaultChainIds.return_value.call.return_value = [8453]
        functions.getVault.return_value.call.return_value = VAULT.lower()
        functions.portalAssetsReport.return_value.call.return_value = (76_368_801_102 * 10**12, 0, 0, NOW)

        (report,) = fetch_reports(client)

        self.assertEqual(report, VaultReport(8453, VAULT, Decimal("76368.801102"), NOW))


if __name__ == "__main__":
    unittest.main()
