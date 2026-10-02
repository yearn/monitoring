import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from web3.exceptions import ContractLogicError

from protocols.infinifi import escrow_valuation
from protocols.infinifi.escrow_valuation import (
    EscrowValuation,
    Position,
    _MidasVault,
    _pending_requests,
    check_valuation,
    gap_message,
)
from utils.alert import AlertSeverity

FARM = "0x2fa5E6C5549BEdF98A935Cac3BB4337459c74897"
ESCROW = "0x7912Eaff92B2f5Bc64Cdd21C76d79FFC12eA855E"
OTHER = "0x80608f852D152024c0a2087b16939235fEc2400c"
VAULT = "0x55f3Ab43E49FFb6b1FFf5E2B310C21278bDAf0f5"
MGLO = "0x1DD91a111606382B77A917633ED90feAf25E0F76"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def _valuation(reported: str, *values: str | None, protocol_assets: str = "0") -> EscrowValuation:
    positions = tuple(Position(f"p{i}", None if v is None else Decimal(v)) for i, v in enumerate(values))
    return EscrowValuation(
        farm=FARM,
        escrow=ESCROW,
        reported=Decimal(reported),
        positions=positions,
        protocol_assets=Decimal(protocol_assets),
    )


class TestEscrowValuation(unittest.TestCase):
    def test_gap_is_reported_minus_priced_holdings(self) -> None:
        valuation = _valuation("21280488.88", "12113333.78", "7499999.97")
        self.assertEqual(valuation.value, Decimal("19613333.75"))
        self.assertEqual(valuation.gap, Decimal("1667155.13"))
        self.assertAlmostEqual(float(valuation.gap_ratio), 0.0783, places=4)

    def test_empty_router_has_no_gap(self) -> None:
        valuation = _valuation("0", "209.91")
        self.assertEqual(valuation.gap_ratio, Decimal(0))

    def test_unpriced_positions_are_excluded_from_value(self) -> None:
        valuation = _valuation("100", "40", None)
        self.assertEqual(valuation.value, Decimal(40))
        self.assertEqual(len(valuation.unpriced), 1)

    def test_gap_message_links_full_addresses(self) -> None:
        text = gap_message(_valuation("21280488.88", "19613333.75", protocol_assets="44156429.71"))
        self.assertIn(f"[{ESCROW}](https://etherscan.io/address/{ESCROW})", text)
        self.assertIn("Gap: $1,667,155.13 (7.83% of reported, threshold 3%)", text)
        self.assertIn("Share of infiniFi total assets ($44,156,429.71): 3.78% (HIGH above 5%)", text)


class TestCheckValuation(unittest.TestCase):
    @patch.object(escrow_valuation, "_clear")
    @patch.object(escrow_valuation, "_alert_once")
    def test_gap_small_against_protocol_alerts_medium(self, alert: MagicMock, clear: MagicMock) -> None:
        # Today's case: 7.8% of the router, 3.8% of infiniFi's assets.
        check_valuation(_valuation("21280488.88", "19613333.75", protocol_assets="44156429.71"))
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], f"infinifi_escrow_gap_{ESCROW.lower()}")
        self.assertEqual(alert.call_args.args[2], AlertSeverity.MEDIUM)
        clear.assert_called_once_with(f"infinifi_escrow_unpriced_{ESCROW.lower()}")

    @patch.object(escrow_valuation, "_clear")
    @patch.object(escrow_valuation, "_alert_once")
    def test_gap_large_against_protocol_alerts_high(self, alert: MagicMock, _clear: MagicMock) -> None:
        check_valuation(_valuation("100", "90", protocol_assets="150"))
        self.assertEqual(alert.call_args.args[2], AlertSeverity.HIGH)

    @patch.object(escrow_valuation, "_clear")
    @patch.object(escrow_valuation, "_alert_once")
    def test_gap_within_threshold_clears(self, alert: MagicMock, clear: MagicMock) -> None:
        check_valuation(_valuation("100", "98"))
        alert.assert_not_called()
        self.assertEqual(clear.call_count, 2)

    @patch.object(escrow_valuation, "_clear")
    @patch.object(escrow_valuation, "_alert_once")
    def test_reporting_less_than_held_does_not_alert(self, alert: MagicMock, _clear: MagicMock) -> None:
        check_valuation(_valuation("100", "150"))
        alert.assert_not_called()

    @patch.object(escrow_valuation, "_clear")
    @patch.object(escrow_valuation, "_alert_once")
    def test_unpriced_holding_alerts_medium_and_skips_gap(self, alert: MagicMock, clear: MagicMock) -> None:
        check_valuation(_valuation("100", "10", None))
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], f"infinifi_escrow_unpriced_{ESCROW.lower()}")
        self.assertEqual(alert.call_args.args[2], AlertSeverity.MEDIUM)
        clear.assert_not_called()


class TestAlertOnce(unittest.TestCase):
    def _run(self, stored: int, severity: AlertSeverity) -> tuple[MagicMock, MagicMock]:
        with (
            patch.object(escrow_valuation, "get_fresh_last_value_for_key_from_file", return_value=stored),
            patch.object(escrow_valuation, "write_last_value_with_timestamp_to_file") as write,
            patch.object(escrow_valuation, "send_alert") as send,
        ):
            escrow_valuation._alert_once("key", "msg", severity)
        return send, write

    def test_first_breach_alerts(self) -> None:
        send, write = self._run(0, AlertSeverity.MEDIUM)
        send.assert_called_once()
        self.assertEqual(write.call_args.args[2], 1)

    def test_repeat_breach_stays_quiet(self) -> None:
        send, _ = self._run(1, AlertSeverity.MEDIUM)
        send.assert_not_called()

    def test_escalation_to_high_alerts_again(self) -> None:
        send, write = self._run(1, AlertSeverity.HIGH)
        self.assertEqual(send.call_args.args[0].severity, AlertSeverity.HIGH)
        self.assertEqual(write.call_args.args[2], 2)

    def test_easing_from_high_to_medium_stays_quiet(self) -> None:
        send, write = self._run(2, AlertSeverity.MEDIUM)
        send.assert_not_called()
        self.assertEqual(write.call_args.args[2], 1)


class TestPendingRequests(unittest.TestCase):
    def _client(self, logs: list[dict], requests: dict[int, tuple], redeem: bool = True) -> MagicMock:
        client = MagicMock()
        client.eth.get_logs.return_value = logs
        functions = client.get_contract.return_value.functions

        def lookup(request_id: int) -> MagicMock:
            call = MagicMock()
            call.call.return_value = requests[request_id]
            return call

        def missing(_request_id: int) -> MagicMock:
            call = MagicMock()
            call.call.side_effect = ContractLogicError("no function")
            return call

        functions.redeemRequests.side_effect = lookup if redeem else missing
        functions.mintRequests.side_effect = missing if redeem else lookup
        return client

    @staticmethod
    def _log(request_id: int) -> dict:
        return {"topics": [b"\x00" * 32, request_id.to_bytes(32, "big"), b"\x00" * 32]}

    def test_keeps_only_the_routers_pending_redemptions(self) -> None:
        requests = {
            1: (ESCROW, USDC, 0, 7_457_000 * 10**18, 0, 0),
            2: (ESCROW, USDC, 2, 5 * 10**18, 0, 0),  # canceled
            3: (OTHER, USDC, 0, 9 * 10**18, 0, 0),  # someone else's
        }
        client = self._client([self._log(1), self._log(1), self._log(2), self._log(3)], requests)
        vault = _MidasVault(VAULT, MGLO, Decimal("1.005"))

        (position,) = _pending_requests(client, vault, ESCROW, "mGLO")

        self.assertEqual(position.value, Decimal("7494285.000"))
        self.assertIn("Pending Midas redemption #1", position.description)
        topics = client.eth.get_logs.call_args.args[0]["topics"]
        self.assertEqual(topics[2], "0x" + "0" * 24 + ESCROW[2:].lower())

    def test_pending_deposit_is_valued_at_its_usd_amount(self) -> None:
        requests = {4: (ESCROW, USDC, 0, 1_000 * 10**18, 990 * 10**18, 0)}
        client = self._client([self._log(4)], requests, redeem=False)

        (position,) = _pending_requests(client, _MidasVault(VAULT, MGLO, Decimal(1)), ESCROW, "mGLO")

        self.assertEqual(position.value, Decimal(990))
        self.assertIn("Pending Midas deposit #4", position.description)


if __name__ == "__main__":
    unittest.main()
