"""Tests for utils/llm/credits.py."""

import os
import unittest
from unittest.mock import MagicMock, patch

from utils.llm import credits

DAY = credits.ALERT_COOLDOWN_SECONDS


def _response(usd: float, diem: float = 0, access_permitted: bool = True) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "data": {
            "accessPermitted": access_permitted,
            "balances": {"USD": usd, "DIEM": diem, "BUNDLED_CREDITS": 0},
        }
    }
    return response


class TestCheckLLMCredits(unittest.TestCase):
    """Tests for check_llm_credits."""

    def setUp(self) -> None:
        self.cache: dict[str, object] = {}
        env = patch.dict(
            os.environ,
            {"LLM_PROVIDER": "venice", "LLM_API_KEY": "k", "LLM_BASE_URL": "", "LLM_CREDIT_ALERT_THRESHOLD_USD": ""},
        )
        env.start()
        self.addCleanup(env.stop)
        for name, fake in (
            ("get_last_value_for_key_from_file", lambda _f, k: self.cache.get(k, 0)),
            ("write_last_value_to_file", lambda _f, k, v: self.cache.__setitem__(k, v)),
        ):
            p = patch.object(credits, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)
        send = patch.object(credits, "send_error_message")
        self.mock_send = send.start()
        self.addCleanup(send.stop)

    def _check(self, response: MagicMock, now: float = 1_000_000) -> MagicMock:
        with patch.object(credits, "request_with_retry", return_value=response) as mock_request:
            credits.check_llm_credits("yearn", alert_protocol="yearn-internal", now=now)
        return mock_request

    def test_healthy_balance_does_not_alert(self) -> None:
        mock_request = self._check(_response(5.0))
        self.mock_send.assert_not_called()
        self.assertEqual(mock_request.call_args.args[1], "https://api.venice.ai/api/v1/api_keys/rate_limits")
        self.assertEqual(mock_request.call_args.kwargs["headers"], {"Authorization": "Bearer k"})

    def test_low_balance_alerts_internal_channel(self) -> None:
        self._check(_response(0.42))
        self.mock_send.assert_called_once()
        message, label = self.mock_send.call_args.args
        self.assertIn("$0.42", message)
        self.assertEqual(label, "yearn")
        self.assertEqual(self.mock_send.call_args.kwargs["alert_protocol"], "yearn-internal")
        self.assertFalse(self.mock_send.call_args.kwargs["disable_notification"])

    def test_diem_counts_towards_spendable_balance(self) -> None:
        self._check(_response(0.1, diem=10))
        self.mock_send.assert_not_called()

    def test_access_blocked_alerts_even_with_balance(self) -> None:
        self._check(_response(5.0, access_permitted=False))
        self.mock_send.assert_called_once()
        self.assertIn("access blocked", self.mock_send.call_args.args[0])

    def test_threshold_env_override(self) -> None:
        with patch.dict(os.environ, {"LLM_CREDIT_ALERT_THRESHOLD_USD": "10"}):
            self._check(_response(5.0))
        self.mock_send.assert_called_once()

    def test_alerts_once_per_cooldown(self) -> None:
        self._check(_response(0.5), now=1_000_000)
        self._check(_response(0.4), now=1_000_000 + 3600)
        self.assertEqual(self.mock_send.call_count, 1)
        self._check(_response(0.3), now=1_000_000 + DAY)
        self.assertEqual(self.mock_send.call_count, 2)

    def test_top_up_resets_cooldown(self) -> None:
        self._check(_response(0.5), now=1_000_000)
        self._check(_response(20.0), now=1_000_000 + 60)
        self._check(_response(0.5), now=1_000_000 + 120)
        self.assertEqual(self.mock_send.call_count, 2)

    def test_fetch_failure_is_swallowed(self) -> None:
        with patch.object(credits, "request_with_retry", side_effect=RuntimeError("boom")):
            credits.check_llm_credits("yearn")
        self.mock_send.assert_not_called()

    def test_skipped_for_other_providers_and_missing_key(self) -> None:
        for env in ({"LLM_PROVIDER": "anthropic"}, {"LLM_API_KEY": ""}):
            with patch.dict(os.environ, env), patch.object(credits, "request_with_retry") as mock_request:
                credits.check_llm_credits("yearn")
            mock_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
