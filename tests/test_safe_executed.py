"""The executed-nonce check: every executed Safe tx must have been covered by a queued-tx alert."""

import unittest
from unittest.mock import MagicMock, Mock, patch

from tests.test_safe_main import (  # noqa: F401 - the autouse fixture sets test keys and a Telegram destination
    SAFE,
    _isolate_safe_api_keys,
    import_safe_main,
    queue_response,
    queue_tx,
    safe_tx_hash,
)

SAFES = [["YEARN_MS", "mainnet", SAFE]]
KEY = ("mainnet", SAFE)


class TestNonceCheckResolution(unittest.TestCase):
    """``plan_nonce_check`` + ``NonceCheck.resolve`` without I/O."""

    def setUp(self) -> None:
        from protocols.safe import executed

        self.executed = executed

    def _resolve(self, state, current: int, handled: set, executed_hashes: set[str]):
        check = self.executed.plan_nonce_check(KEY, "YEARN_MS", state, current, handled)
        return check.resolve(executed_hashes)

    def test_nonce_with_no_alerted_tx_alerts(self):
        state = self.executed.NonceCheckState(10)
        self.assertEqual(self._resolve(state, 12, {(10, safe_tx_hash(10))}, {safe_tx_hash(10)}), ([11], frozenset()))

    def test_alerted_tx_that_executed_settles(self):
        state = self.executed.NonceCheckState(10)
        self.assertEqual(self._resolve(state, 11, {(10, safe_tx_hash(10))}, {safe_tx_hash(10)}), ([], frozenset()))

    def test_any_alerted_tx_at_the_nonce_may_be_the_executed_one(self):
        handled = {(10, safe_tx_hash(10)), (10, safe_tx_hash(10, variant=1))}
        state = self.executed.NonceCheckState(10)
        self.assertEqual(self._resolve(state, 11, handled, {safe_tx_hash(10, variant=1)}), ([], frozenset()))

    def test_unconfirmed_alerted_nonce_waits_one_run_then_alerts(self):
        """Another tx took the nonce (or the service lags): wait one run before alerting."""
        handled = {(10, safe_tx_hash(10))}
        first = self._resolve(self.executed.NonceCheckState(10), 11, handled, set())
        self.assertEqual(first, ([], frozenset({10})))

        second = self._resolve(self.executed.NonceCheckState(11, first[1]), 11, handled, set())
        self.assertEqual(second, ([10], frozenset()))

    def test_awaited_nonce_settles_once_the_service_reports_it(self):
        state = self.executed.NonceCheckState(11, frozenset({10}))
        self.assertEqual(self._resolve(state, 11, {(10, safe_tx_hash(10))}, {safe_tx_hash(10)}), ([], frozenset()))

    def test_entry_without_a_hash_cannot_be_confirmed(self):
        state = self.executed.NonceCheckState(11, frozenset({10}))
        self.assertEqual(self._resolve(state, 11, {(10, "")}, set()), ([10], frozenset()))

    def test_state_round_trips_through_the_cache(self):
        for state in (self.executed.NonceCheckState(7), self.executed.NonceCheckState(7, frozenset({3, 5}))):
            with self.subTest(state=state):
                self.executed.save_nonce_check_state(KEY, state)
                self.assertEqual(self.executed.load_nonce_check_state(KEY), state)
        self.assertIsNone(self.executed.load_nonce_check_state(("base-main", SAFE)))


class TestReadOnchainNonces(unittest.TestCase):
    def test_one_batch_per_chain_and_failed_chains_are_left_out(self):
        from protocols.safe import executed

        mainnet = MagicMock()
        mainnet.execute_batch.return_value = [5, 9]
        safes = [("mainnet", SAFE), ("mainnet", "0x" + "11" * 20), ("base-main", "0x" + "22" * 20)]

        def client_for(chain):
            if chain == executed.Chain.MAINNET:
                return mainnet
            raise ValueError("No providers")

        with patch.object(executed.ChainManager, "get_client", side_effect=client_for):
            nonces = executed.read_onchain_nonces(safes)

        self.assertEqual(nonces, {safes[0]: 5, safes[1]: 9})
        mainnet.execute_batch.assert_called_once()
        self.assertEqual(mainnet.batch_requests.return_value.__enter__.return_value.add.call_count, 2)


class TestFetchExecutedHashes(unittest.TestCase):
    def test_lookup_batches_hashes_and_keeps_executed_ones(self):
        safe_main = import_safe_main()
        hashes = [safe_tx_hash(n) for n in range(150)]
        first = Mock(status_code=200)
        first.json.return_value = [queue_tx(0, txHash="0x" + "ab" * 32), queue_tx(1)]
        second = Mock(status_code=200)
        second.json.return_value = [queue_tx(149, txHash="0x" + "cd" * 32, failed=True)]

        with patch.object(safe_main.requests, "get", side_effect=[first, second]) as mock_get:
            executed = safe_main.fetch_executed_safe_tx_hashes(hashes)

        # A failed execution still used the nonce, so it counts as the alerted tx executing.
        self.assertEqual(executed, {safe_tx_hash(0), safe_tx_hash(149)})
        self.assertEqual(mock_get.call_args.args[0], "https://api.safe.global/queue/api/v1/multisig-transactions/batch")
        self.assertEqual([len(c.kwargs["params"]) for c in mock_get.call_args_list], [100, 50])
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"][0], ("safeTxHash", safe_tx_hash(0)))


class TestCheckExecutedNonces(unittest.TestCase):
    """End to end over the real cache, with RPC, the hash lookup and Telegram mocked."""

    def setUp(self) -> None:
        self.safe_main = import_safe_main()

    def _run(self, onchain: dict, executed: set[str] | None = None, sent: bool = True):
        safe_main = self.safe_main
        with (
            patch.object(safe_main, "read_onchain_nonces", return_value=onchain),
            patch.object(safe_main, "fetch_executed_safe_tx_hashes", return_value=executed or set()) as mock_lookup,
            patch.object(safe_main, "send_telegram_message", return_value=sent) as mock_send,
        ):
            safe_main.check_executed_nonces(SAFES)
        return mock_lookup, mock_send

    def _state(self):
        return self.safe_main.load_nonce_check_state(KEY)

    def test_first_read_only_records_the_nonce(self):
        mock_lookup, mock_send = self._run({KEY: 40})
        mock_lookup.assert_not_called()
        mock_send.assert_not_called()
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(40))

    def test_execution_never_queued_alerts_loudly(self):
        self._run({KEY: 40})
        mock_lookup, mock_send = self._run({KEY: 42})

        mock_lookup.assert_not_called()  # no alerted tx to confirm: no Safe API request
        message, protocol, disable_notification = mock_send.call_args.args
        self.assertIn("EXECUTED WITHOUT A QUEUED ALERT", message)
        self.assertIn("#️⃣ Nonce: 40–41", message)
        self.assertIn(f"https://app.safe.global/transactions/history?safe=eth:{SAFE}", message)
        self.assertEqual(protocol, "YEARN_MS")
        self.assertFalse(disable_notification)
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(42))

    def test_alerted_tx_that_executed_is_quiet(self):
        self._run({KEY: 40})
        self.safe_main.mark_tx_handled(SAFE, "mainnet", queue_tx(40))
        mock_lookup, mock_send = self._run({KEY: 41}, executed={safe_tx_hash(40)})

        mock_lookup.assert_called_once_with([safe_tx_hash(40)])
        mock_send.assert_not_called()
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(41))

    def test_different_tx_executed_at_an_alerted_nonce_alerts_after_one_run(self):
        """A signer executing their own tx in place of the alerted one must not hide behind it."""
        self._run({KEY: 40})
        self.safe_main.mark_tx_handled(SAFE, "mainnet", queue_tx(40))

        _, mock_send = self._run({KEY: 41})
        mock_send.assert_not_called()
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(41, frozenset({40})))
        # The handled entry survives pruning while the nonce is awaited.
        self.safe_main.get_pending_transactions(SAFE, "mainnet", [])
        self.assertEqual(self.safe_main.load_handled_txs(SAFE, "mainnet"), {(40, safe_tx_hash(40))})

        _, mock_send = self._run({KEY: 41})
        self.assertIn("#️⃣ Nonce: 40", mock_send.call_args.args[0])
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(41))
        self.safe_main.get_pending_transactions(SAFE, "mainnet", [])
        self.assertEqual(self.safe_main.load_handled_txs(SAFE, "mainnet"), set())

    def test_undelivered_alert_is_retried_next_run(self):
        self._run({KEY: 40})
        _, mock_send = self._run({KEY: 41}, sent=False)
        mock_send.assert_called_once()
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(40))

        _, mock_send = self._run({KEY: 41})
        self.assertIn("#️⃣ Nonce: 40", mock_send.call_args.args[0])
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(41))

    def test_failed_delivery_does_not_block_the_next_safe(self):
        from utils.telegram import TelegramError

        safe_main = self.safe_main
        other = ("mainnet", "0x" + "12" * 20)
        safes = [*SAFES, ["LIDO", *other]]
        for key in (KEY, other):
            safe_main.save_nonce_check_state(key, safe_main.NonceCheckState(40))

        with (
            patch.object(safe_main, "read_onchain_nonces", return_value={KEY: 41, other: 41}),
            patch.object(
                safe_main, "send_telegram_message", side_effect=[TelegramError("topic not found"), True]
            ) as send,
            patch.object(safe_main, "send_error_message") as send_error,
        ):
            safe_main.check_executed_nonces(safes)

        self.assertEqual([call.args[1] for call in send.call_args_list], ["YEARN_MS", "LIDO"])
        self.assertEqual(self._state(), safe_main.NonceCheckState(40))
        self.assertEqual(safe_main.load_nonce_check_state(other), safe_main.NonceCheckState(41))
        self.assertIn(SAFE, send_error.call_args.args[0])

    def test_consecutive_backlog_fits_in_one_message(self):
        self._run({KEY: 1000})
        _, send = self._run({KEY: 2000})

        send.assert_called_once()
        self.assertIn("#️⃣ Nonce: 1000–1999", send.call_args.args[0])
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(2000))

    def test_successful_chunk_stays_settled_after_later_delivery_is_skipped(self):
        safe_main = self.safe_main
        self._run({KEY: 40})
        confirmed = {safe_tx_hash(nonce) for nonce in (41, 43)}
        for nonce in (41, 43):
            safe_main.mark_tx_handled(SAFE, "mainnet", queue_tx(nonce))

        with (
            patch.object(safe_main, "_MAX_NONCE_LIST_LENGTH", 6),
            patch.object(safe_main, "read_onchain_nonces", return_value={KEY: 46}),
            patch.object(safe_main, "fetch_executed_safe_tx_hashes", return_value=confirmed),
            patch.object(safe_main, "send_telegram_message", side_effect=[True, False]) as send,
        ):
            safe_main.check_executed_nonces(SAFES)

        self.assertEqual(send.call_count, 2)
        self.assertIn("#️⃣ Nonce: 40, 42", send.call_args_list[0].args[0])
        self.assertIn("#️⃣ Nonce: 44–45", send.call_args_list[1].args[0])
        self.assertEqual(self._state(), safe_main.NonceCheckState(46, frozenset({44, 45})))

        lookup, retry = self._run({KEY: 46})
        lookup.assert_not_called()
        retry.assert_called_once()
        self.assertIn("#️⃣ Nonce: 44–45", retry.call_args.args[0])
        self.assertEqual(self._state(), safe_main.NonceCheckState(46))

    def test_missing_destination_defers_without_sending(self):
        self._run({KEY: 40})
        with patch.object(self.safe_main, "telegram_credentials_configured", return_value=False):
            _, mock_send = self._run({KEY: 41})
        mock_send.assert_not_called()
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(40))

    def test_failed_rpc_read_keeps_progress(self):
        self._run({KEY: 40})
        _, mock_send = self._run({})
        mock_send.assert_not_called()
        self.assertEqual(self._state(), self.safe_main.NonceCheckState(40))


class TestQueueAndExecutedChecksTogether(unittest.TestCase):
    """A full ``main()`` per poll: one queue request, the alert loop, then the nonce check."""

    def test_alerted_then_executed_tx_raises_no_second_alert(self):
        safe_main = import_safe_main()
        tx = queue_tx(40)

        def poll(queue: list[dict], onchain_nonce: int, executed: list[dict] | None = None):
            responses = [queue_response(queue)]
            if executed is not None:
                lookup = Mock(status_code=200)
                lookup.json.return_value = executed
                responses.append(lookup)
            with (
                patch.object(safe_main, "ALL_SAFE_ADDRESSES", SAFES),
                patch.object(safe_main, "check_llm_credits"),
                patch.object(safe_main, "read_onchain_nonces", return_value={KEY: onchain_nonce}),
                patch.object(safe_main.requests, "get", side_effect=responses) as mock_get,
                patch.object(safe_main, "_explain_safe_tx", return_value=None),
                patch.object(safe_main, "send_telegram_message", return_value=True) as mock_send,
            ):
                safe_main.main()
            return mock_get, mock_send

        _, mock_send = poll([tx], 40)  # rollout: queue alert, nonce recorded
        self.assertIn("QUEUED TX DETECTED", mock_send.call_args.args[0])

        mock_get, mock_send = poll([], 41, executed=[{**tx, "txHash": "0x" + "ab" * 32}])  # it executed
        mock_send.assert_not_called()
        self.assertEqual(mock_get.call_count, 2)  # the queue, plus one lookup for the executed nonce

        mock_get, mock_send = poll([], 41)  # nothing new: one request, no alert
        mock_send.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(safe_main.load_handled_txs(SAFE, "mainnet"), set())


if __name__ == "__main__":
    unittest.main()
