import importlib
import os
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
