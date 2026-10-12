import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from protocols.infinifi import l2_vaults
from protocols.infinifi.l2_vaults import (
    VaultReport,
    check_report,
    drop_message,
    fetch_reports,
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
        timestamp = self.values.get(f"{key}_timestamp", 0)
        assert isinstance(timestamp, int)
        written = timestamp
        return 0 if written == 0 or self.now - written > stale_after else self.values.get(key, 0)

    def write(self, _file: str, key: str, value: object) -> None:
        self.set(key, value, 0)


class TestCheckReport(unittest.TestCase):
    VALUE_KEY = f"infinifi_l2_vault_value_{VAULT.lower()}"
    STALE_KEY = f"infinifi_l2_vault_stale_{VAULT.lower()}"

    def _run(self, cache: _Cache, report: VaultReport) -> MagicMock:
        with (
            patch.object(l2_vaults, "cache_timestamp_key", side_effect=lambda key: f"{key}_timestamp"),
            patch.object(l2_vaults, "get_last_value_for_key_from_file", side_effect=cache.last),
            patch.object(l2_vaults, "get_fresh_last_value_for_key_from_file", side_effect=cache.fresh),
            patch.object(l2_vaults, "write_last_value_with_timestamp_to_file", side_effect=cache.write),
            patch.object(l2_vaults, "send_alert") as send,
        ):
            check_report(report, NOW)
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

    def test_drop_without_a_recent_assets_update_mentions_token_transfers(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.VALUE_KEY, "100000", HOUR)

        send = self._run(cache, _report("40000", hours_ago=2))

        send.assert_called_once()
        self.assertIn("token transfers", send.call_args.args[0].message)
        self.assertIn("before treating the fall as a loss", send.call_args.args[0].message)

    def test_first_run_does_not_alert(self) -> None:
        send = self._run(_Cache(NOW), _report("0"))
        send.assert_not_called()

    def test_outbound_transfer_increase_does_not_alert(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.VALUE_KEY, "235033.709146", HOUR)

        send = self._run(cache, _report("1035033.709146", hours_ago=44, chain_id=143))

        send.assert_not_called()
        self.assertEqual(cache.values[self.VALUE_KEY], "1035033.709146")

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


class TestMain(unittest.TestCase):
    @patch.object(l2_vaults.ChainManager, "get_client")
    @patch("utils.http_client.request_with_retry")
    def test_outbound_transfer_uses_mainnet_without_an_api_comparison(
        self, http_request: MagicMock, get_client: MagicMock
    ) -> None:
        functions = get_client.return_value.get_contract.return_value.functions
        functions.getVaultChainIds.return_value.call.return_value = [143]
        functions.getVault.return_value.call.return_value = VAULT
        functions.portalAssetsReport.return_value.call.return_value = (
            1_035_033_709_146 * 10**12,
            0,
            0,
            NOW - 44 * HOUR,
        )
        cache = _Cache(NOW)
        cache.set(f"infinifi_l2_vault_value_{VAULT.lower()}", "235033.709146", HOUR)

        with (
            patch.object(l2_vaults, "datetime") as clock,
            patch.object(l2_vaults, "cache_timestamp_key", side_effect=lambda key: f"{key}_timestamp"),
            patch.object(l2_vaults, "get_last_value_for_key_from_file", side_effect=cache.last),
            patch.object(l2_vaults, "get_fresh_last_value_for_key_from_file", side_effect=cache.fresh),
            patch.object(l2_vaults, "write_last_value_with_timestamp_to_file", side_effect=cache.write),
            patch.object(l2_vaults, "send_alert") as send,
        ):
            clock.now.return_value = datetime.fromtimestamp(NOW, UTC)
            l2_vaults.main()

        http_request.assert_not_called()
        send.assert_not_called()


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
