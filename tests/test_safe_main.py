import importlib
import os
import re
import unittest
from unittest.mock import Mock, patch

import pytest
import requests
from web3 import Web3

SAFE = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"


@pytest.fixture(autouse=True)
def _isolate_safe_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use test credentials even when a developer has Safe API keys configured."""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in tuple(os.environ):
        if name.startswith("SAFE_API_KEY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SAFE_API_KEY", "test-key")
    # Tests patch send_telegram_message; treat every protocol as having a destination.
    # Patched on utils.telegram so the reload in each test picks it up.
    monkeypatch.setattr("utils.telegram.telegram_credentials_configured", lambda _protocol: True)


def safe_tx_hash(nonce: int, variant: int = 0) -> str:
    """A deterministic, well-formed safeTxHash for a nonce (``variant`` tells replacements apart)."""
    return f"0x{variant:032x}{nonce:032x}"


def queue_tx(nonce: int, **fields) -> dict:
    """A Safe Queue Service row: string numerics, lowercase addresses, ``created``."""
    return {
        "safeTxHash": safe_tx_hash(nonce),
        "safe": SAFE.lower(),
        "nonce": str(nonce),
        "chainId": "1",
        "to": "0x0000000000000000000000000000000000000abc",
        "data": "0x",
        "operation": 0,
        "created": "2026-06-14T00:00:00Z",
        "proposer": "0x0000000000000000000000000000000000000999",
        "proposedVia": "OWNER",
        "proposedBy": None,
        "value": "0",
        "txHash": None,
        "failed": None,
        **fields,
    }


def queue_response(txs: list[dict], next_url: str | None = None) -> Mock:
    """A 200 response from the ``/queue`` endpoint."""
    response = Mock(status_code=200)
    response.json.return_value = {"count": len(txs), "next": next_url, "previous": None, "results": txs}
    return response


def import_safe_main():
    with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
        import protocols.safe.main as safe_main

        return importlib.reload(safe_main)


class TestSafePendingTransactions(unittest.TestCase):
    """Which queued txs ``get_pending_transactions`` returns, against the real (per-test) cache."""

    SAFE = SAFE

    def setUp(self) -> None:
        self.safe_main = import_safe_main()

    def _pending(self, txs: list[dict], network: str = "mainnet", complete: bool = True) -> list[int]:
        """Run ``get_pending_transactions`` on ``txs`` and return the pending nonces."""
        return [int(tx["nonce"]) for tx in self.safe_main.get_pending_transactions(self.SAFE, network, txs, complete)]

    def _alerted(self, *txs: dict, network: str = "mainnet") -> None:
        for tx in txs:
            self.safe_main.mark_tx_handled(self.SAFE, network, tx)

    def test_alerted_tx_is_not_returned_again(self):
        self._alerted(queue_tx(101))
        self.assertEqual(self._pending([queue_tx(101)]), [])

    def test_lower_nonce_queued_after_a_higher_one_alerted(self):
        self._alerted(queue_tx(101))
        self.assertEqual(self._pending([queue_tx(100), queue_tx(101)]), [100])

    def test_replacement_at_an_alerted_nonce_alerts(self):
        replacement = queue_tx(101, safeTxHash=safe_tx_hash(101, variant=1))
        self._alerted(queue_tx(101))

        pending = self.safe_main.get_pending_transactions(self.SAFE, "mainnet", [queue_tx(101), replacement])

        self.assertEqual([tx["safeTxHash"] for tx in pending], [replacement["safeTxHash"]])

    def test_replacement_by_an_unexpected_proposer_is_critical(self):
        """A different tx at an alerted nonce must still reach the unexpected-proposer check."""
        bot = "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5"
        original = queue_tx(101, proposer=bot)
        replacement = queue_tx(101, safeTxHash=safe_tx_hash(101, variant=1), proposer="0x" + "66" * 20)
        self._alerted(original)

        with (
            patch.object(self.safe_main, "YEARN_EXPECTED_PROPOSERS", {("mainnet", self.SAFE.lower()): {bot}}),
            patch.object(self.safe_main, "_explain_safe_tx", return_value=None),
            patch.object(self.safe_main, "send_telegram_message", return_value=True) as mock_send,
        ):
            self.safe_main.check_for_pending_transactions(self.SAFE, "mainnet", "YEARN_MS", [original, replacement])

        mock_send.assert_called_once()
        message, _protocol, disable_notification = mock_send.call_args.args
        self.assertIn("UNEXPECTED PROPOSER", message)
        self.assertFalse(disable_notification)
        # Both txs at nonce 101 are now handled; the next run alerts on neither.
        self.assertEqual(self._pending([original, replacement]), [])

    def test_txs_without_a_hash_fall_back_to_nonce_dedup(self):
        self._alerted(queue_tx(101, safeTxHash=None))
        self.assertEqual(self._pending([queue_tx(101, safeTxHash=None), queue_tx(102, safeTxHash="")]), [102])

    def _settle_below(self, next_nonce: int, awaiting: frozenset[int] = frozenset()) -> None:
        state = self.safe_main.NonceCheckState(next_nonce, awaiting)
        self.safe_main.save_nonce_check_state(("mainnet", self.SAFE), state)

    def test_settled_txs_no_longer_queued_are_pruned(self):
        """Executed, replaced or deleted txs leave the queue; once their nonce is checked they are dropped."""
        self._alerted(queue_tx(100), queue_tx(101), queue_tx(102, safeTxHash=safe_tx_hash(102, variant=1)))
        self._settle_below(102)

        self.assertEqual(self._pending([queue_tx(102, safeTxHash=safe_tx_hash(102, variant=1)), queue_tx(103)]), [103])

        self.assertEqual(self.safe_main.load_handled_txs(self.SAFE, "mainnet"), {(102, safe_tx_hash(102, variant=1))})

    def test_unsettled_txs_are_kept_for_the_executed_nonce_check(self):
        """The check needs the alerted hashes to tell an alerted execution from a replacement."""
        alerted = {(n, safe_tx_hash(n)) for n in (100, 101, 102)}
        self._alerted(*(queue_tx(n) for n in (100, 101, 102)))

        self._pending([])  # no check progress yet: keep everything
        self.assertEqual(self.safe_main.load_handled_txs(self.SAFE, "mainnet"), alerted)

        self._settle_below(102, awaiting=frozenset({100}))
        self._pending([])
        self.assertEqual(
            self.safe_main.load_handled_txs(self.SAFE, "mainnet"),
            {(100, safe_tx_hash(100)), (102, safe_tx_hash(102))},
        )

    def test_truncated_listing_does_not_prune(self):
        """A tx missing from a truncated listing may still be queued; pruning it would re-alert it."""
        self._alerted(queue_tx(100), queue_tx(101))
        self._settle_below(102)

        self.assertEqual(self._pending([queue_tx(100)], complete=False), [])

        self.assertEqual(
            self.safe_main.load_handled_txs(self.SAFE, "mainnet"), {(100, safe_tx_hash(100)), (101, safe_tx_hash(101))}
        )

    def test_handled_txs_are_stored_compactly(self):
        self._alerted(queue_tx(102), queue_tx(101))
        raw = self.safe_main.get_last_value_for_key_from_file(
            self.safe_main.nonces_filename, self.safe_main._handled_txs_cache_key(self.SAFE, "mainnet")
        )
        self.assertEqual(raw, f"101@{safe_tx_hash(101)},102@{safe_tx_hash(102)}")

    def test_same_address_on_another_chain_has_its_own_cache(self):
        self._alerted(queue_tx(101), network="base-main")
        self.assertEqual(self._pending([queue_tx(101)]), [101])

    def test_first_poll_ignores_the_old_nonce_cache(self):
        """The old cache holds only a nonce, so it cannot prove which proposal at it was delivered."""
        bot = "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5"
        self.safe_main.write_last_value_to_file(self.safe_main.nonces_filename, self.SAFE, 102)
        replacement = queue_tx(101, proposer=bot, safeTxHash=safe_tx_hash(101, variant=1))
        txs = [queue_tx(101, proposer=bot), replacement, queue_tx(102, proposer=bot)]

        self.assertEqual(self._pending(txs), [101, 101, 102])


class TestSafeQueueFetch(unittest.TestCase):
    """One ``/queue`` request lists every watched Safe; each page costs one request against the quota."""

    BASE_SAFE = "0x1111111111111111111111111111111111111111"

    def setUp(self) -> None:
        self.safe_main = import_safe_main()

    def _fetch(self, responses: list, safes: list[tuple[str, str]] | None = None, **kwargs):
        safes = safes or [("mainnet", SAFE), ("base-main", self.BASE_SAFE)]
        with (
            patch.object(self.safe_main.requests, "get", side_effect=responses) as mock_get,
            patch.object(self.safe_main.time, "sleep"),
        ):
            queues, complete = self.safe_main.fetch_queued_transactions(safes, **kwargs)
        return queues, complete, mock_get

    def test_one_request_lists_every_safe_and_groups_rows(self):
        base_tx = queue_tx(7, safe=self.BASE_SAFE, chainId="8453")
        stray = queue_tx(9, safe="0x" + "22" * 20)
        queues, complete, mock_get = self._fetch([queue_response([queue_tx(5), base_tx, stray])])

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.args[0], "https://api.safe.global/queue/api/v1/multisig-transactions/queue")
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            [
                ("safes", f"{SAFE.lower()}:1"),
                ("safes", f"{self.BASE_SAFE}:8453"),
                ("nonceOrder", "asc"),
                ("limit", 100),
            ],
        )
        self.assertTrue(complete)
        self.assertEqual(queues, {("mainnet", SAFE): [queue_tx(5)], ("base-main", self.BASE_SAFE): [base_tx]})

    def test_same_address_on_two_chains_is_kept_apart(self):
        safes = [("mainnet", SAFE), ("base-main", SAFE)]
        base_tx = queue_tx(7, chainId="8453")
        queues, _, _ = self._fetch([queue_response([queue_tx(5), base_tx])], safes)
        self.assertEqual(queues, {("mainnet", SAFE): [queue_tx(5)], ("base-main", SAFE): [base_tx]})

    def test_next_page_is_fetched_and_overlap_is_dropped(self):
        first = queue_response([queue_tx(n) for n in range(100)], next_url="https://next")
        second = queue_response([queue_tx(99), queue_tx(100)])
        queues, complete, mock_get = self._fetch([first, second])

        self.assertEqual([int(tx["nonce"]) for tx in queues[("mainnet", SAFE)]], list(range(101)))
        self.assertTrue(complete)
        self.assertEqual([dict(c.kwargs["params"]).get("offset") for c in mock_get.call_args_list], [None, 100])

    def test_page_cap_bounds_requests_and_marks_the_listing_incomplete(self):
        pages = [queue_response([queue_tx(n)], next_url="https://next") for n in range(3)]
        queues, complete, mock_get = self._fetch(pages, max_pages=2)

        self.assertEqual(mock_get.call_count, 2)
        self.assertFalse(complete)
        self.assertEqual(len(queues[("mainnet", SAFE)]), 2)

    def test_single_safe_cap_keeps_highest_nonces(self):
        pages = [
            queue_response([queue_tx(nonce) for nonce in range(start, start - 100, -1)], next_url="https://next")
            for start in range(500, 0, -100)
        ]
        queues, complete, mock_get = self._fetch(pages, [("mainnet", SAFE)])

        self.assertFalse(complete)
        self.assertEqual(mock_get.call_count, 5)
        self.assertEqual({dict(call.kwargs["params"])["nonceOrder"] for call in mock_get.call_args_list}, {"desc"})
        self.assertEqual([int(tx["nonce"]) for tx in queues[("mainnet", SAFE)]], list(range(500, 0, -1)))

    def test_connection_error_is_retried(self):
        queues, _, mock_get = self._fetch([requests.exceptions.ConnectionError("reset"), queue_response([queue_tx(5)])])
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(queues[("mainnet", SAFE)], [queue_tx(5)])

    def test_exhausted_retries_raise_unavailable(self):
        with self.assertRaises(self.safe_main.SafeApiUnavailable):
            self._fetch([Mock(status_code=503)] * 3)

    def test_validation_error_is_raised_not_swallowed(self):
        rejected = Mock(status_code=422, text='{"detail": "Safe not found on chain"}')
        with self.assertRaises(self.safe_main.SafeApiRequestError) as ctx:
            self._fetch([rejected])
        self.assertIn("422", str(ctx.exception))
        self.assertIn("Safe not found on chain", str(ctx.exception))

    def test_rejected_key_raises_auth_error(self):
        with self.assertRaises(self.safe_main.SafeApiAuthError):
            self._fetch([Mock(status_code=401)])


class TestCheckForPendingTransactions(unittest.TestCase):
    """The alert loop: each alerted (or rule-skipped) tx is recorded as handled right away."""

    def _import_safe_main(self):
        return import_safe_main()

    def _tx(self, nonce: int) -> dict:
        return queue_tx(nonce)

    def test_each_delivered_alert_is_marked_handled(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        pending = [self._tx(2281), self._tx(2280)]  # API returns descending

        with (
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message", return_value=True) as mock_send,
            patch.object(safe_main, "mark_tx_handled") as mock_mark,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", pending)

        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual([c.args for c in mock_mark.call_args_list], [(safe_address, "mainnet", tx) for tx in pending])

    def test_cache_progress_survives_later_send_failure(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        queued = [self._tx(2281), self._tx(2280)]

        with (
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message", side_effect=[True, RuntimeError("telegram down")]),
        ):
            with self.assertRaises(RuntimeError):
                safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", queued)
            # The next run re-sends only the alert that failed.
            pending = safe_main.get_pending_transactions(safe_address, "mainnet", queued)

        self.assertEqual(safe_main.load_handled_txs(safe_address, "mainnet"), {safe_main._tx_id(queued[0])})
        self.assertEqual(pending, [queued[1]])

    def test_skipped_telegram_delivery_retries_on_next_poll(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        tx = self._tx(2281)

        with (
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message", side_effect=[False, True]) as mock_send,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])
            self.assertEqual(safe_main.load_handled_txs(safe_address, "mainnet"), set())

            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])
            self.assertEqual(safe_main.get_pending_transactions(safe_address, "mainnet", [tx]), [])

        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(safe_main.load_handled_txs(safe_address, "mainnet"), {safe_main._tx_id(tx)})

    def test_missing_telegram_destination_defers_without_explaining(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        tx = self._tx(2281)

        with (
            patch.object(safe_main, "_explain_safe_tx", return_value=None) as mock_explain,
            patch.object(safe_main, "_upgrade_alert_lines", return_value=[]) as mock_upgrades,
            patch.object(safe_main, "send_telegram_message", return_value=True) as mock_send,
            patch.object(safe_main, "telegram_credentials_configured", side_effect=[False, False, True]),
        ):
            # Two polls without a destination: no explanation, simulation, upgrade lookup or send.
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])
            mock_explain.assert_not_called()
            mock_upgrades.assert_not_called()
            mock_send.assert_not_called()
            self.assertEqual(safe_main.load_handled_txs(safe_address, "mainnet"), set())

            # Once a destination is configured, the tx alerts and is handled.
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])

        mock_explain.assert_called_once()
        mock_send.assert_called_once()
        self.assertEqual(safe_main.load_handled_txs(safe_address, "mainnet"), {safe_main._tx_id(tx)})

    def test_no_cache_write_when_no_pending(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

        with (
            patch.object(safe_main, "mark_tx_handled") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [])

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
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message", return_value=True) as mock_send,
            patch.object(safe_main, "mark_tx_handled") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "EULER", pending)

        # Only the matching-target tx should produce a Telegram message.
        self.assertEqual(mock_send.call_count, 1)
        # The skipped tx is handled too, so the rule is not re-run on it every poll.
        self.assertEqual([c.args[2]["nonce"] for c in mock_write.call_args_list], ["10", "11"])

    def test_empty_calldata_reaches_explainer(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        tx = {**self._tx(2281), "data": "0x", "value": "1000000000000000000"}

        with (
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx") as mock_explain,
            patch.object(safe_main, "send_telegram_message"),
            patch.object(safe_main, "mark_tx_handled"),
        ):
            mock_explain.return_value = None
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])

        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["hex_data"], "0x")
        self.assertEqual(mock_explain.call_args.kwargs["target"], Web3.to_checksum_address(tx["to"]))

    def test_short_calldata_reaches_explainer(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        tx = {**self._tx(2281), "data": "0x12"}

        with (
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx") as mock_explain,
            patch.object(safe_main, "send_telegram_message"),
            patch.object(safe_main, "mark_tx_handled"),
        ):
            mock_explain.return_value = None
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS", [tx])

        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["hex_data"], "0x12")

    def test_upgrade_nested_in_timelock_call_is_in_the_alert(self):
        from tests.test_calldata_wrappers import (
            CUSD,
            NEW_CUSD_IMPL,
            NEW_ORACLE_IMPL,
            ORACLE,
            execute_batch,
            upgrade_to_and_call,
        )
        from utils.llm.ai_explainer import Explanation

        safe_main = self._import_safe_main()
        safe_address = "0xb8FC49402dF3ee4f8587268FB89fda4d621a8793"
        timelock = "0xD8236031d8279d82E615aF2BFab5FC0127A329ab"
        data = execute_batch([ORACLE, CUSD], [upgrade_to_and_call(NEW_ORACLE_IMPL), upgrade_to_and_call(NEW_CUSD_IMPL)])
        tx = {**self._tx(249), "to": timelock, "data": data, "operation": 0}
        old_impl = "0x" + "ab" * 20
        urls = {
            NEW_ORACLE_IMPL: "https://gist.wavey.info/oracle-diff",
            NEW_CUSD_IMPL: "https://gist.wavey.info/cusd-diff",
        }

        with (
            patch.dict("os.environ", {"WAVEY_GIST_API_KEY": "test-key"}),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx", return_value=Explanation("Upgrades CAP.", "Contract details.")),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled"),
            patch("utils.proxy.get_current_implementation", return_value=old_impl),
            patch(
                "utils.upgrade_diff.publish_upgrade_diff",
                side_effect=lambda _old, new, _chain, **_kw: urls[new],
            ),
            patch(
                "utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/report"
            ) as mock_report,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "CAP", [tx])

        message = mock_send.call_args.args[0]
        self.assertIn(f"🅿️ Proxy: `{ORACLE}` (via executeBatch call 1)", message)
        self.assertIn(f"🅿️ Proxy: `{CUSD}` (via executeBatch call 2)", message)
        self.assertIn(f"🔄 Upgrade: `{old_impl}` → `{NEW_CUSD_IMPL}`", message)
        self.assertIn("📊 [Diff](https://gist.wavey.info/oracle-diff)", message)
        self.assertIn("📊 [Diff](https://gist.wavey.info/cusd-diff)", message)
        report = mock_report.call_args.args[0]
        self.assertIn(
            f"`{ORACLE}`](https://etherscan.io/address/{ORACLE}) [source diff]({urls[NEW_ORACLE_IMPL]})", report
        )
        self.assertIn(f"`{CUSD}`](https://etherscan.io/address/{CUSD}) [source diff]({urls[NEW_CUSD_IMPL]})", report)

    def test_multisend_upgrade_path_names_the_inner_call(self):
        from tests.test_calldata_wrappers import NEW_ORACLE_IMPL, ORACLE, upgrade_to_and_call
        from tests.test_safe_multisend import MULTISEND_CALL_ONLY, pack_multisend

        safe_main = self._import_safe_main()
        transfer = "0xa9059cbb" + "00" * 64
        data = pack_multisend([(0, ORACLE, 0, transfer), (0, ORACLE, 0, upgrade_to_and_call(NEW_ORACLE_IMPL))])
        # v1.3.0, plus the v1.5.0 MultiSendCallOnly and MultiSend deployments.
        for multisend in (
            MULTISEND_CALL_ONLY,
            "0xA83c336B20401Af773B6219BA5027174338D1836",
            "0x218543288004CD07832472D464648173c77D7eB7",
        ):
            with self.subTest(multisend=multisend):
                tx = {**self._tx(10), "to": multisend.lower(), "operation": 1, "data": data}
                upgrades = safe_main._find_safe_tx_upgrades(tx, multisend, data, ORACLE)
                self.assertEqual([(u.proxy_address, u.via) for u in upgrades], [(ORACLE, "multiSend call 2")])

    def test_upgrade_detection_failure_does_not_block_the_alert(self):
        safe_main = self._import_safe_main()
        with (
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_upgrade_alert_lines", side_effect=RuntimeError("rpc down")),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled"),
        ):
            safe_main.check_for_pending_transactions(
                "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52", "mainnet", "CAP", [self._tx(5)]
            )
        mock_send.assert_called_once()

    def test_explain_safe_tx_forwards_empty_calldata(self):
        safe_main = self._import_safe_main()
        safe_tx_hash = "0x" + "12" * 32
        tx = {**self._tx(2281), "data": "0x", "value": "1", "operation": 0, "safeTxHash": safe_tx_hash}

        with patch.object(safe_main, "explain_transaction") as mock_explain:
            mock_explain.return_value = None
            safe_main._explain_safe_tx(
                tx=tx,
                target=tx["to"],
                hex_data="0x",
                chain_id=1,
                protocol="YEARN_MS",
                safe_address="0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52",
                additional_info=None,
            )

        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["calldata"], "0x")
        self.assertEqual(mock_explain.call_args.kwargs["value"], 1)
        self.assertEqual(mock_explain.call_args.kwargs["safe_tx_hash"], safe_tx_hash)

    def _multisend_tx(self, inner_operations: list[int]) -> dict:
        from tests.test_safe_multisend import MULTISEND_CALL_ONLY, pack_multisend

        return {
            **self._tx(2296),
            "safeTxHash": "0x" + "34" * 32,
            "to": MULTISEND_CALL_ONLY.lower(),
            "operation": 1,
            "data": pack_multisend([(op, f"0x{i + 1:040x}", 0, "0x8456cb59") for i, op in enumerate(inner_operations)]),
        }

    def _explain_multisend(self, tx: dict):
        safe_main = self._import_safe_main()
        with patch.object(safe_main, "explain_batch_transaction", return_value=None) as mock_batch:
            safe_main._explain_safe_tx(
                tx=tx,
                target=tx["to"],
                hex_data=tx["data"],
                chain_id=1,
                protocol="YEARN_MS",
                safe_address="0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52",
                additional_info=None,
            )
        mock_batch.assert_called_once()
        return mock_batch.call_args.kwargs

    def test_all_call_multisend_is_simulated_as_a_bundle(self):
        """MultiSend inner CALLs run from the Safe, so a bundle from the Safe is the real execution."""
        tx = self._multisend_tx([0, 0, 0])
        kwargs = self._explain_multisend(tx)
        self.assertFalse(kwargs["skip_simulation"])
        self.assertEqual(kwargs["from_address"], "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52")
        self.assertEqual(kwargs["safe_tx_hash"], tx["safeTxHash"])
        self.assertIn("can be simulated as one ordered bundle", kwargs["context_note"])

    def test_multisend_zero_destination_uses_version_specific_target(self):
        from tests.test_calldata_wrappers import encode_call
        from tests.test_safe_multisend import pack_multisend

        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        inner_data = encode_call("getThreshold()", [], [])
        data = pack_multisend([(0, safe_main.ZERO_ADDRESS, 0, inner_data)])
        for utility, target in (
            ("0x9641d764fc13c8b624c04430c7356c1c7c8102e2", safe_main.ZERO_ADDRESS),  # v1.4.1
            ("0xa83c336b20401af773b6219ba5027174338d1836", safe_address),  # v1.5.0 CallOnly
            ("0x218543288004cd07832472d464648173c77d7eb7", safe_address),  # v1.5.0
        ):
            with self.subTest(utility=utility):
                tx = self._tx(2296) | {"to": utility, "operation": 1, "data": data}
                kwargs = self._explain_multisend(tx)
                self.assertEqual(kwargs["calls"][0]["target"], target)
                self.assertFalse(kwargs["skip_simulation"])

                with patch.object(safe_main, "find_proxy_upgrades", return_value=[]) as mock_find:
                    safe_main._find_safe_tx_upgrades(tx, utility, data, safe_address)
                mock_find.assert_called_once_with(inner_data, target)

    def test_multisend_with_inner_delegatecall_skips_simulation(self):
        kwargs = self._explain_multisend(self._multisend_tx([0, 1]))
        self.assertTrue(kwargs["skip_simulation"])
        self.assertIn("simulation was skipped", kwargs["context_note"])


def test_watched_safes_use_supported_networks() -> None:
    """Require each watched Safe's network to have a chain id (for the Queue Service) and an app.safe.global prefix."""
    from protocols.safe.addresses import ALL_SAFE_ADDRESSES, safe_address_network_prefix
    from utils.chains import safe_network_to_chain_id

    for _, network, *_ in ALL_SAFE_ADDRESSES:
        assert safe_network_to_chain_id(network), f"{network} has no chain id"
        assert network in safe_address_network_prefix, f"{network} has no app.safe.global prefix"


class TestSafeProtocolLabels(unittest.TestCase):
    """The protocol field in ALL_SAFE_ADDRESSES doubles as the Telegram channel key.

    ``send_telegram_message`` resolves credentials via
    ``TELEGRAM_TOPIC_ID_{protocol.upper()}`` / ``TELEGRAM_CHAT_ID_{protocol.upper()}``.
    A label that is not a legal env-var suffix can never resolve, and the alert is
    dropped with only a "Missing Telegram credentials" warning — silent to operators.
    """

    def test_protocol_labels_are_valid_env_var_suffixes(self):
        from protocols.safe.addresses import ALL_SAFE_ADDRESSES

        invalid = [entry[0] for entry in ALL_SAFE_ADDRESSES if not re.fullmatch(r"[A-Za-z0-9_]+", entry[0])]
        self.assertEqual(
            invalid,
            [],
            f"Safe protocol labels must be usable as env-var suffixes, got: {invalid}",
        )


class TestSafeApiQuota(unittest.TestCase):
    SAFES = [("mainnet", SAFE)]

    def _import_safe_main(self, keys: dict[str, str]):
        env = {"SAFE_API_KEY": "", "SAFE_API_KEY_2": "", **keys}
        with patch.dict(os.environ, env):
            import protocols.safe.main as safe_main

            safe_main = importlib.reload(safe_main)
        # main() checks Venice credits first; keep these tests off the network.
        credits_patch = patch.object(safe_main, "check_llm_credits")
        credits_patch.start()
        self.addCleanup(credits_patch.stop)
        return safe_main

    @staticmethod
    def _response(status: int, remaining: str | None = None, reset: str = "3600") -> Mock:
        response = queue_response([]) if status == 200 else Mock(status_code=status)
        response.headers = {} if remaining is None else {"x-ratelimit-remaining": remaining, "x-ratelimit-reset": reset}
        return response

    def _fetch(self, safe_main) -> None:
        safe_main.fetch_queued_transactions(self.SAFES)

    def test_exhausted_key_rotates_to_next_key_without_sleeping(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        ok = self._response(200)

        with (
            patch.object(safe_main.requests, "get", side_effect=[self._response(429, remaining="0"), ok]) as mock_get,
            patch.object(safe_main.time, "sleep") as mock_sleep,
        ):
            self._fetch(safe_main)

        used_keys = [call.kwargs["headers"]["Authorization"] for call in mock_get.call_args_list]
        self.assertEqual(used_keys, ["Bearer k1", "Bearer k2"])
        mock_sleep.assert_not_called()

        # The exhausted key is skipped for the rest of the run.
        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            self._fetch(safe_main)
            self._fetch(safe_main)
        self.assertEqual({c.kwargs["headers"]["Authorization"] for c in mock_get.call_args_list}, {"Bearer k2"})

    def test_exhausted_key_is_skipped_by_later_runs_until_reset(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        ok = self._response(200)

        with patch.object(safe_main.requests, "get", side_effect=[self._response(429, "0", "3600"), ok]):
            self._fetch(safe_main)
        stored = safe_main.get_last_value_for_key_from_file(
            safe_main.cache_filename, safe_main._exhausted_cache_key("k1")
        )
        self.assertNotIn("k1", safe_main._exhausted_cache_key("k1"))  # never persist the key itself

        # Next run: a fresh process that has not seen the 429 yet.
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        safe_main.load_known_exhausted_keys(now=float(stored) - 60)
        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            self._fetch(safe_main)
            self._fetch(safe_main)
        self.assertEqual({c.kwargs["headers"]["Authorization"] for c in mock_get.call_args_list}, {"Bearer k2"})

        # Once the window has reset, the key is used again.
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        safe_main.load_known_exhausted_keys(now=float(stored) + 1)
        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            self._fetch(safe_main)
            self._fetch(safe_main)
        self.assertEqual(
            [c.kwargs["headers"]["Authorization"] for c in mock_get.call_args_list], ["Bearer k1", "Bearer k2"]
        )

    def test_all_keys_known_exhausted_alerts_without_api_requests(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        now = safe_main.time.time()
        for key in ("k1", "k2"):
            safe_main.write_last_value_to_file(
                safe_main.cache_filename, safe_main._exhausted_cache_key(key), int(now + 7200)
            )

        with (
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", [["LIDO", "mainnet", "0x1"]]),
            patch.object(safe_main.requests, "get") as mock_get,
            patch.object(safe_main, "send_error_message") as mock_error,
        ):
            safe_main.main()

        mock_get.assert_not_called()
        mock_error.assert_called_once()

    def test_all_keys_exhausted_raises_with_earliest_reset(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})

        with (
            patch.object(
                safe_main.requests,
                "get",
                side_effect=[self._response(429, "0", "355270"), self._response(429, "0", "1601328")],
            ) as mock_get,
            patch.object(safe_main.time, "sleep") as mock_sleep,
            self.assertRaises(safe_main.SafeApiQuotaExhausted) as ctx,
        ):
            self._fetch(safe_main)

        self.assertEqual(ctx.exception.reset_seconds, 355270)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_not_called()

    def test_short_term_rate_limit_still_backs_off(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})

        with (
            patch.object(safe_main.requests, "get", side_effect=[self._response(429, "12"), self._response(200)]),
            patch.object(safe_main.time, "sleep") as mock_sleep,
        ):
            self._fetch(safe_main)
        mock_sleep.assert_called_once_with(1)

    def test_main_stops_and_alerts_once_per_quota_window(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})
        cache: dict[str, object] = {}
        safes = [["LIDO", "mainnet", "0x1"], ["LIDO", "mainnet", "0x2"]]

        with (
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", safes),
            patch.object(
                safe_main, "fetch_queued_transactions", side_effect=safe_main.SafeApiQuotaExhausted(3600)
            ) as mock_fetch,
            patch.object(safe_main, "check_for_pending_transactions") as mock_check,
            patch.object(safe_main, "get_last_value_for_key_from_file", side_effect=lambda _f, k: cache.get(k, 0)),
            patch.object(safe_main, "write_last_value_to_file", side_effect=lambda _f, k, v: cache.__setitem__(k, v)),
            patch.object(safe_main, "send_error_message") as mock_error,
            patch.object(safe_main, "send_telegram_message") as mock_public,
        ):
            safe_main.main()
            safe_main.main()

        self.assertEqual(mock_fetch.call_count, 2)  # one batched request per run, then stop
        mock_check.assert_not_called()
        mock_public.assert_not_called()  # never to a public channel/topic
        mock_error.assert_called_once()
        message, label = mock_error.call_args.args
        self.assertEqual(label, "yearn")
        self.assertEqual(mock_error.call_args.kwargs["alert_protocol"], "yearn-internal")
        self.assertIn("Safe API quota exhausted", message)


class TestLoadSafeApiKeys(unittest.TestCase):
    def _load(self, env: dict[str, str]) -> list[str]:
        import protocols.safe.main as safe_main

        clean = {k: v for k, v in os.environ.items() if not k.startswith("SAFE_API_KEY")}
        with patch.dict(os.environ, {**clean, **env}, clear=True):
            return safe_main.load_safe_api_keys()

    def test_reads_numbered_keys_in_order(self):
        env = {f"SAFE_API_KEY_{i}": f"k{i}" for i in range(2, 6)}
        self.assertEqual(self._load({"SAFE_API_KEY": "k1", **env}), ["k1", "k2", "k3", "k4", "k5"])

    def test_stops_at_first_missing_key(self):
        keys = self._load({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2", "SAFE_API_KEY_4": "k4"})
        self.assertEqual(keys, ["k1", "k2"])

    def test_stops_at_empty_or_placeholder_values(self):
        for bad in ["", "   ", '""', "null", "None", "undefined", "your-api-key", "<key>", "0"]:
            with self.subTest(bad=bad):
                keys = self._load(
                    {"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2", "SAFE_API_KEY_3": bad, "SAFE_API_KEY_4": "k4"}
                )
                self.assertEqual(keys, ["k1", "k2"])

    def test_no_keys_when_first_is_placeholder(self):
        self.assertEqual(self._load({"SAFE_API_KEY": "your-api-key", "SAFE_API_KEY_2": "k2"}), [])

    def test_strips_whitespace_and_quotes(self):
        self.assertEqual(self._load({"SAFE_API_KEY": ' "k1" ', "SAFE_API_KEY_2": "k2\n"}), ["k1", "k2"])

    def test_three_keys_rotate(self):
        with patch.dict(
            os.environ, {"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2", "SAFE_API_KEY_3": "k3", "SAFE_API_KEY_4": ""}
        ):
            import protocols.safe.main as safe_main

            safe_main = importlib.reload(safe_main)
        self.assertEqual([safe_main._next_api_key() for _ in range(4)], ["k1", "k2", "k3", "k1"])


class TestQueueServiceMigration(unittest.TestCase):
    """Safe Queue Service response fields, proposer escalation and run-level failure handling."""

    SAFE = SAFE
    BOT = "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5"
    OWNER = "0x962228a90eac69238c7d1f216d80037e61ea9255"

    def _tx(self, nonce: int = 10, **fields) -> dict:
        return queue_tx(nonce, **fields)

    def _alert(self, safe_main, txs: list[dict], protocol: str = "YEARN_MS", expected: set[str] | None = None):
        """Run the alert loop for ``txs`` and return the send_telegram_message mock."""
        proposers = {("mainnet", self.SAFE.lower()): expected} if expected else {}
        with (
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", proposers),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled"),
        ):
            safe_main.check_for_pending_transactions(self.SAFE, "mainnet", protocol, txs)
        return mock_send

    # --- proposer escalation ---------------------------------------------------

    def _assert_quiet(self, mock_send) -> None:
        message, _protocol, disable_notification = mock_send.call_args.args
        self.assertNotIn("UNEXPECTED PROPOSER", message)
        self.assertTrue(disable_notification)

    def _assert_critical(self, mock_send) -> None:
        message, _protocol, disable_notification = mock_send.call_args.args
        self.assertIn("UNEXPECTED PROPOSER", message)
        self.assertFalse(disable_notification)

    def test_delegate_proposal_by_known_bot_is_quiet(self):
        tx = self._tx(proposer=self.OWNER, proposedVia="DELEGATE", proposedBy=self.BOT)
        self._assert_quiet(self._alert(import_safe_main(), [tx], expected={self.BOT}))

    def test_nested_owner_signer_on_allowlist_does_not_make_tx_expected(self):
        tx = self._tx(proposer=self.OWNER, proposedVia="NESTED_OWNER", proposedBy=self.BOT)
        self._assert_critical(self._alert(import_safe_main(), [tx], expected={self.BOT}))

    def test_unrecognized_route_does_not_trust_an_allowlisted_proposer(self):
        safe_main = import_safe_main()
        for route in ("SOMETHING_NEW", "UNKNOWN", None):
            with self.subTest(route=route):
                tx = self._tx(proposer=self.BOT, proposedVia=route, proposedBy=self.BOT)
                self._assert_critical(self._alert(safe_main, [tx], expected={self.BOT}))

    def test_owner_proposal_by_known_address_is_quiet(self):
        tx = self._tx(proposer=self.BOT, proposedVia="OWNER", proposedBy=None)
        self._assert_quiet(self._alert(import_safe_main(), [tx], expected={self.BOT}))

    # --- response fields -------------------------------------------------------

    def test_alert_shows_created_date_and_checksummed_target(self):
        message = self._alert(import_safe_main(), [self._tx(created="2026-11-17T10:37:02Z")]).call_args.args[0]
        self.assertIn("📅 Submission Date: 2026-11-17T10:37:02Z", message)
        self.assertIn("0x0000000000000000000000000000000000000aBc", message)

    def test_null_destination_is_the_zero_address(self):
        safe_main = import_safe_main()
        message = self._alert(safe_main, [self._tx(to=None)]).call_args.args[0]
        self.assertIn(f"Target Contract Address: {safe_main.ZERO_ADDRESS}", message)

    def test_null_calldata_does_not_crash_any_protocol_path(self):
        safe_main = import_safe_main()
        vault = "0x797dd80692c3b2dadabce8e30c07fde5307d48a9"
        cases = {
            "PENDLE": (self._tx(data=None), 0),
            "EULER": (self._tx(to=vault, data=None), 1),
            "LIDO": (self._tx(data=None), 1),
        }
        for protocol, (tx, alerts) in cases.items():
            with self.subTest(protocol):
                self.assertEqual(self._alert(safe_main, [tx], protocol=protocol).call_count, alerts)

    def test_pendle_alerts_on_privileged_call_only(self):
        from tests.test_calldata_wrappers import NEW_ORACLE_IMPL, ORACLE, pendle_aggregate, upgrade_to_and_call

        safe_main = import_safe_main()
        upgrade = self._tx(11, data=pendle_aggregate([(ORACLE, 0, upgrade_to_and_call(NEW_ORACLE_IMPL))]))
        routine = self._tx(10, data=pendle_aggregate([(ORACLE, 0, "0x8456cb59")]))
        with (
            patch("utils.proxy.get_current_implementation", return_value=None),
            patch("utils.upgrade_diff.publish_upgrade_diff", return_value=None),
        ):
            mock_send = self._alert(safe_main, [routine, upgrade], protocol="PENDLE")
        self.assertEqual(mock_send.call_count, 1)
        self.assertIn("#️⃣ Nonce: 11", mock_send.call_args.args[0])

    # --- run-level failure handling ----------------------------------------------

    SAFES = [["PENDLE", "mainnet", "0x1"], ["LIDO", "mainnet", "0x2"]]

    def _main(self, safe_main, fetch, check=None, error=None):
        """Run ``main()`` over ``SAFES`` and return the (fetch, check, error) mocks."""
        with (
            patch.object(safe_main, "check_llm_credits"),
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", self.SAFES),
            patch.object(safe_main, "fetch_queued_transactions", side_effect=fetch) as mock_fetch,
            patch.object(safe_main, "check_for_pending_transactions", side_effect=check) as mock_check,
            patch.object(safe_main, "send_error_message", side_effect=error) as mock_error,
            patch.object(safe_main, "check_executed_nonces") as mock_executed,
            patch.object(safe_main.time, "sleep"),
        ):
            safe_main.main()
        mock_executed.assert_called_once_with(self.SAFES)
        return mock_fetch, mock_check, mock_error

    def test_one_batched_request_feeds_every_safe(self):
        safe_main = import_safe_main()
        txs = [self._tx(5)]
        queues = {("mainnet", "0x1"): txs, ("mainnet", "0x2"): []}
        mock_fetch, mock_check, _ = self._main(safe_main, [(queues, True)])

        mock_fetch.assert_called_once_with([("mainnet", "0x1"), ("mainnet", "0x2")])
        self.assertEqual(
            [c.args for c in mock_check.call_args_list],
            [("0x1", "mainnet", "PENDLE", txs, True), ("0x2", "mainnet", "LIDO", [], True)],
        )

    def test_rejected_batch_falls_back_to_one_request_per_safe(self):
        """A Safe the service cannot resolve must not blind the others."""
        safe_main = import_safe_main()
        fetch = [
            safe_main.SafeApiRequestError("422 Safe not found"),
            safe_main.SafeApiRequestError("422 Safe not found"),
            ({("mainnet", "0x2"): []}, True),
        ]
        mock_fetch, mock_check, mock_error = self._main(safe_main, fetch)

        self.assertEqual(
            [(c.args, c.kwargs) for c in mock_fetch.call_args_list[1:]],
            [(([("mainnet", "0x1")],), {"max_retries": 1}), (([("mainnet", "0x2")],), {"max_retries": 1})],
        )
        self.assertEqual([c.args for c in mock_check.call_args_list], [("0x2", "mainnet", "LIDO", [], True)])
        mock_error.assert_called_once()
        self.assertIn("0x1", mock_error.call_args.args[0])

    def test_oversized_shared_listing_does_not_hide_another_safes_queue(self):
        safe_main = import_safe_main()
        safes = [[protocol, "mainnet", f"0x{index:040x}"] for index, protocol in enumerate(("LIDO", "CAP", "AAVE"), 1)]
        rows = {
            safes[0][2]: [queue_tx(nonce, safe=safes[0][2]) for nonce in range(250)],
            safes[1][2]: [queue_tx(nonce, safe=safes[1][2]) for nonce in range(250, 500)],
            safes[2][2]: [queue_tx(500, safe=safes[2][2])],
        }

        def respond(_url: str, *, params: list[tuple[str, str | int]], **_kwargs: object) -> Mock:
            options = dict(params)
            selected = [tx for key, value in params if key == "safes" for tx in rows[str(value).split(":")[0]]]
            selected.sort(key=lambda tx: int(tx["nonce"]), reverse=options["nonceOrder"] == "desc")
            offset = int(options.get("offset", 0))
            end = offset + int(options["limit"])
            response = queue_response(selected[offset:end], next_url="https://next" if end < len(selected) else None)
            response.json.return_value["count"] = len(selected)
            return response

        with (
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", safes),
            patch.object(safe_main, "check_llm_credits"),
            patch.object(safe_main.requests, "get", side_effect=respond) as get,
            patch.object(safe_main.time, "monotonic", return_value=10),
            patch.object(safe_main.time, "sleep") as sleep,
            patch.object(safe_main, "check_for_pending_transactions") as check,
            patch.object(safe_main, "check_executed_nonces"),
        ):
            safe_main.main()

        self.assertEqual(get.call_count, 8)  # One shared page, then 3 + 3 + 1 individual pages.
        sleep.assert_called_once_with(1)
        self.assertEqual([len(call.args[3]) for call in check.call_args_list], [250, 250, 1])
        check.assert_any_call(safes[2][2], "mainnet", "AAVE", rows[safes[2][2]], True)

    def test_service_wide_failures_crash_the_run_for_one_alert(self):
        safe_main = import_safe_main()
        for exc in (safe_main.SafeApiUnavailable("retries exhausted"), safe_main.SafeApiAuthError("bad key")):
            with (
                self.subTest(type(exc).__name__),
                patch.object(safe_main, "check_llm_credits"),
                patch.object(safe_main, "fetch_queued_transactions", side_effect=exc),
                patch.object(safe_main, "check_executed_nonces") as mock_executed,
                self.assertRaises(type(exc)),
            ):
                safe_main.main()
            mock_executed.assert_not_called()

    def test_executed_check_failure_alerts_internally(self):
        safe_main = import_safe_main()
        queues = {("mainnet", "0x1"): [], ("mainnet", "0x2"): []}
        with (
            patch.object(safe_main, "check_llm_credits"),
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", self.SAFES),
            patch.object(safe_main, "fetch_queued_transactions", return_value=(queues, True)),
            patch.object(safe_main, "check_for_pending_transactions"),
            patch.object(safe_main, "check_executed_nonces", side_effect=safe_main.SafeApiUnavailable("down")),
            patch.object(safe_main, "send_error_message") as mock_error,
        ):
            safe_main.main()
        mock_error.assert_called_once()
        self.assertIn("executed Safe txs", mock_error.call_args.args[0])
        self.assertEqual(mock_error.call_args.kwargs["alert_protocol"], "yearn-internal")

    def test_one_failing_safe_alerts_and_the_rest_still_run(self):
        safe_main = import_safe_main()
        queues = {("mainnet", "0x1"): [], ("mainnet", "0x2"): []}
        _, mock_check, mock_error = self._main(safe_main, [(queues, True)], check=[TypeError("boom"), None])

        self.assertEqual(mock_check.call_count, 2)
        mock_error.assert_called_once()
        message, label = mock_error.call_args.args
        self.assertEqual(label, "yearn")
        self.assertEqual(mock_error.call_args.kwargs["alert_protocol"], "yearn-internal")
        self.assertIn("0x1", message)
        self.assertIn("TypeError: boom", message)

    def test_failed_error_alert_does_not_skip_the_remaining_safes(self):
        from utils.telegram import TelegramError

        safe_main = import_safe_main()
        queues = {("mainnet", "0x1"): [], ("mainnet", "0x2"): []}
        _, mock_check, mock_error = self._main(
            safe_main, [(queues, True)], check=[TypeError("boom"), None], error=TelegramError("chat not found")
        )

        self.assertEqual(mock_check.call_count, 2)
        mock_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
