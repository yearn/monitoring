"""Tests for 3Jane account-level context: LCC bounces and USD3 supply-cap exemptions."""

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm import threejane_account_context as account_context
from utils.llm.threejane_account_context import (
    UNLIMITED,
    USD3_ADDRESS,
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


def _exemption(**overrides: object) -> SupplyCapExemptContext:
    context = SupplyCapExemptContext(
        usd3_address=USD3_ADDRESS,
        account_address=OTHER,
        proposed_exempt=True,
        current_exempt=False,
        ring_fence_conduit=False,
        account_is_contract=False,
        asset=USDC,
        min_deposit_raw=1_000_000_000,
        supply_cap_raw=80_000_000_000_000,
        total_assets_raw=83_483_415_520_897,
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


def _client(batch_results: list, code: bytes = b"") -> MagicMock:
    client = MagicMock()
    client.execute_batch.return_value = batch_results
    client.eth.get_code.return_value = code
    return client


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

    def test_labels_name_the_assets_by_role(self) -> None:
        labels = _bounce().labels
        self.assertEqual(labels[VAULT], "LCCVault")
        self.assertEqual(labels[USDC.address], "USDC (LCC funding asset)")
        self.assertEqual(labels[WAEUSDC.address], "waEthUSDC (LCC margin asset)")


class TestSupplyCapExempt(unittest.TestCase):
    def test_flag_change_and_noop(self) -> None:
        self.assertEqual(_exemption().flag_line(), "supplyCapExempt: false → true.")
        self.assertIn("already true", _exemption(current_exempt=True).flag_line())

    def test_semantics_name_every_bypass_and_what_still_applies(self) -> None:
        line = _exemption().semantics_line()
        self.assertIn("supply-cap headroom check", line)
        self.assertIn("first-deposit minimum of 1,000 USDC", line)
        self.assertIn("waUSDC is paused", line)
        self.assertIn("msg.sender == receiver", line)
        self.assertIn("outstanding borrow shares", line)
        self.assertIn("supply cap of 0", line)

    def test_eoa_without_ring_fence_is_called_out(self) -> None:
        line = _exemption().pairing_line()
        self.assertIn("an EOA (no contract code)", line)
        self.assertIn("receive no ring-fence credit", line)

    def test_paired_conduit_is_not_flagged(self) -> None:
        line = _exemption(ring_fence_conduit=True, account_is_contract=True).pairing_line()
        self.assertIn("a contract", line)
        self.assertNotIn("no ring-fence credit", line)

    def test_supply_line_above_cap(self) -> None:
        self.assertIn("above the cap by 3,483,415.520897 USDC", _exemption().supply_line())

    def test_supply_line_with_headroom(self) -> None:
        line = _exemption(total_assets_raw=70_000_000_000_000).supply_line()
        self.assertIn("10,000,000 USDC of headroom", line)

    def test_supply_line_unlimited_cap(self) -> None:
        self.assertIn("unlimited", _exemption(supply_cap_raw=UNLIMITED).supply_line())

    def test_prompt_and_report(self) -> None:
        prompt = format_account_prompt(_exemption())
        report = format_account_report(_exemption(), 1, {USD3_ADDRESS: "USD3"})
        self.assertIn(f"USD3.setSupplyCapExempt on {USD3_ADDRESS} for account {OTHER}", prompt)
        self.assertIn("waUSDC is paused", report)
        self.assertIn(f"https://etherscan.io/address/{OTHER}", report)


class TestReaders(unittest.TestCase):
    def test_lcc_bounce_reads_account_and_units(self) -> None:
        account = (MARGIN, COMMITMENT, 0, 0, 0, 0, 0, 0, 0, False, 0, False, False, 0)
        asset_config = (WAEUSDC.address, USDC.address, USD3_ADDRESS, OTHER, OTHER, OTHER)
        risk_config = (10**13, 10**12, 2000, 1_585_000_000, 10000, 0)
        totals = (575_342_153_334, 9_084_283_497_207, 0, 0)
        client = _client([asset_config, risk_config, totals, account])
        units = {USDC.address: USDC, WAEUSDC.address: WAEUSDC}
        with (
            patch.object(account_context, "exposes", return_value=True),
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", side_effect=lambda _chain, address: units[address]),
        ):
            contexts = resolve_account_contexts(1, VAULT, [_bounce_call()])

        self.assertEqual(contexts, [_bounce()])

    def test_exit_claimed_is_not_in_progress(self) -> None:
        account = (MARGIN, COMMITMENT, 0, 0, 0, 0, 0, 0, 0, True, 0, True, False, 0)
        asset_config = (WAEUSDC.address, USDC.address, USD3_ADDRESS, OTHER, OTHER, OTHER)
        client = _client([asset_config, (0, 0, 0, 0, 0, 0), (0, 0, 0, 0), account])
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

    def test_supply_cap_exemptions_read_flags_per_account(self) -> None:
        client = _client(
            [USDC.address, 83_483_415_520_897, 1_000_000_000, 80_000_000_000_000, False, False, True, True],
            code=b"",
        )
        calls = [_exempt_call(OTHER), _exempt_call(USER)]
        with (
            patch.object(account_context.ChainManager, "get_client", return_value=client),
            patch.object(account_context, "fetch_token_unit", return_value=USDC),
        ):
            contexts = resolve_account_contexts(1, USD3_ADDRESS, calls)

        self.assertEqual(contexts[0], _exemption())
        self.assertEqual(contexts[1], _exemption(account_address=USER, current_exempt=True, ring_fence_conduit=True))

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
