import unittest

from protocols.apyusd.main import _format_rate, get_rate_delta, should_alert_on_rate_delta


class TestApyusdRateMonitor(unittest.TestCase):
    def test_get_rate_delta_returns_none_without_previous_rate(self):
        self.assertIsNone(get_rate_delta(None, 2 * 10**18))

    def test_get_rate_delta_returns_zero_when_rate_does_not_change(self):
        self.assertEqual(get_rate_delta(10**18, 10**18), 0.0)

    def test_get_rate_delta_returns_relative_increase(self):
        self.assertEqual(get_rate_delta(10**18, 15 * 10**17), 0.5)

    def test_get_rate_delta_returns_relative_decrease(self):
        self.assertEqual(get_rate_delta(10**18, 5 * 10**17), -0.5)

    def test_should_alert_when_increase_meets_threshold(self):
        self.assertTrue(should_alert_on_rate_delta(10**18, 15 * 10**17, 0.5))

    def test_should_alert_when_decrease_meets_threshold(self):
        self.assertTrue(should_alert_on_rate_delta(10**18, 5 * 10**17, 0.5))

    def test_should_not_alert_when_absolute_delta_is_below_threshold(self):
        self.assertFalse(should_alert_on_rate_delta(10**18, 109 * 10**16, 0.1))
        self.assertFalse(should_alert_on_rate_delta(10**18, 91 * 10**16, 0.1))

    def test_format_rate_uses_1e18_precision(self):
        self.assertEqual(_format_rate(15 * 10**17), 1.5)
