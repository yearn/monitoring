import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from hexbytes import HexBytes

from protocols.infinifi import l2_vaults
from protocols.infinifi.l2_vaults import (
    VaultReport,
    VaultUpdate,
    check_report,
    drop_message,
    fetch_reports,
    fetch_updates,
    stale_message,
)
from utils.chains import Chain

VAULT = "0x4197F2ADFCe9fDeB36B02e96737Ddf646C841e45"
NOW = 1_791_000_000
HOUR = 3600
BLOCK = 10000
TX_HASH = "0x" + "ab" * 32


def _report(value: str, hours_ago: float = 1, chain_id: int = 8453) -> VaultReport:
    return VaultReport(chain_id, VAULT, Decimal(value), NOW - int(hours_ago * HOUR))


def _update(previous: str, value: str) -> VaultUpdate:
    return VaultUpdate(NOW, Decimal(previous), Decimal(value), TX_HASH)


def _event(previous: str, value: str, block_number: int = BLOCK) -> dict:
    return {
        "args": {
            "timestamp": NOW,
            "oldAssets": int(Decimal(previous) * 10**18),
            "newAssets": int(Decimal(value) * 10**18),
        },
        "transactionHash": HexBytes(TX_HASH),
        "blockNumber": block_number,
        "logIndex": 0,
    }


class TestDropMessage(unittest.TestCase):
    def test_write_down_to_zero_alerts(self) -> None:
        text = drop_message(_report("0"), _update("33355.45", "0"))
        assert text is not None
        self.assertIn("base (8453)", text)
        self.assertIn("$33,355.45 → $0.00 (−100.0%)", text)
        self.assertIn(f"[{VAULT}](https://etherscan.io/address/{VAULT})", text)
        self.assertIn(f"[{TX_HASH}](https://etherscan.io/tx/{TX_HASH})", text)
        self.assertIn("burned the vault's farm shares", text)

    def test_normal_update_is_quiet(self) -> None:
        self.assertIsNone(drop_message(_report("65954.23"), _update("69537.16", "65954.23")))

    def test_small_absolute_loss_is_quiet(self) -> None:
        self.assertIsNone(drop_message(_report("0", chain_id=143), _update("14", "0")))

    def test_initial_update_has_no_loss(self) -> None:
        self.assertIsNone(drop_message(_report("50000"), _update("0", "50000")))

    def test_unknown_chain_is_named_by_id(self) -> None:
        text = drop_message(_report("0", chain_id=143), _update("50000", "0"))
        assert text is not None
        self.assertIn("chain 143", text)

    def test_transfer_decrease_with_unchanged_farm_shares_is_quiet(self) -> None:
        self.assertIsNone(drop_message(_report("235033.709146"), _update("1035033.709146", "1035033.709146")))

    def test_loss_threshold_boundaries(self) -> None:
        for previous, value, alerts in [
            ("20000", "10000", False),
            ("20000", "9999", True),
            ("10000", "1", False),
            ("10000", "0", True),
        ]:
            with self.subTest(previous=previous, value=value):
                self.assertEqual(drop_message(_report(value), _update(previous, value)) is not None, alerts)


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
        return 0 if timestamp == 0 or self.now - timestamp > stale_after else self.values.get(key, 0)

    def write(self, _file: str, key: str, value: object) -> None:
        self.set(key, value, 0)


class TestCheckReport(unittest.TestCase):
    CURSOR_KEY = f"infinifi_l2_vault_update_block_{VAULT.lower()}"
    STALE_KEY = f"infinifi_l2_vault_stale_{VAULT.lower()}"

    def _run(
        self, cache: _Cache, report: VaultReport, logs: list[dict] | Exception | None = None, block: int = BLOCK
    ) -> tuple[MagicMock, MagicMock]:
        client = MagicMock()
        event = client.get_contract.return_value.events.PortalUpdate.return_value
        if isinstance(logs, Exception):
            event.get_logs.side_effect = logs
        else:
            event.get_logs.return_value = logs or []
        with (
            patch.object(l2_vaults, "get_last_value_for_key_from_file", side_effect=cache.last),
            patch.object(l2_vaults, "get_fresh_last_value_for_key_from_file", side_effect=cache.fresh),
            patch.object(l2_vaults, "write_last_value_with_timestamp_to_file", side_effect=cache.write),
            patch.object(l2_vaults, "send_alert") as send,
        ):
            check_report(report, NOW, client, block)
        return send, client

    def test_loss_alerts_and_advances_cursor(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        send, _ = self._run(cache, _report("0"), [_event("33355.45", "0")])
        send.assert_called_once()
        self.assertIn("Value Drop", send.call_args.args[0].message)
        self.assertEqual(cache.values[self.CURSOR_KEY], BLOCK)
        self.assertEqual(cache.values[self.STALE_KEY], 0)

    def test_loss_during_monitoring_gap_still_alerts(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 1500, 5 * HOUR)
        send, _ = self._run(cache, _report("0"), [_event("76368.80", "0")])
        send.assert_called_once()
        self.assertIn(TX_HASH, send.call_args.args[0].message)

    def test_return_transfer_without_an_assets_update_is_quiet(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        send, _ = self._run(cache, _report("235033.709146", hours_ago=44, chain_id=143))
        send.assert_not_called()

    def test_return_transfer_and_fresh_assets_update_are_quiet(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        send, _ = self._run(
            cache,
            _report("235033.709146", hours_ago=0, chain_id=143),
            [_event("1035033.709146", "1035033.709146")],
        )
        send.assert_not_called()

    def test_refunded_transfer_round_trip_is_quiet(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        for block, value in [(BLOCK, "1035033.709146"), (BLOCK + 300, "235033.709146")]:
            with self.subTest(block=block):
                send, _ = self._run(cache, _report(value, hours_ago=44, chain_id=143), block=block)
                send.assert_not_called()
        self.assertEqual(cache.values[self.CURSOR_KEY], BLOCK + 300)

    def test_loss_then_recovery_between_runs_still_alerts(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        send, _ = self._run(
            cache,
            _report("1035033.709146", hours_ago=0),
            [_event("1035033.709146", "235033.709146", BLOCK - 1), _event("235033.709146", "1035033.709146")],
        )
        send.assert_called_once()
        self.assertIn("−77.3%", send.call_args.args[0].message)

    def test_first_run_initializes_cursor_without_replaying_history(self) -> None:
        cache = _Cache(NOW)
        send, client = self._run(cache, _report("0"), [_event("50000", "0")])
        send.assert_not_called()
        client.get_contract.assert_not_called()
        self.assertEqual(cache.values[self.CURSOR_KEY], BLOCK)

    def test_same_block_does_not_replay_a_loss(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK, HOUR)
        send, client = self._run(cache, _report("0"), [_event("50000", "0")])
        send.assert_not_called()
        client.get_contract.return_value.events.PortalUpdate.return_value.get_logs.assert_not_called()

    def test_rpc_failure_does_not_advance_cursor(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        with self.assertRaisesRegex(RuntimeError, "RPC unavailable"):
            self._run(cache, _report("0"), RuntimeError("RPC unavailable"))
        self.assertEqual(cache.values[self.CURSOR_KEY], BLOCK - 300)

    def test_rpc_behind_cursor_fails_without_rolling_it_back(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK + 1, HOUR)
        with self.assertRaisesRegex(ValueError, "RPC is behind"):
            self._run(cache, _report("0"))
        self.assertEqual(cache.values[self.CURSOR_KEY], BLOCK + 1)

    def test_stale_alert_is_sent_once(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 300, HOUR)
        cache.set(self.STALE_KEY, 1, HOUR)
        send, _ = self._run(cache, _report("76368.80", hours_ago=60))
        send.assert_not_called()

    def test_stale_alert_rearms_after_a_gap(self) -> None:
        cache = _Cache(NOW)
        cache.set(self.CURSOR_KEY, BLOCK - 1500, 5 * HOUR)
        cache.set(self.STALE_KEY, 1, 5 * HOUR)
        send, _ = self._run(cache, _report("76368.80", hours_ago=60))
        send.assert_called_once()
        self.assertIn("Report Stale", send.call_args.args[0].message)


class TestMain(unittest.TestCase):
    @patch.object(l2_vaults.ChainManager, "get_client")
    @patch("requests.sessions.Session.request")
    def test_reads_only_mainnet_at_a_finalized_block(self, http_request: MagicMock, get_client: MagicMock) -> None:
        client = get_client.return_value
        client.eth.get_block.return_value = {"number": BLOCK}
        functions = client.get_contract.return_value.functions
        functions.getVaultChainIds.return_value.call.return_value = [143]
        functions.getVault.return_value.call.return_value = VAULT
        functions.portalAssetsReport.return_value.call.return_value = (
            1_035_033_709_146 * 10**12,
            0,
            0,
            NOW - 44 * HOUR,
        )
        client.get_contract.return_value.events.PortalUpdate.return_value.get_logs.return_value = []
        cache = _Cache(NOW)
        cache.set(f"infinifi_l2_vault_update_block_{VAULT.lower()}", BLOCK - 300, HOUR)

        with (
            patch.object(l2_vaults, "datetime") as clock,
            patch.object(l2_vaults, "get_last_value_for_key_from_file", side_effect=cache.last),
            patch.object(l2_vaults, "get_fresh_last_value_for_key_from_file", side_effect=cache.fresh),
            patch.object(l2_vaults, "write_last_value_with_timestamp_to_file", side_effect=cache.write),
            patch.object(l2_vaults, "send_alert") as send,
        ):
            clock.now.return_value = datetime.fromtimestamp(NOW, UTC)
            l2_vaults.main()

        get_client.assert_called_once_with(Chain.MAINNET)
        client.eth.get_block.assert_called_once_with("finalized")
        functions.portalAssetsReport.return_value.call.assert_called_once_with(block_identifier=BLOCK)
        http_request.assert_not_called()
        send.assert_not_called()


class TestFetchReports(unittest.TestCase):
    def test_reads_every_hub_chain_at_the_same_block(self) -> None:
        client = MagicMock()
        functions = client.get_contract.return_value.functions
        functions.getVaultChainIds.return_value.call.return_value = [8453]
        functions.getVault.return_value.call.return_value = VAULT.lower()
        functions.portalAssetsReport.return_value.call.return_value = (76_368_801_102 * 10**12, 0, 0, NOW)

        (report,) = fetch_reports(client, BLOCK)

        self.assertEqual(report, VaultReport(8453, VAULT, Decimal("76368.801102"), NOW))
        functions.getVaultChainIds.return_value.call.assert_called_once_with(block_identifier=BLOCK)
        functions.getVault.return_value.call.assert_called_once_with(block_identifier=BLOCK)
        functions.portalAssetsReport.return_value.call.assert_called_once_with(block_identifier=BLOCK)


class TestFetchUpdates(unittest.TestCase):
    def test_normalizes_and_orders_updates(self) -> None:
        client = MagicMock()
        event = client.get_contract.return_value.events.PortalUpdate.return_value
        event.get_logs.return_value = [_event("0", "50000", BLOCK), _event("50000", "0", BLOCK - 1)]

        updates = fetch_updates(client, VAULT, BLOCK - 300, BLOCK)

        self.assertEqual(updates, [_update("50000", "0"), _update("0", "50000")])

    def test_long_gaps_use_bounded_nonoverlapping_requests(self) -> None:
        client = MagicMock()
        event = client.get_contract.return_value.events.PortalUpdate.return_value
        event.get_logs.side_effect = [[], [_event("50000", "0", 2500)]]

        updates = fetch_updates(client, VAULT, 1, 2500)

        self.assertEqual(updates, [_update("50000", "0")])
        requests = [call.kwargs for call in event.get_logs.call_args_list]
        self.assertEqual(requests[0]["from_block"], 1)
        self.assertEqual(requests[-1]["to_block"], 2500)
        self.assertEqual(requests[0]["to_block"] + 1, requests[1]["from_block"])
        self.assertTrue(all(r["to_block"] - r["from_block"] < 2000 for r in requests))


if __name__ == "__main__":
    unittest.main()
