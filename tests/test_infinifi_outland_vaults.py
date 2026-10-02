import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from protocols.infinifi import outland_vaults
from protocols.infinifi.outland_vaults import VaultReport, check_report, drop_message, fetch_reports, stale_message

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


class TestCheckReport(unittest.TestCase):
    @patch.object(outland_vaults, "write_last_value_with_timestamp_to_file")
    @patch.object(outland_vaults, "send_alert")
    @patch.object(outland_vaults, "get_fresh_last_value_for_key_from_file")
    def test_drop_alerts_and_updates_baseline(self, fresh: MagicMock, send: MagicMock, write: MagicMock) -> None:
        fresh.side_effect = lambda _file, key, _stale: "33355.45" if "_value_" in key else 0
        check_report(_report("0"), NOW)
        send.assert_called_once()
        self.assertIn("Value Drop", send.call_args.args[0].message)
        written = {call.args[1]: call.args[2] for call in write.call_args_list}
        self.assertEqual(written[f"infinifi_outland_value_{VAULT.lower()}"], "0")
        self.assertEqual(written[f"infinifi_outland_stale_{VAULT.lower()}"], 0)

    @patch.object(outland_vaults, "write_last_value_with_timestamp_to_file")
    @patch.object(outland_vaults, "send_alert")
    @patch.object(outland_vaults, "get_fresh_last_value_for_key_from_file")
    def test_stale_alert_is_sent_once(self, fresh: MagicMock, send: MagicMock, _write: MagicMock) -> None:
        fresh.side_effect = lambda _file, key, _stale: "76368.80" if "_value_" in key else 1
        check_report(_report("76368.80", hours_ago=60), NOW)
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
