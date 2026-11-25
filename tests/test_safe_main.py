import importlib
import os
import re
import unittest
from unittest.mock import Mock, patch

import pytest
import requests
from web3 import Web3


@pytest.fixture(autouse=True)
def _isolate_safe_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use test credentials even when a developer has Safe API keys configured."""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in tuple(os.environ):
        if name.startswith("SAFE_API_KEY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SAFE_API_KEY", "test-key")


def safe_tx_hash(nonce: int, variant: int = 0) -> str:
    """A deterministic, well-formed safeTxHash for a nonce (``variant`` tells replacements apart)."""
    return f"0x{variant:032x}{nonce:032x}"


def queue_tx(nonce: int, **fields) -> dict:
    """A Safe Queue Service row: string numerics, lowercase addresses, ``created``."""
    return {
        "safeTxHash": safe_tx_hash(nonce),
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


class TestSafePendingTransactions(unittest.TestCase):
    """Which queued txs ``get_pending_transactions`` returns, against the real (per-test) cache."""

    SAFE = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    def setUp(self) -> None:
        self.safe_main = self._import_safe_main()

    def _pending(
        self, txs: list[dict], current_nonce: int | None, legacy_nonce: int = 0, network: str = "mainnet"
    ) -> list[int]:
        """Run ``get_pending_transactions`` on ``txs`` and return the pending nonces."""
        with (
            patch.object(self.safe_main, "get_last_executed_nonce_from_file", return_value=legacy_nonce),
            patch.object(self.safe_main, "get_safe_current_nonce", return_value=current_nonce),
            patch.object(self.safe_main, "get_safe_transactions", return_value=txs),
        ):
            pending = self.safe_main.get_pending_transactions(self.SAFE, network)
        return [int(tx["nonce"]) for tx in pending]

    def _alerted(self, *txs: dict, network: str = "mainnet") -> None:
        for tx in txs:
            self.safe_main.mark_tx_handled(self.SAFE, network, tx)

    def test_get_safe_transactions_retries_on_connection_error(self):
        safe_main = self.safe_main

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
            result = safe_main.get_safe_transactions("0xSafe", "base-main")

        self.assertEqual(result, [{"nonce": 5}])
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once()

    def test_get_safe_transactions_returns_empty_after_exhausting_retries(self):
        safe_main = self.safe_main

        with (
            patch.object(
                safe_main.requests,
                "get",
                side_effect=requests.exceptions.ConnectionError("Connection reset by peer"),
            ) as mock_get,
            patch.object(safe_main.time, "sleep"),
        ):
            result = safe_main.get_safe_transactions("0xSafe", "katana-main", max_retries=3)

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 3)

    def test_executed_rows_are_filtered_even_when_api_returns_them(self):
        txs = [
            queue_tx(30, txHash="0x" + "ab" * 32, failed=False),
            queue_tx(31, failed=True),
            queue_tx(32, txHash="0x" + "cd" * 32),
            queue_tx(33),
        ]
        self.assertEqual(self._pending(txs, current_nonce=30), [33])

    def test_current_nonce_unknown_fails_closed(self):
        self.assertEqual(self._pending([queue_tx(3274), queue_tx(3280)], current_nonce=None), [])
        self.assertIsNone(self.safe_main.load_handled_txs(self.SAFE, "mainnet"))

    def test_dead_slot_rows_below_current_nonce_never_alert(self):
        """A tx below the onchain nonce can never execute, even if it was never alerted on."""
        self.assertEqual(self._pending([queue_tx(3282), queue_tx(3281), queue_tx(3280)], current_nonce=3282), [3282])

    def test_alerted_tx_is_not_returned_again(self):
        self._alerted(queue_tx(101))
        self.assertEqual(self._pending([queue_tx(101)], current_nonce=100), [])

    def test_lower_nonce_queued_after_a_higher_one_alerted(self):
        self._alerted(queue_tx(101))
        self.assertEqual(self._pending([queue_tx(101), queue_tx(100)], current_nonce=100), [100])

    def test_replacement_at_an_alerted_nonce_alerts(self):
        replacement = queue_tx(101, safeTxHash=safe_tx_hash(101, variant=1))
        self._alerted(queue_tx(101))

        with (
            patch.object(self.safe_main, "get_safe_current_nonce", return_value=100),
            patch.object(self.safe_main, "get_safe_transactions", return_value=[replacement, queue_tx(101)]),
        ):
            pending = self.safe_main.get_pending_transactions(self.SAFE, "mainnet")

        self.assertEqual([tx["safeTxHash"] for tx in pending], [replacement["safeTxHash"]])

    def test_replacement_by_an_unexpected_proposer_is_critical(self):
        """A different tx at an alerted nonce must still reach the unexpected-proposer check."""
        bot = "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5"
        original = queue_tx(101, proposer=bot)
        replacement = queue_tx(101, safeTxHash=safe_tx_hash(101, variant=1), proposer="0x" + "66" * 20)
        self._alerted(original)

        with (
            patch.object(self.safe_main, "get_safe_current_nonce", return_value=100),
            patch.object(self.safe_main, "get_safe_transactions", return_value=[replacement, original]),
            patch.object(self.safe_main, "YEARN_EXPECTED_PROPOSERS", {("mainnet", self.SAFE.lower()): {bot}}),
            patch.object(self.safe_main, "_explain_safe_tx", return_value=None),
            patch.object(self.safe_main, "send_telegram_message") as mock_send,
        ):
            self.safe_main.check_for_pending_transactions(self.SAFE, "mainnet", "YEARN_MS")

        mock_send.assert_called_once()
        message, _protocol, disable_notification = mock_send.call_args.args
        self.assertIn("UNEXPECTED PROPOSER", message)
        self.assertFalse(disable_notification)
        # Both txs at nonce 101 are now handled; the next run alerts on neither.
        self.assertEqual(self._pending([replacement, original], current_nonce=100), [])

    def test_txs_without_a_hash_fall_back_to_nonce_dedup(self):
        self._alerted(queue_tx(101, safeTxHash=None))
        self.assertEqual(self._pending([queue_tx(101, safeTxHash=None), queue_tx(102, safeTxHash="")], 100), [102])

    def test_handled_txs_below_current_nonce_are_pruned(self):
        self._alerted(queue_tx(100), queue_tx(101), queue_tx(102, safeTxHash=safe_tx_hash(102, variant=1)))

        self.assertEqual(self._pending([queue_tx(102)], current_nonce=102), [102])

        self.assertEqual(self.safe_main.load_handled_txs(self.SAFE, "mainnet"), {(102, safe_tx_hash(102, variant=1))})

    def test_handled_txs_are_stored_compactly(self):
        self._alerted(queue_tx(102), queue_tx(101))
        raw = self.safe_main.get_last_value_for_key_from_file(
            self.safe_main.nonces_filename, self.safe_main._handled_txs_cache_key(self.SAFE, "mainnet")
        )
        self.assertEqual(raw, f"101@{safe_tx_hash(101)},102@{safe_tx_hash(102)}")

    def test_same_address_on_another_chain_has_its_own_cache(self):
        self._alerted(queue_tx(101), network="base-main")
        self.assertEqual(self._pending([queue_tx(101)], current_nonce=100), [101])

    def test_first_run_seeds_from_the_old_nonce_cache(self):
        """Switching to hash dedup must not re-alert txs the old nonce cache already covered."""
        txs = [queue_tx(103), queue_tx(102), queue_tx(101), queue_tx(100)]

        self.assertEqual(self._pending(txs, current_nonce=101, legacy_nonce=102), [103])

        self.assertEqual(
            self.safe_main.load_handled_txs(self.SAFE, "mainnet"),
            {(101, safe_tx_hash(101)), (102, safe_tx_hash(102))},
        )
        # Once seeded, the old cache is no longer read.
        self.assertEqual(self._pending(txs, current_nonce=101, legacy_nonce=10**6), [103])

    def test_more_than_ten_eligible_txs_are_all_returned(self):
        txs = [queue_tx(nonce) for nonce in range(124, 99, -1)]  # 25 queued, descending
        ok = Mock(status_code=200)
        ok.json.return_value = {"results": txs}

        with (
            patch.object(self.safe_main, "get_safe_current_nonce", return_value=100),
            patch.object(self.safe_main.requests, "get", return_value=ok) as mock_get,
        ):
            pending = self.safe_main.get_pending_transactions(self.SAFE, "mainnet")

        self.assertEqual(len(pending), 25)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["params"]["limit"], 100)


class TestSafeQueuePaging(unittest.TestCase):
    """A second page costs quota, so it is fetched only when the first may hide wanted txs."""

    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    @staticmethod
    def _page(nonces: range) -> Mock:
        response = Mock(status_code=200)
        response.json.return_value = {"results": [queue_tx(nonce) for nonce in nonces]}
        return response

    def _fetch(self, pages: list[Mock], min_nonce: int | None, **kwargs) -> tuple[list[int], list[dict]]:
        safe_main = self._import_safe_main()
        with patch.object(safe_main.requests, "get", side_effect=pages) as mock_get:
            txs = safe_main.get_safe_transactions("0xSafe", "mainnet", executed=False, min_nonce=min_nonce, **kwargs)
        return [int(tx["nonce"]) for tx in txs], [call.kwargs["params"] for call in mock_get.call_args_list]

    def test_full_page_of_eligible_txs_fetches_the_next_page(self):
        nonces, params = self._fetch([self._page(range(249, 149, -1)), self._page(range(149, 119, -1))], 120)

        self.assertEqual(nonces, list(range(249, 119, -1)))
        self.assertEqual([p.get("offset") for p in params], [None, 100])
        self.assertEqual({p["limit"] for p in params}, {100})

    def test_full_page_reaching_dead_slots_stops(self):
        nonces, params = self._fetch([self._page(range(249, 149, -1))], 200)
        self.assertEqual(len(nonces), 100)
        self.assertEqual(len(params), 1)

    def test_partial_page_stops(self):
        _, params = self._fetch([self._page(range(130, 119, -1))], 0)
        self.assertEqual(len(params), 1)

    def test_without_min_nonce_only_the_first_page_is_fetched(self):
        _, params = self._fetch([self._page(range(249, 149, -1))], None)
        self.assertEqual(len(params), 1)

    def test_page_cap_bounds_requests(self):
        pages = [self._page(range(start, start - 2, -1)) for start in (9, 7, 5)]
        nonces, params = self._fetch(pages, 0, limit=2, max_pages=2)
        self.assertEqual(nonces, [9, 8, 7, 6])
        self.assertEqual(len(params), 2)


class TestCheckForPendingTransactions(unittest.TestCase):
    """The alert loop: each alerted (or rule-skipped) tx is recorded as handled right away."""

    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    def _tx(self, nonce: int) -> dict:
        return queue_tx(nonce)

    def test_each_delivered_alert_is_marked_handled(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        pending = [self._tx(2281), self._tx(2280)]  # API returns descending

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=pending),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled") as mock_mark,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual([c.args for c in mock_mark.call_args_list], [(safe_address, "mainnet", tx) for tx in pending])

    def test_cache_progress_survives_later_send_failure(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        queued = [self._tx(2281), self._tx(2280)]

        with (
            patch.object(safe_main, "get_safe_current_nonce", return_value=2280),
            patch.object(safe_main, "get_safe_transactions", return_value=queued),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message", side_effect=[None, RuntimeError("telegram down")]),
        ):
            with self.assertRaises(RuntimeError):
                safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")
            # The next run re-sends only the alert that failed.
            pending = safe_main.get_pending_transactions(safe_address, "mainnet")

        self.assertEqual(safe_main.load_handled_txs(safe_address, "mainnet"), {safe_main._tx_id(queued[0])})
        self.assertEqual(pending, [queued[1]])

    def test_no_cache_write_when_no_pending(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[]),
            patch.object(safe_main, "mark_tx_handled") as mock_write,
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
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled") as mock_write,
        ):
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "EULER")

        # Only the matching-target tx should produce a Telegram message.
        self.assertEqual(mock_send.call_count, 1)
        # The skipped tx is handled too, so the rule is not re-run on it every poll.
        self.assertEqual([c.args[2]["nonce"] for c in mock_write.call_args_list], ["10", "11"])

    def test_empty_calldata_reaches_explainer(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        tx = {**self._tx(2281), "data": "0x", "value": "1000000000000000000"}

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[tx]),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx") as mock_explain,
            patch.object(safe_main, "send_telegram_message"),
            patch.object(safe_main, "mark_tx_handled"),
        ):
            mock_explain.return_value = None
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["hex_data"], "0x")
        self.assertEqual(mock_explain.call_args.kwargs["target"], Web3.to_checksum_address(tx["to"]))

    def test_short_calldata_reaches_explainer(self):
        safe_main = self._import_safe_main()
        safe_address = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
        tx = {**self._tx(2281), "data": "0x12"}

        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[tx]),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_explain_safe_tx") as mock_explain,
            patch.object(safe_main, "send_telegram_message"),
            patch.object(safe_main, "mark_tx_handled"),
        ):
            mock_explain.return_value = None
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "YEARN_MS")

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
            patch.object(safe_main, "get_pending_transactions", return_value=[tx]),
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
            safe_main.check_for_pending_transactions(safe_address, "mainnet", "CAP")

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
                upgrades = safe_main._find_safe_tx_upgrades(tx, multisend, data)
                self.assertEqual([(u.proxy_address, u.via) for u in upgrades], [(ORACLE, "multiSend call 2")])

    def test_upgrade_detection_failure_does_not_block_the_alert(self):
        safe_main = self._import_safe_main()
        with (
            patch.object(safe_main, "get_pending_transactions", return_value=[self._tx(5)]),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", {}),
            patch.object(safe_main, "_upgrade_alert_lines", side_effect=RuntimeError("rpc down")),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled"),
        ):
            safe_main.check_for_pending_transactions("0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52", "mainnet", "CAP")
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


if __name__ == "__main__":
    unittest.main()


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
    def _response(status: int, remaining: str | None = None, reset: str = "3600", results=None) -> Mock:
        response = Mock(status_code=status)
        response.headers = {} if remaining is None else {"x-ratelimit-remaining": remaining, "x-ratelimit-reset": reset}
        response.json.return_value = {"results": results or []}
        return response

    def test_exhausted_key_rotates_to_next_key_without_sleeping(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        exhausted = self._response(429, remaining="0")
        ok = self._response(200, results=[{"nonce": 7}])

        with (
            patch.object(safe_main.requests, "get", side_effect=[exhausted, ok]) as mock_get,
            patch.object(safe_main.time, "sleep") as mock_sleep,
        ):
            result = safe_main.get_safe_transactions("0xSafe", "mainnet")

        self.assertEqual(result, [{"nonce": 7}])
        used_keys = [call.kwargs["headers"]["Authorization"] for call in mock_get.call_args_list]
        self.assertEqual(used_keys, ["Bearer k1", "Bearer k2"])
        mock_sleep.assert_not_called()

        # The exhausted key is skipped for the rest of the run.
        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            safe_main.get_safe_transactions("0xSafe", "mainnet")
            safe_main.get_safe_transactions("0xSafe", "mainnet")
        self.assertEqual({c.kwargs["headers"]["Authorization"] for c in mock_get.call_args_list}, {"Bearer k2"})

    def test_exhausted_key_is_skipped_by_later_runs_until_reset(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        ok = self._response(200, results=[{"nonce": 7}])

        with patch.object(safe_main.requests, "get", side_effect=[self._response(429, "0", "3600"), ok]):
            safe_main.get_safe_transactions("0xSafe", "mainnet")
        stored = safe_main.get_last_value_for_key_from_file(
            safe_main.cache_filename, safe_main._exhausted_cache_key("k1")
        )
        self.assertNotIn("k1", safe_main._exhausted_cache_key("k1"))  # never persist the key itself

        # Next run: a fresh process that has not seen the 429 yet.
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        safe_main.load_known_exhausted_keys(now=float(stored) - 60)
        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            safe_main.get_safe_transactions("0xSafe", "mainnet")
            safe_main.get_safe_transactions("0xSafe", "mainnet")
        self.assertEqual({c.kwargs["headers"]["Authorization"] for c in mock_get.call_args_list}, {"Bearer k2"})

        # Once the window has reset, the key is used again.
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1", "SAFE_API_KEY_2": "k2"})
        safe_main.load_known_exhausted_keys(now=float(stored) + 1)
        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            safe_main.get_safe_transactions("0xSafe", "mainnet")
            safe_main.get_safe_transactions("0xSafe", "mainnet")
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
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=0),
            patch.object(safe_main, "get_safe_current_nonce", return_value=1),
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
            safe_main.get_safe_transactions("0xSafe", "mainnet")

        self.assertEqual(ctx.exception.reset_seconds, 355270)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_not_called()

    def test_short_term_rate_limit_still_backs_off(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})

        with (
            patch.object(
                safe_main.requests,
                "get",
                side_effect=[self._response(429, "12"), self._response(200, results=[{"nonce": 1}])],
            ),
            patch.object(safe_main.time, "sleep") as mock_sleep,
        ):
            self.assertEqual(safe_main.get_safe_transactions("0xSafe", "mainnet"), [{"nonce": 1}])
        mock_sleep.assert_called_once_with(1)

    def test_main_stops_and_alerts_once_per_quota_window(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})
        cache: dict[str, object] = {}
        safes = [["LIDO", "mainnet", "0x1"], ["LIDO", "mainnet", "0x2"]]

        with (
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", safes),
            patch.object(safe_main, "run_for_network", side_effect=safe_main.SafeApiQuotaExhausted(3600)) as mock_run,
            patch.object(safe_main, "get_last_value_for_key_from_file", side_effect=lambda _f, k: cache.get(k, 0)),
            patch.object(safe_main, "write_last_value_to_file", side_effect=lambda _f, k, v: cache.__setitem__(k, v)),
            patch.object(safe_main, "send_error_message") as mock_error,
            patch.object(safe_main, "send_telegram_message") as mock_public,
        ):
            safe_main.main()
            safe_main.main()

        self.assertEqual(mock_run.call_count, 2)  # one safe per run, then stop
        mock_public.assert_not_called()  # never to a public channel/topic
        mock_error.assert_called_once()
        message, label = mock_error.call_args.args
        self.assertEqual(label, "yearn")
        self.assertEqual(mock_error.call_args.kwargs["alert_protocol"], "yearn-internal")
        self.assertIn("Safe API quota exhausted", message)

    def test_unknown_nonce_skips_safe_api_call(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})

        with (
            patch.object(safe_main, "get_last_executed_nonce_from_file", return_value=0),
            patch.object(safe_main, "get_safe_current_nonce", return_value=None),
            patch.object(safe_main, "get_safe_transactions") as mock_txs,
        ):
            self.assertEqual(safe_main.get_pending_transactions("0xSafe", "mainnet"), [])
        mock_txs.assert_not_called()

    def test_current_nonce_is_read_onchain(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})
        client = Mock()
        client.eth.contract.return_value.functions.nonce.return_value.call.return_value = 42

        with (
            patch.object(safe_main.ChainManager, "get_client", return_value=client) as mock_get_client,
            patch.object(safe_main.requests, "get") as mock_http,
        ):
            nonce = safe_main.get_safe_current_nonce("0x73b047fe6337183A454c5217241D780a932777bD", "katana-main")

        self.assertEqual(nonce, 42)
        mock_get_client.assert_called_once_with(safe_main.Chain.KATANA)
        mock_http.assert_not_called()

    def test_current_nonce_rpc_failure_returns_none(self):
        safe_main = self._import_safe_main({"SAFE_API_KEY": "k1"})

        with patch.object(safe_main.ChainManager, "get_client", side_effect=ValueError("No providers")):
            self.assertIsNone(safe_main.get_safe_current_nonce("0x73b047fe6337183A454c5217241D780a932777bD", "mainnet"))


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
    """Safe Queue Service request shape, response fields and failure handling."""

    SAFE = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"
    BOT = "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5"
    OWNER = "0x962228a90eac69238c7d1f216d80037e61ea9255"

    def _import_safe_main(self):
        with patch.dict(os.environ, {"SAFE_API_KEY": "test-key"}):
            import protocols.safe.main as safe_main

            return importlib.reload(safe_main)

    def _tx(self, nonce: int = 10, **fields) -> dict:
        return queue_tx(nonce, **fields)

    def _alert(self, safe_main, txs: list[dict], protocol: str = "YEARN_MS", expected: set[str] | None = None):
        """Run the alert loop for ``txs`` and return the send_telegram_message mock."""
        proposers = {("mainnet", self.SAFE.lower()): expected} if expected else {}
        with (
            patch.object(safe_main, "get_pending_transactions", return_value=txs),
            patch.object(safe_main, "YEARN_EXPECTED_PROPOSERS", proposers),
            patch.object(safe_main, "_explain_safe_tx", return_value=None),
            patch.object(safe_main, "send_telegram_message") as mock_send,
            patch.object(safe_main, "mark_tx_handled"),
        ):
            safe_main.check_for_pending_transactions(self.SAFE, "mainnet", protocol)
        return mock_send

    # --- request -------------------------------------------------------------

    def test_request_targets_queue_service_with_chain_id_and_nonce_order(self):
        safe_main = self._import_safe_main()
        ok = Mock(status_code=200)
        ok.json.return_value = {"results": []}

        with patch.object(safe_main.requests, "get", return_value=ok) as mock_get:
            safe_main.get_safe_transactions(self.SAFE, "base-main", executed=False)

        url = mock_get.call_args.args[0]
        self.assertEqual(url, f"https://api.safe.global/queue/api/v1/safes/{self.SAFE}/multisig-transactions")
        self.assertFalse(url.endswith("/"))
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {"chainId": 8453, "limit": 100, "nonceOrder": "desc", "executed": "false"},
        )

    def test_validation_error_is_raised_not_swallowed(self):
        safe_main = self._import_safe_main()
        rejected = Mock(status_code=422, text='{"detail": "Invalid chainId"}')

        with (
            patch.object(safe_main.requests, "get", return_value=rejected),
            self.assertRaises(safe_main.SafeApiRequestError) as ctx,
        ):
            safe_main.get_safe_transactions(self.SAFE, "mainnet")
        self.assertIn("422", str(ctx.exception))
        self.assertIn("Invalid chainId", str(ctx.exception))

    def test_rejected_key_raises_auth_error(self):
        safe_main = self._import_safe_main()
        with (
            patch.object(safe_main.requests, "get", return_value=Mock(status_code=401)),
            self.assertRaises(safe_main.SafeApiAuthError),
        ):
            safe_main.get_safe_transactions(self.SAFE, "mainnet")

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
        safe_main = self._import_safe_main()
        tx = self._tx(proposer=self.OWNER, proposedVia="DELEGATE", proposedBy=self.BOT)
        self._assert_quiet(self._alert(safe_main, [tx], expected={self.BOT}))

    def test_nested_owner_signer_on_allowlist_does_not_make_tx_expected(self):
        safe_main = self._import_safe_main()
        tx = self._tx(proposer=self.OWNER, proposedVia="NESTED_OWNER", proposedBy=self.BOT)
        self._assert_critical(self._alert(safe_main, [tx], expected={self.BOT}))

    def test_unrecognized_route_does_not_trust_an_allowlisted_proposer(self):
        safe_main = self._import_safe_main()
        for route in ("SOMETHING_NEW", "UNKNOWN", None):
            with self.subTest(route=route):
                tx = self._tx(proposer=self.BOT, proposedVia=route, proposedBy=self.BOT)
                self._assert_critical(self._alert(safe_main, [tx], expected={self.BOT}))

    def test_owner_proposal_by_known_address_is_quiet(self):
        safe_main = self._import_safe_main()
        tx = self._tx(proposer=self.BOT, proposedVia="OWNER", proposedBy=None)
        self._assert_quiet(self._alert(safe_main, [tx], expected={self.BOT}))

    # --- response fields -------------------------------------------------------

    def test_alert_shows_created_date_and_checksummed_target(self):
        safe_main = self._import_safe_main()
        message = self._alert(safe_main, [self._tx(created="2026-11-17T10:37:02Z")]).call_args.args[0]
        self.assertIn("📅 Submission Date: 2026-11-17T10:37:02Z", message)
        self.assertIn("0x0000000000000000000000000000000000000aBc", message)

    def test_null_destination_is_the_zero_address(self):
        safe_main = self._import_safe_main()
        message = self._alert(safe_main, [self._tx(to=None)]).call_args.args[0]
        self.assertIn(f"Target Contract Address: {safe_main.ZERO_ADDRESS}", message)

    def test_null_calldata_does_not_crash_any_protocol_path(self):
        safe_main = self._import_safe_main()
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

        safe_main = self._import_safe_main()
        upgrade = self._tx(11, data=pendle_aggregate([(ORACLE, 0, upgrade_to_and_call(NEW_ORACLE_IMPL))]))
        routine = self._tx(10, data=pendle_aggregate([(ORACLE, 0, "0x8456cb59")]))
        with (
            patch("utils.proxy.get_current_implementation", return_value=None),
            patch("utils.upgrade_diff.publish_upgrade_diff", return_value=None),
        ):
            mock_send = self._alert(safe_main, [upgrade, routine], protocol="PENDLE")
        self.assertEqual(mock_send.call_count, 1)
        self.assertIn("#️⃣ Nonce: 11", mock_send.call_args.args[0])

    # --- per-safe isolation ------------------------------------------------------

    def test_one_failing_safe_alerts_and_the_rest_still_run(self):
        safe_main = self._import_safe_main()
        safes = [["PENDLE", "mainnet", "0x1"], ["LIDO", "mainnet", "0x2"]]

        with (
            patch.object(safe_main, "check_llm_credits"),
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", safes),
            patch.object(safe_main, "run_for_network", side_effect=[TypeError("boom"), None]) as mock_run,
            patch.object(safe_main, "send_error_message") as mock_error,
        ):
            safe_main.main()

        self.assertEqual(
            [c.args for c in mock_run.call_args_list], [("mainnet", "0x1", "PENDLE"), ("mainnet", "0x2", "LIDO")]
        )
        mock_error.assert_called_once()
        message, label = mock_error.call_args.args
        self.assertEqual(label, "yearn")
        self.assertEqual(mock_error.call_args.kwargs["alert_protocol"], "yearn-internal")
        self.assertIn("0x1", message)
        self.assertIn("TypeError: boom", message)

    def test_failed_error_alert_does_not_skip_the_remaining_safes(self):
        from utils.telegram import TelegramError

        safe_main = self._import_safe_main()
        safes = [["PENDLE", "mainnet", "0x1"], ["LIDO", "mainnet", "0x2"]]

        with (
            patch.object(safe_main, "check_llm_credits"),
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", safes),
            patch.object(safe_main, "run_for_network", side_effect=[TypeError("boom"), None]) as mock_run,
            patch.object(safe_main, "send_error_message", side_effect=TelegramError("chat not found")) as mock_error,
        ):
            safe_main.main()

        self.assertEqual(mock_run.call_count, 2)
        mock_error.assert_called_once()

    def test_rejected_key_stops_the_run(self):
        safe_main = self._import_safe_main()
        safes = [["LIDO", "mainnet", "0x1"], ["LIDO", "mainnet", "0x2"]]

        with (
            patch.object(safe_main, "check_llm_credits"),
            patch.object(safe_main, "ALL_SAFE_ADDRESSES", safes),
            patch.object(safe_main, "run_for_network", side_effect=safe_main.SafeApiAuthError("bad key")) as mock_run,
            patch.object(safe_main, "send_error_message") as mock_error,
            self.assertRaises(safe_main.SafeApiAuthError),
        ):
            safe_main.main()

        mock_run.assert_called_once()
        mock_error.assert_not_called()
