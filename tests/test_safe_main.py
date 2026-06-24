import importlib
import os
import unittest
from unittest.mock import Mock, patch

import requests


class TestSafePendingTransactions(unittest.TestCase):
    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    def test_current_nonce_advances_nonce_cache(self):
        safe_main = self._import_safe_main()
        safe_address = "0xSafe"

        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=9),
            patch.object(safe_main, "get_safe_current_nonce", return_value=23),
            patch.object(
                safe_main,
                "get_safe_transactions",
                return_value=[
                    {"nonce": 22},
                    {"nonce": 20},
                    {"nonce": 23},
                ],
            ),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            pending = safe_main.get_pending_transactions(safe_address, "arbitrum-main")

        mock_write.assert_called_once_with(safe_address, 22)
        self.assertEqual(pending, [{"nonce": 23}])

    def test_get_safe_transactions_retries_on_connection_error(self):
        safe_main = self._import_safe_main()

        ok_response = Mock(status_code=200)
        ok_response.json.return_value = {"results": [{"nonce": 5}]}

        with (
            patch.object(
                safe_main.requests,
                "get",
                side_effect=[
                    requests.exceptions.ConnectionError("Connection reset by peer"),
                    ok_response,
                ],
            ) as mock_get,
            patch.object(safe_main.time, "sleep") as mock_sleep,
        ):
            result = safe_main.get_safe_transactions("0xSafe", "arbitrum-main")

        self.assertEqual(result, [{"nonce": 5}])
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once()

    def test_get_safe_transactions_returns_empty_after_exhausting_retries(self):
        safe_main = self._import_safe_main()

        with (
            patch.object(
                safe_main.requests,
                "get",
                side_effect=requests.exceptions.ConnectionError("Connection reset by peer"),
            ) as mock_get,
            patch.object(safe_main.time, "sleep"),
        ):
            result = safe_main.get_safe_transactions("0xSafe", "arbitrum-main", max_retries=3)

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 3)

    def test_executed_rows_are_filtered_even_when_api_returns_them(self):
        safe_main = self._import_safe_main()
        safe_address = "0xSafe"

        txs = [
            {"nonce": 30, "isExecuted": True},
            {"nonce": 31, "executionDate": "2026-06-14T21:00:00Z"},
            {"nonce": 32, "transactionHash": "0x" + "ab" * 32},
            {"nonce": 33, "isExecuted": False, "executionDate": None, "transactionHash": None},
        ]

        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=0),
            patch.object(safe_main, "get_safe_current_nonce", return_value=30),
            patch.object(safe_main, "get_safe_transactions", return_value=txs),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            pending = safe_main.get_pending_transactions(safe_address, "mainnet")

        mock_write.assert_called_once_with(safe_address, 29)
        self.assertEqual(pending, [{"nonce": 33, "isExecuted": False, "executionDate": None, "transactionHash": None}])

    def test_current_nonce_unknown_fails_closed(self):
        safe_main = self._import_safe_main()
        safe_address = "0xSafe"

        txs = [
            {"nonce": 3274, "isExecuted": False, "executionDate": None, "transactionHash": None},
            {"nonce": 3280, "isExecuted": False, "executionDate": None, "transactionHash": None},
        ]

        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=0),
            patch.object(safe_main, "get_safe_current_nonce", return_value=None),
            patch.object(safe_main, "get_safe_transactions", return_value=txs),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            pending = safe_main.get_pending_transactions(safe_address, "mainnet")

        self.assertEqual(pending, [])
        mock_write.assert_not_called()

    def test_dead_slot_rows_below_current_nonce_are_filtered(self):
        safe_main = self._import_safe_main()
        safe_address = "0xSafe"

        txs = [
            {"nonce": 3280, "isExecuted": False, "executionDate": None, "transactionHash": None},
            {"nonce": 3282, "isExecuted": False, "executionDate": None, "transactionHash": None},
        ]

        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=0),
            patch.object(safe_main, "get_safe_current_nonce", return_value=3282),
            patch.object(safe_main, "get_safe_transactions", return_value=txs),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            pending = safe_main.get_pending_transactions(safe_address, "mainnet")

        mock_write.assert_called_once_with(safe_address, 3281)
        self.assertEqual(
            pending, [{"nonce": 3282, "isExecuted": False, "executionDate": None, "transactionHash": None}]
        )


class TestCheckForPendingTransactions(unittest.TestCase):
    """Regression tests for the dedupe write and dead-slot / stale-snapshot behavior.

    The Safe tx-service returns pending txs in descending nonce order
    (ordering=-nonce). The original loop wrote the cache to each nonce in
    turn, so the cache ended at the *lowest* nonce and re-fired the highest
    one next run. We now write progress after each delivered alert without
    ever moving the scalar cache backwards.
    """

    _DIAG = {
        "last_cached_nonce": 0,
        "current_safe_nonce": None,
        "chain_baseline": None,
        "baseline": 0,
    }

    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    def _tx(self, nonce: int) -> dict:
        return {
            "nonce": str(nonce),
            "to": "0x0000000000000000000000000000000000000abc",
            "data": "0x",
            "submissionDate": "2026-06-14T00:00:00Z",
            "proposer": "0x0000000000000000000000000000000000000999",
            "value": "0",
        }

    def test_cache_write_uses_highest_nonce_not_lowest(self):
        """Two pending txs in DESCENDING order; cache must end at the highest.

        Regression: previously the loop wrote the cache after each tx, so
        the final value was the *lowest* nonce (2280) and the highest (2281)
        was re-alerted on the next run.
        """
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

        pending = [self._tx(2281), self._tx(2280)]  # API returns descending

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=pending),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_pending_filter_diag", return_value=self._DIAG),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

        self.assertEqual(mock_send.call_count, 2)
        mock_write.assert_called_once_with(safe_address, 2281)

    def test_cache_write_single_call_even_for_single_tx(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[self._tx(2281)]),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_pending_filter_diag", return_value=self._DIAG),
            patch.object(safe_main, "send_telegram_message"),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

        mock_write.assert_called_once_with(safe_address, 2281)

    def test_cache_progress_survives_later_send_failure(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[self._tx(2281), self._tx(2280)]),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_pending_filter_diag", return_value=self._DIAG),
            patch.object(safe_main, "send_telegram_message", side_effect=[None, RuntimeError("telegram down")]),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            with self.assertRaises(RuntimeError):
                safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

        mock_write.assert_called_once_with(safe_address, 2281)

    def test_no_cache_write_when_no_pending(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[]),
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

        mock_write.assert_not_called()

    def test_euler_vault_filter_skips_non_matched_target(self):
        """EULER protocol: only alert on txs to the monitored vault address."""
        safe_main = self._import_safe_main()
        safe_address = "0xcAD001c30E96765aC90307669d578219D4fb1DCe"
        euler_vault = "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9"

        pending = [
            {**self._tx(10), "to": "0x0000000000000000000000000000000000000bad"},  # wrong target
            {**self._tx(11), "to": euler_vault},  # right target
        ]

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=pending),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_pending_filter_diag", return_value=self._DIAG),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "write_last_executed_nonce_to_file") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "EULER")

        # Only the matching-target tx should produce a Telegram message.
        self.assertEqual(mock_send.call_count, 1)
        # Cache still advances to the highest processed nonce.
        mock_write.assert_called_once_with(safe_address, 11)


class TestPendingFilterDiag(unittest.TestCase):
    """The diagnostic helper must mirror the same baseline math as get_pending_transactions."""

    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    def test_diag_baseline_when_current_nonce_known(self):
        safe_main = self._import_safe_main()
        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=10),
            patch.object(safe_main, "get_safe_current_nonce", return_value=42),
        ):
            diag = safe_main._pending_filter_diag("0xSafe", "mainnet")

        self.assertEqual(
            diag,
            {
                "last_cached_nonce": 10,
                "current_safe_nonce": 42,
                "chain_baseline": 41,
                "baseline": 41,  # max(10, 41)
            },
        )

    def test_diag_baseline_when_current_nonce_unknown(self):
        safe_main = self._import_safe_main()
        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=10),
            patch.object(safe_main, "get_safe_current_nonce", return_value=None),
        ):
            diag = safe_main._pending_filter_diag("0xSafe", "mainnet")

        self.assertEqual(
            diag,
            {
                "last_cached_nonce": 10,
                "current_safe_nonce": None,
                "chain_baseline": None,
                "baseline": 10,  # degraded to last_cached
            },
        )

    def test_diag_baseline_uses_last_cached_when_ahead_of_chain(self):
        """If the cache is ahead of chain_baseline (e.g. we previously alerted on a future tx), baseline is last_cached."""
        safe_main = self._import_safe_main()
        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=100),
            patch.object(safe_main, "get_safe_current_nonce", return_value=42),
        ):
            diag = safe_main._pending_filter_diag("0xSafe", "mainnet")

        self.assertEqual(diag["chain_baseline"], 41)
        self.assertEqual(diag["baseline"], 100)  # max(100, 41)


if __name__ == "__main__":
    unittest.main()
