"""Tests for 3Jane account-level context: LCC bounces and USD3 supply-cap exemptions."""

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm import threejane_account_context as account_context
from utils.llm.threejane_account_context import (
    UNLIMITED,
    USD3_ADDRESS,
    ExemptAccount,
    ExemptionChange,
    LCCBounceContext,
    SupplyCapExemptContext,
    TokenUnit,
    format_account_prompt,
    format_account_report,
    resolve_account_contexts,
)

VAULT = "0x8350ba7c69aeADD74b891EFc53F52a0f592aD796"
USER = "0x66C0d9152209B51977047b9DC3b0B5bf2339b67C"
OTHER = "0x445d1098c0ABC313dAcc04558855c86A9e492210"
USDC = TokenUnit("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", 6)
WAEUSDC = TokenUnit("0xD4fa2D31b7968E448877f69A96DE69f5de8cD23E", "waEthUSDC", 6)
USD3 = TokenUnit(USD3_ADDRESS, "USD3", 6)
# Accounts from the 44-call revocation that called three delegated EOAs "contracts".
DELEGATED = "0x48cadD085e157A3F1C1C4BD6F9Bc0D6EF6215ADB"
DELEGATE = "0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B"
SAFE = "0xcaB6b18D178502D6e18609a5f7228011CbF34F56"

# The live account behind the alert that summarized this bounce as dust at 1e18.
COMMITMENT = 499_999_999_986
MARGIN = 31_685_345_344


def _bounce(**overrides: object) -> LCCBounceContext:
    context = LCCBounceContext(
        vault_address=VAULT,
        user_address=USER,
        commitment_raw=COMMITMENT,
        funding=USDC,
        margin=WAEUSDC,
        active_commitment_raw=COMMITMENT,
        active_margin_raw=MARGIN,
        pending_commitment_raw=0,
        pending_margin_raw=0,
        exit_in_progress=False,
        min_deposit_margin_raw=1_585_000_000,
        vault_active_commitment_raw=9_084_283_497_207,
    )
    return replace(context, **overrides)


def _account(**overrides: object) -> ExemptAccount:
    account = ExemptAccount(
        address=OTHER,
        proposed_exempt=True,
        current_exempt=False,
        ring_fence_conduit=False,
        is_contract=False,
        delegate=None,
        usd3_balance_raw=0,
    )
    return replace(account, **overrides)


def _exemption(*accounts: ExemptAccount, **overrides: object) -> SupplyCapExemptContext:
    context = SupplyCapExemptContext(
        usd3_address=USD3_ADDRESS,
        asset=USDC,
        share=USD3,
        min_deposit_raw=1_000_000_000,
        supply_cap_raw=80_000_000_000_000,
        total_assets_raw=83_483_415_520_897,
        accounts=accounts or (_account(),),
    )
    return replace(context, **overrides)


def _bounce_call(user: str = USER, commitment: int = COMMITMENT) -> DecodedCall:
    return DecodedCall(
        "bounceCommitment", "bounceCommitment(address,uint256)", [("address", user), ("uint256", commitment)]
    )


def _exempt_call(account: str = OTHER, exempt: bool = True) -> DecodedCall:
    return DecodedCall(
        "setSupplyCapExempt", "setSupplyCapExempt(address,bool)", [("address", account), ("bool", exempt)]
    )


# The live alert: 0x6477… was exempted in the 25/09 batch, revoked with 43 others on 30/09, then re-granted.
REGRANTED = "0x6477841947E73B025d69bc9a7dA7cD5890E7Cf0E"
REVOKE_TX = "0xd7f79155b132a9788d63bb015db3ca7e43600d4cf21930756a3bfe14335bc8fd"


def _client(batch_results: list, code: bytes = b"", logs: list | None = None) -> MagicMock:
    client = MagicMock()
    client.execute_batch.return_value = batch_results
    client.eth.get_code.return_value = code
    client.eth.get_logs.return_value = logs or []
    return client


# ILCCVault.SyncState with no pending auction, and an EpochState builder (callOpened 0, slashFinalized 8).
_IDLE_SYNC = (0, 0, 0, 0)


def _epoch_state(call_opened: bool = False, slash_finalized: bool = False) -> tuple:
    return (call_opened, 0, 0, 0, 0, 0, 0, 0, slash_finalized, False, 0, 0, 0)


def _exempt_log(account: str, exempt: bool, block: int, tx_hash: str) -> dict:
    return {
        "topics": [bytes.fromhex(account_context.EXEMPT_UPDATED_TOPIC[2:]), bytes(12) + bytes.fromhex(account[2:])],
        "data": int(exempt).to_bytes(32, "big"),
        "blockNumber": block,
        "transactionHash": bytes.fromhex(tx_hash[2:]),
    }


class TestTokenUnit(unittest.TestCase):
    def test_amount_is_exact_and_grouped(self) -> None:
        self.assertEqual(USDC.amount(COMMITMENT), "499,999.999986 USDC")
        self.assertEqual(USDC.amount(80_000_000_000_000), "80,000,000 USDC")
        self.assertEqual(USDC.amount(0), "0 USDC")


class TestLCCBounce(unittest.TestCase):
    def test_full_bounce_returns_all_margin(self) -> None:
        context = _bounce()
        self.assertTrue(context.is_full)
        self.assertEqual(context.margin_returned_raw, MARGIN)
        self.assertEqual(context.blocker(), "")
        self.assertIn("FULL bounce", context.effect_line())
        self.assertIn("returns all 31,685.345344 waEthUSDC", context.effect_line())

    def test_partial_bounce_is_pro_rata_rounded_down(self) -> None:
        context = _bounce(commitment_raw=100_000_000_000)  # 100k of ~500k
        expected = MARGIN * 100_000_000_000 // COMMITMENT
        self.assertEqual(context.margin_returned_raw, expected)
        self.assertIn("PARTIAL bounce: removes 20.0%", context.effect_line())
        self.assertIn("leaving 399,999.999986 USDC committed", context.effect_line())

    def test_commitment_above_active_would_revert(self) -> None:
        context = _bounce(commitment_raw=COMMITMENT + 1)
        self.assertIn("InvalidAmount", context.blocker())
        self.assertIn("would REVERT", context.effect_line())
        self.assertEqual(context.vault_share_line(), "")

    def test_zero_commitment_would_revert(self) -> None:
        self.assertIn("InvalidAmount", _bounce(commitment_raw=0).blocker())

    def test_pending_deposit_would_revert(self) -> None:
        self.assertIn("PendingDepositExists", _bounce(pending_margin_raw=1).blocker())

    def test_exit_in_progress_would_revert(self) -> None:
        self.assertIn("ExitInProgress", _bounce(exit_in_progress=True).blocker())

    def test_partial_bounce_below_min_margin_would_revert(self) -> None:
        # Leaves ~317 waEthUSDC of margin, below the 1,585 minimum.
        context = _bounce(commitment_raw=COMMITMENT - 5_000_000_000)
        self.assertIn("below the vault's minDepositAssets of 1,585 waEthUSDC", context.blocker())

    def test_vault_share_line(self) -> None:
        line = _bounce().vault_share_line()
        self.assertIn("Vault-wide active commitment: 9,084,283.497207 USDC", line)
        self.assertIn("withdraws 5.5% of it", line)

    def test_prompt_states_units_and_raw_value(self) -> None:
        prompt = format_account_prompt(_bounce())
        self.assertIn("`commitment` is in the funding asset USDC (6 decimals", prompt)
        self.assertIn("margin is in waEthUSDC (6 decimals", prompt)
        self.assertIn("Commitment to remove: 499,999.999986 USDC (raw 499999999986)", prompt)
        self.assertIn("State is read at scheduling time", prompt)

    def test_report_links_vault_user_and_assets(self) -> None:
        report = format_account_report(_bounce(), 1, {VAULT: "LCCVault"})
        for address in (VAULT, USER, USDC.address, WAEUSDC.address):
            self.assertIn(f"https://etherscan.io/address/{address}", report)
        self.assertIn("`499,999.999986 USDC`", report)

    def test_phase_blockers_come_first(self) -> None:
        """bounceCommitment reverts InvalidPhase during a pending auction or an unsettled call."""
        self.assertIn("shortfall auction is pending", _bounce(auction_pending=True).blocker())
        unsettled = _bounce(call_unsettled=True)
        self.assertIn("capital call is open and its slash is not finalized", unsettled.blocker())
        self.assertIn("would REVERT", unsettled.effect_line())
        self.assertEqual(unsettled.vault_share_line(), "")

    def test_labels_name_the_assets_by_role(self) -> None:
        labels = _bounce().labels
        self.assertEqual(labels[VAULT], "LCCVault")
        self.assertEqual(labels[USDC.address], "USDC (LCC funding asset)")
        self.assertEqual(labels[WAEUSDC.address], "waEthUSDC (LCC margin asset)")


class TestExemptAccount(unittest.TestCase):
    def test_kind_separates_delegated_eoas_from_contracts(self) -> None:
        self.assertEqual(_account().kind(), "EOA")
        self.assertEqual(_account(delegate=DELEGATE).kind(), f"EOA with EIP-7702 delegation to {DELEGATE}")
        self.assertEqual(_account(is_contract=True).kind(), "contract")

    def test_change_and_noop(self) -> None:
        self.assertEqual(_account().change(), "false → true")
        self.assertEqual(_account(proposed_exempt=False, current_exempt=True).change(), "true → false")
        self.assertEqual(_account(current_exempt=True).change(), "already true (no change)")

    def test_grant_without_ring_fence_is_not_an_issue(self) -> None:
        """Every exempt EOA is a direct depositor without the conduit flag; the ⚠️ framed that as a deviation."""
        self.assertEqual(_account().pairing_issue(), "")
        self.assertEqual(_account(ring_fence_conduit=True).pairing_issue(), "")

    def test_history_names_a_regrant(self) -> None:
        account = _account(last_change=ExemptionChange(26091398, False, REVOKE_TX))
        self.assertEqual(account.history(), f"re-grants an exemption revoked at block 26091398 (tx {REVOKE_TX})")

    def test_history_states_the_last_change_otherwise(self) -> None:
        revoke = _account(proposed_exempt=False, current_exempt=True, last_change=ExemptionChange(1, True, "0xab"))
        self.assertEqual(revoke.history(), "before this transaction, last set to true at block 1 (tx 0xab)")
        self.assertEqual(_account().history(), "no earlier exemption change on record")

    def test_revoke_leaving_ring_fence_set_is_an_issue(self) -> None:
        issue = _account(proposed_exempt=False, current_exempt=True, ring_fence_conduit=True).pairing_issue()
        self.assertIn("ringFenceConduit stays true", issue)
        self.assertIn("revoke both flags together", issue)

    def test_revoke_without_ring_fence_is_clean(self) -> None:
        self.assertEqual(_account(proposed_exempt=False, current_exempt=True).pairing_issue(), "")


class TestSupplyCapExempt(unittest.TestCase):
    def _revocation(self) -> SupplyCapExemptContext:
        revoke = {"proposed_exempt": False, "current_exempt": True}
        return _exemption(
            _account(address=OTHER, usd3_balance_raw=1_271_316_913_410, **revoke),
            _account(address=DELEGATED, delegate=DELEGATE, **revoke),
            _account(address=SAFE, is_contract=True, usd3_balance_raw=85_185_325_821, **revoke),
            _account(address=USER, **revoke),
            total_assets_raw=100_497_447_211_275,
            supply_cap_raw=100_000_000_000_000,
        )

    def test_overview_counts_directions_and_kinds(self) -> None:
        line = self._revocation().overview_line()
        self.assertIn(
            "4 setSupplyCapExempt call(s) on USD3 across 4 account(s): 0 grant (false → true), 4 revoke (true → false)",
            line,
        )
        self.assertIn("2 EOA, 1 EOA with an EIP-7702 delegation", line)
        self.assertIn("not a deployed contract), 1 contract", line)
        self.assertIn("2 hold USD3, 1,356,502.239231 USD3 in total", line)

    def test_overview_counts_a_repeated_account_once(self) -> None:
        """Calls count per call; type, holders and the USD3 total count each account once."""
        grant = _account(usd3_balance_raw=1_271_316_913_410)
        revoke = replace(grant, proposed_exempt=False, current_exempt=True)
        line = _exemption(grant, revoke, _account(address=SAFE, is_contract=True)).overview_line()
        self.assertIn("3 setSupplyCapExempt call(s) on USD3 across 2 account(s)", line)
        self.assertIn("2 grant (false → true), 1 revoke (true → false), 0 no-op", line)
        self.assertIn("Accounts: 1 EOA, 0 EOA with an EIP-7702 delegation", line)
        self.assertIn("not a deployed contract), 1 contract", line)
        self.assertIn("1 hold USD3, 1,271,316.91341 USD3 in total", line)

    def test_semantics_name_every_bypass_and_what_still_applies(self) -> None:
        line = _exemption().semantics_line()
        self.assertIn("supply-cap headroom check", line)
        self.assertIn("first-deposit minimum of 1,000 USDC", line)
        self.assertIn("waUSDC is paused", line)
        self.assertIn("msg.sender == receiver", line)
        self.assertIn("outstanding borrow shares", line)
        self.assertIn("supply cap of 0", line)
        self.assertIn("does not touch existing balances or withdrawals", line)

    def test_pairing_line_counts_out_of_step_accounts(self) -> None:
        conduit_revoke = _account(proposed_exempt=False, current_exempt=True, ring_fence_conduit=True)
        self.assertIn("1 account(s) end up out of step", _exemption(conduit_revoke).pairing_line())
        self.assertIn("No call in this batch", self._revocation().pairing_line())
        self.assertIn("No call in this batch", _exemption().pairing_line())
        self.assertIn("only matters for LCC capital-call funding", _exemption().pairing_line())

    def test_purpose_line_states_the_documented_intent(self) -> None:
        """A report once called a wallet exemption routine; the contract reserves it for protocol receivers."""
        line = _exemption().purpose_line()
        self.assertIn("controls overall protocol size and risk exposure", line)
        self.assertIn("protocol-controlled deposit receivers", line)
        self.assertIn("per-wallet right to grow USD3 past the cap", line)
        self.assertIn("adds no credit exposure", line)
        self.assertEqual(format_account_prompt(_exemption()).count("Why it matters"), 1)
        self.assertEqual(format_account_report(_exemption(), 1, {}).count("Why it matters"), 1)

    def test_supply_line_above_cap_blocks_non_exempt(self) -> None:
        line = _exemption().supply_line()
        self.assertIn("above the cap by 3,483,415.520897 USDC", line)
        self.assertIn("availableDepositLimit is 0 for every non-exempt receiver", line)

    def test_supply_line_exactly_at_cap(self) -> None:
        line = _exemption(total_assets_raw=80_000_000_000_000).supply_line()
        self.assertIn("exactly at the cap", line)
        self.assertNotIn("above the cap by 0", line)

    def test_supply_line_with_headroom(self) -> None:
        line = _exemption(total_assets_raw=70_000_000_000_000).supply_line()
        self.assertIn("10,000,000 USDC of headroom", line)

    def test_supply_line_unlimited_cap(self) -> None:
        self.assertIn("unlimited", _exemption(supply_cap_raw=UNLIMITED).supply_line())

    def test_prompt_states_shared_facts_once_and_lists_every_account(self) -> None:
        context = self._revocation()
        prompt = format_account_prompt(context)
        self.assertEqual(prompt.count("What the flag does"), 1)
        self.assertEqual(prompt.count("USD3 now:"), 1)
        self.assertIn(f"- {DELEGATED} (EOA with EIP-7702 delegation to {DELEGATE}): true → false", prompt)
        self.assertIn(f"- {SAFE} (contract): true → false; ringFenceConduit false; holds 85,185.325821 USD3", prompt)
        self.assertEqual(sum(1 for line in prompt.splitlines() if line.startswith("- 0x")), 4)

    def test_report_tabulates_accounts_with_links(self) -> None:
        report = format_account_report(self._revocation(), 1, {USD3_ADDRESS: "USD3"})
        self.assertEqual(report.count("What the flag does"), 1)
        self.assertIn("| # | Account | Type | supplyCapExempt | ringFenceConduit | USD3 balance |", report)
        self.assertIn(f"EOA, EIP-7702 → [`{DELEGATE}`](https://etherscan.io/address/{DELEGATE})", report)
        for address in (OTHER, DELEGATED, SAFE, USER):
            self.assertIn(f"https://etherscan.io/address/{address}", report)
        self.assertIn("`1,271,316.91341 USD3`", report)

    def test_report_flags_out_of_step_accounts(self) -> None:
        conduit_revoke = _account(proposed_exempt=False, current_exempt=True, ring_fence_conduit=True)
        self.assertIn(
            "⚠️ exemption revoked while ringFenceConduit stays true",
            format_account_report(_exemption(conduit_revoke), 1, {}),
        )
        self.assertNotIn("⚠️", format_account_report(_exemption(), 1, {}))

    def test_history_appears_only_when_read(self) -> None:
        account = _account(address=REGRANTED, last_change=ExemptionChange(26091398, False, REVOKE_TX))
        context = _exemption(account, history_read=True, exempt_before=5, exempt_conduits_before=2)
        prompt = format_account_prompt(context)
        self.assertIn(
            "Exempt before this transaction (replayed from SupplyCapExemptUpdated): 5 account(s), 2 of them", prompt
        )
        self.assertIn(f"re-grants an exemption revoked at block 26091398 (tx {REVOKE_TX})", prompt)
        report = format_account_report(context, 1, {})
        self.assertIn("| USD3 balance | Last change |", report)
        self.assertIn(f"set false at [block 26091398](https://etherscan.io/tx/{REVOKE_TX})", report)

        unread = _exemption(account)
        self.assertNotIn("re-grants", format_account_prompt(unread))
        self.assertNotIn("Last change", format_account_report(unread, 1, {}))

    def test_addresses_include_every_account(self) -> None:
        self.assertEqual(self._revocation().addresses, [USD3_ADDRESS, OTHER, DELEGATED, SAFE, USER])


class TestReaders(unittest.TestCase):
    def test_lcc_bounce_reads_account_and_units(self) -> None:
        account = (MARGIN, COMMITMENT, 0, 0, 0, 0, 0, 0, 0, False, 0, False, False, 0)
        asset_config = (WAEUSDC.address, USDC.address, USD3_ADDRESS, OTHER, OTHER, OTHER)
        risk_config = (10**13, 10**12, 2000, 1_585_000_000, 10000, 0)
        totals = (575_342_153_334, 9_084_283_497_207, 0, 0)
        client = _client([asset_config, risk_config, totals, _IDLE_SYNC, 42, account])
        client.get_contract.return_value.functions.getEpochState.return_value.call.return_value = _epoch_state()
        units = {USDC.address: USDC, WAEUSDC.address: WAEUSDC}
        with (
            patch.object(account_context, "exposes", return_value=True),
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", side_effect=lambda _chain, address: units[address]),
        ):
            contexts = resolve_account_contexts(1, VAULT, [_bounce_call()])

        self.assertEqual(contexts, [_bounce()])

    def test_lcc_bounce_reads_the_vault_phase(self) -> None:
        account = (MARGIN, COMMITMENT, 0, 0, 0, 0, 0, 0, 0, False, 0, False, False, 0)
        asset_config = (WAEUSDC.address, USDC.address, USD3_ADDRESS, OTHER, OTHER, OTHER)
        client = _client([asset_config, (0, 0, 0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 43), 42, account])
        epoch_call = client.get_contract.return_value.functions.getEpochState
        epoch_call.return_value.call.return_value = _epoch_state(call_opened=True)
        units = {USDC.address: USDC, WAEUSDC.address: WAEUSDC}
        with (
            patch.object(account_context, "exposes", return_value=True),
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", side_effect=lambda _chain, address: units[address]),
        ):
            (context,) = resolve_account_contexts(1, VAULT, [_bounce_call()])
        self.assertTrue(context.auction_pending)
        self.assertTrue(context.call_unsettled)
        epoch_call.assert_called_with(42)

    def test_exit_claimed_is_not_in_progress(self) -> None:
        account = (MARGIN, COMMITMENT, 0, 0, 0, 0, 0, 0, 0, True, 0, True, False, 0)
        asset_config = (WAEUSDC.address, USDC.address, USD3_ADDRESS, OTHER, OTHER, OTHER)
        client = _client([asset_config, (0, 0, 0, 0, 0, 0), (0, 0, 0, 0), _IDLE_SYNC, 42, account])
        client.get_contract.return_value.functions.getEpochState.return_value.call.return_value = _epoch_state()
        units = {USDC.address: USDC, WAEUSDC.address: WAEUSDC}
        with (
            patch.object(account_context, "exposes", return_value=True),
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", side_effect=lambda _chain, address: units[address]),
        ):
            (context,) = resolve_account_contexts(1, VAULT, [_bounce_call()])
        self.assertFalse(context.exit_in_progress)

    def test_non_lcc_target_is_not_read(self) -> None:
        with (
            patch.object(account_context, "exposes", return_value=False),
            patch.object(account_context.ChainManager, "get_client") as get_client,
        ):
            self.assertEqual(resolve_account_contexts(1, VAULT, [_bounce_call()]), [])
        get_client.assert_not_called()

    def test_supply_cap_exemptions_read_flags_code_and_balance_in_one_batch(self) -> None:
        delegation = bytes.fromhex("ef0100" + DELEGATE[2:].lower())
        safe_code = bytes.fromhex("6080604052" + "00" * 166)
        shared = [USDC.address, 83_483_415_520_897, 1_000_000_000, 80_000_000_000_000, "USD3", 6]
        per_account = [
            *(False, False, 0, b""),
            *(True, True, 5, delegation),
            *(False, False, 7, safe_code),
        ]
        client = _client([*shared, *per_account])
        calls = [_exempt_call(OTHER), _exempt_call(DELEGATED, exempt=False), _exempt_call(SAFE)]
        with (
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", return_value=USDC),
        ):
            (context,) = resolve_account_contexts(1, USD3_ADDRESS, calls)

        self.assertEqual(
            context,
            _exemption(
                _account(),
                _account(
                    address=DELEGATED,
                    proposed_exempt=False,
                    current_exempt=True,
                    ring_fence_conduit=True,
                    delegate=DELEGATE,
                    usd3_balance_raw=5,
                ),
                _account(address=SAFE, is_contract=True, usd3_balance_raw=7),
                history_read=True,
            ),
        )
        # Code is read through the batch, never with a direct per-account RPC.
        self.assertEqual(client.execute_batch.call_count, 1)

    def test_repeated_account_sees_the_previous_call_as_its_before_state(self) -> None:
        """Grant then revoke the same account: the revoke is a real true → false, not a no-op."""
        shared = [USDC.address, 83_483_415_520_897, 1_000_000_000, 80_000_000_000_000, "USD3", 6]
        # Both reads return the pre-batch state: not exempt.
        per_account = [*(False, False, 0, b""), *(False, False, 0, b"")]
        client = _client([*shared, *per_account])
        with (
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", return_value=USDC),
        ):
            (context,) = resolve_account_contexts(1, USD3_ADDRESS, [_exempt_call(OTHER), _exempt_call(OTHER, False)])

        grant, revoke = context.accounts
        self.assertEqual(grant.change(), "false → true")
        self.assertEqual(revoke.change(), "true → false")
        self.assertIn("1 grant (false → true), 1 revoke (true → false), 0 no-op", context.overview_line())

    def test_history_marks_the_regrant_and_counts_exempt_conduits(self) -> None:
        shared = [USDC.address, 100_435_135_055_453, 1_000_000_000, 100_000_000_000_000, "USD3", 6]
        logs = [
            _exempt_log(VAULT, True, 26057349, "0x01"),
            _exempt_log(REGRANTED, True, 26057378, "0x02"),
            _exempt_log(OTHER, True, 26079019, "0x03"),
            _exempt_log(REGRANTED, False, 26091398, REVOKE_TX),
        ]
        # The account's own reads, then ringFenceConduit for each account exempt before the call: VAULT, OTHER.
        client = _client([*shared, *(False, False, 86_026_259_570, b""), True, False], logs=logs)
        with (
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", return_value=USDC),
        ):
            (context,) = resolve_account_contexts(1, USD3_ADDRESS, [_exempt_call(REGRANTED)])

        self.assertEqual(context.accounts[0].last_change, ExemptionChange(26091398, False, REVOKE_TX))
        self.assertEqual((context.exempt_before, context.exempt_conduits_before), (2, 1))
        self.assertIn("re-grants an exemption revoked at block 26091398", format_account_prompt(context))

    def test_unreadable_history_keeps_the_context(self) -> None:
        shared = [USDC.address, 1, 1_000_000_000, 100_000_000_000_000, "USD3", 6]
        client = _client([*shared, *(False, False, 0, b"")])
        client.eth.get_logs.side_effect = ValueError("query exceeds max block range")
        with (
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", return_value=USDC),
        ):
            (context,) = resolve_account_contexts(1, USD3_ADDRESS, [_exempt_call(REGRANTED)])
        self.assertFalse(context.history_read)
        self.assertIsNone(context.accounts[0].last_change)

    def test_exemption_on_another_target_is_ignored(self) -> None:
        with patch.object(account_context.ChainManager, "get_client") as get_client:
            self.assertEqual(resolve_account_contexts(1, VAULT, [_exempt_call()]), [])
        get_client.assert_not_called()

    def test_malformed_calls_are_skipped(self) -> None:
        bad_bounce = DecodedCall("bounceCommitment", "bounceCommitment(uint256)", [("uint256", 1)])
        bad_exempt = DecodedCall("setSupplyCapExempt", "setSupplyCapExempt(address)", [("address", OTHER)])
        with patch.object(account_context.ChainManager, "get_client") as get_client:
            self.assertEqual(resolve_account_contexts(1, VAULT, [bad_bounce]), [])
            self.assertEqual(resolve_account_contexts(1, USD3_ADDRESS, [bad_exempt]), [])
        get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
