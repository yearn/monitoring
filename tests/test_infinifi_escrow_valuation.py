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
from utils.infinifi_escrow import fetch_whitelist_targets

FARM = "0x2fa5E6C5549BEdF98A935Cac3BB4337459c74897"
ESCROW = "0x7912Eaff92B2f5Bc64Cdd21C76d79FFC12eA855E"
OTHER = "0x80608f852D152024c0a2087b16939235fEc2400c"
VAULT = "0x55f3Ab43E49FFb6b1FFf5E2B310C21278bDAf0f5"
MGLO = "0x1DD91a111606382B77A917633ED90feAf25E0F76"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
FEED = "0x" + "11" * 20
ADJUSTED = "0x" + "22" * 20
AGGREGATOR = "0x" + "33" * 20
BLOCK = 100


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
        request_calls: dict[int, MagicMock] = {}

        def lookup(request_id: int) -> MagicMock:
            if request_id not in request_calls:
                call = MagicMock()
                call.call.return_value = requests[request_id]
                request_calls[request_id] = call
            return request_calls[request_id]

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

        (position,) = _pending_requests(client, vault, ESCROW, "mGLO", BLOCK)

        self.assertEqual(position.value, Decimal("7494285.000"))
        self.assertIn("Pending Midas redemption #1", position.description)
        topics = client.eth.get_logs.call_args.args[0]["topics"]
        self.assertEqual(topics[2], "0x" + "0" * 24 + ESCROW[2:].lower())
        self.assertEqual(client.eth.get_logs.call_args.args[0]["toBlock"], BLOCK)
        for request_id in requests:
            functions = client.get_contract.return_value.functions
            functions.redeemRequests(request_id).call.assert_called_once_with(block_identifier=BLOCK)

    def test_pending_deposit_is_valued_at_its_usd_amount(self) -> None:
        requests = {4: (ESCROW, USDC, 0, 1_000 * 10**18, 990 * 10**18, 0)}
        client = self._client([self._log(4)], requests, redeem=False)

        (position,) = _pending_requests(client, _MidasVault(VAULT, MGLO, Decimal(1)), ESCROW, "mGLO", BLOCK)

        self.assertEqual(position.value, Decimal(990))
        self.assertIn("Pending Midas deposit #4", position.description)


class TestWhitelistHistory(unittest.TestCase):
    def test_disabled_targets_are_retained_only_when_requested(self) -> None:
        client = MagicMock()
        logs = client.get_contract.return_value.events.WhitelistUpdated.return_value.get_logs
        logs.return_value = [
            {"args": {"target": VAULT, "enabled": True}},
            {"args": {"target": MGLO, "enabled": True}},
            {"args": {"target": VAULT.lower(), "enabled": False}},
        ]

        self.assertEqual(fetch_whitelist_targets(client, ESCROW), [MGLO])
        logs.assert_called_with(from_block=0, to_block="latest")
        self.assertEqual(
            fetch_whitelist_targets(client, ESCROW, block_identifier=BLOCK, include_disabled=True), [VAULT, MGLO]
        )
        logs.assert_called_with(from_block=0, to_block=BLOCK)


class TestValuationSnapshot(unittest.TestCase):
    def _client(self, *, disabled: bool = False, pending_amount: int = 6_000_000) -> tuple[MagicMock, list[int]]:
        """The router has $4M of mGLO and a claim that settles into USDC during the scan."""
        client = MagicMock()
        client.eth.block_number = BLOCK
        latest = [BLOCK]
        read_blocks: list[int] = []
        contracts: dict[str, MagicMock] = {}

        def add_call(address: str, name: str, result: object) -> None:
            contract = contracts.setdefault(address, MagicMock())
            call = getattr(contract.functions, name).return_value.call

            def read(*_args: object, block_identifier: int | None = None) -> object:
                block = latest[0] if block_identifier is None else block_identifier
                read_blocks.append(block)
                return result(block) if callable(result) else result

            call.side_effect = read

        add_call(escrow_valuation.FARM_REGISTRY, "getFarms", [FARM])
        add_call(escrow_valuation.ACCOUNTING, "totalAssetsValue", 100_000_000 * 10**18)
        add_call(FARM, "escrow", ESCROW)
        add_call(ESCROW, "whitelist", False)
        add_call(ESCROW, "assetToken", USDC)
        add_call(ESCROW, "totalAssets", 10_000_000 * 10**6)
        add_call(USDC, "decimals", 6)
        add_call(USDC, "symbol", "USDC")
        add_call(USDC, "balanceOf", lambda block: pending_amount * 10**6 if block > BLOCK else 0)
        add_call(MGLO, "decimals", 18)
        add_call(MGLO, "symbol", "mGLO")
        add_call(MGLO, "balanceOf", 4_000_000 * 10**18)
        add_call(VAULT, "mToken", MGLO)
        add_call(VAULT, "mTokenDataFeed", FEED)
        add_call(
            VAULT,
            "redeemRequests",
            lambda block: (ESCROW, USDC, 1 if block > BLOCK else 0, pending_amount * 10**18, 10**18, 10**18),
        )
        add_call(FEED, "aggregator", ADJUSTED)
        add_call(ADJUSTED, "underlyingFeed", AGGREGATOR)
        add_call(AGGREGATOR, "latestRoundData", (1, 10**8, 0, 0, 1))
        add_call(AGGREGATOR, "decimals", 8)
        # Probing token call targets as vaults should fail normally.
        for token in (USDC, MGLO):
            contracts[token].functions.mToken.return_value.call.side_effect = ContractLogicError("not a vault")

        events = [{"args": {"target": VAULT, "enabled": True}}, {"args": {"target": MGLO, "enabled": True}}]
        if disabled:
            events.extend([{"args": {"target": VAULT, "enabled": False}}, {"args": {"target": MGLO, "enabled": False}}])
        contracts[ESCROW].events.WhitelistUpdated.return_value.get_logs.return_value = events
        client.get_contract.side_effect = lambda address, _abi: contracts[address]

        def settle(_filter: dict) -> list[dict]:
            latest[0] = BLOCK + 1
            return [{"topics": [b"\x00" * 32, (1).to_bytes(32, "big"), b"\x00" * 32]}]

        client.eth.get_logs.side_effect = settle
        return client, read_blocks

    def test_settlement_during_run_does_not_create_a_gap(self) -> None:
        client, read_blocks = self._client()
        with (
            patch.object(escrow_valuation.ChainManager, "get_client", return_value=client),
            patch.object(escrow_valuation, "check_valuation", wraps=check_valuation) as check,
            patch.object(escrow_valuation, "_alert_once") as alert,
            patch.object(escrow_valuation, "_clear"),
        ):
            escrow_valuation.main()

        valuation = check.call_args.args[0]
        self.assertEqual(valuation.value, Decimal(10_000_000))
        self.assertEqual(valuation.gap, Decimal(0))
        self.assertEqual(valuation.protocol_assets, Decimal(100_000_000))
        alert.assert_not_called()
        self.assertEqual(set(read_blocks), {BLOCK})
        client.eth.get_logs.assert_called_once()
        self.assertEqual(client.eth.get_logs.call_args.args[0]["toBlock"], BLOCK)
        client.get_contract(ESCROW, []).events.WhitelistUpdated.return_value.get_logs.assert_called_once_with(
            from_block=0, to_block=BLOCK
        )

    def test_disabled_vault_and_token_still_contribute_to_value(self) -> None:
        client, read_blocks = self._client(disabled=True)

        valuation = escrow_valuation.value_router(client, FARM, ESCROW)

        self.assertEqual(valuation.value, Decimal(10_000_000))
        self.assertEqual(valuation.gap, Decimal(0))
        self.assertEqual(len(valuation.positions), 2)
        self.assertFalse(valuation.unpriced)
        self.assertEqual(set(read_blocks), {BLOCK})

    def test_real_gap_at_disabled_vault_still_alerts(self) -> None:
        client, _ = self._client(disabled=True, pending_amount=5_000_000)
        valuation = escrow_valuation.value_router(client, FARM, ESCROW, Decimal(100_000_000))
        with patch.object(escrow_valuation, "_alert_once") as alert, patch.object(escrow_valuation, "_clear"):
            check_valuation(valuation)

        self.assertEqual(valuation.gap, Decimal(1_000_000))
        self.assertEqual(alert.call_args.args[2], AlertSeverity.MEDIUM)


if __name__ == "__main__":
    unittest.main()
