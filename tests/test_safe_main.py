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


if __name__ == "__main__":
    unittest.main()
