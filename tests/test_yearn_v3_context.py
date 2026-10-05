"""Tests for the Yearn V3 vault protocol-context adapter."""

import unittest
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import yearn_v3_context
from utils.llm.yearn_v3_context import (
    MAX_UINT256,
    StrategyState,
    YearnV3VaultContext,
    format_yearn_v3_prompt,
    format_yearn_v3_report,
    resolve_yearn_v3_context,
)

VAULT = "0xAc37729B76db6438CE62042AE1270ee574CA7571"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
PEER = "0x13f6Cb609959a43c3bE29407766A683b42e26D28"
LOOPER = "0x68A14629cb07c74259f481382fE8b6cFD8970121"
E18 = 10**18


def _strategy(address: str, name: str, current: int, max_debt: int, **kwargs: Any) -> StrategyState:
    return StrategyState(
        address=address,
        name=name,
        activation=kwargs.pop("activation", 1),
        current_debt=current,
        max_debt=max_debt,
        **kwargs,
    )


def _call(name: str, types: list[str], values: list[object]) -> DecodedCall:
    return DecodedCall(name, f"{name}({','.join(types)})", list(zip(types, values)))


def _context(calls: list[DecodedCall], use_default_queue: bool | None = True) -> YearnV3VaultContext:
    looper = _strategy(
        LOOPER,
        "wstETH/WETH Spark Looper",
        0,
        0,
        activation=0,
        asset=WETH,
        total_assets=1_034 * E18,
    )
    return YearnV3VaultContext(
        vault_address=VAULT,
        name="WETH-2 yVault",
        symbol="yvWETH-2",
        api_version="3.0.2",
        asset_address=WETH,
        asset_symbol="WETH",
        asset_decimals=18,
        total_assets=1_163_810_000_000_000_000_000,
        total_debt=1_163_810_000_000_000_000_000,
        total_idle=0,
        is_shutdown=False,
        deposit_limit=10_000 * E18,
        minimum_total_idle=0,
        use_default_queue=use_default_queue,
        default_queue=(_strategy(PEER, "Spark Accumulator", 921 * E18, 10_000 * E18),),
        other_strategies=(looper,),
        calls=tuple(calls),
    )


ADD_LOOPER = _call("add_strategy", ["address", "bool"], [LOOPER, False])
CAP_LOOPER = _call("update_max_debt_for_strategy", ["address", "uint256"], [LOOPER, 10_000 * E18])


class TestProposalLines(unittest.TestCase):
    """Proposed values are stated in the vault asset and against current state."""

    def test_add_strategy_out_of_queue(self) -> None:
        (line,) = _context([ADD_LOOPER]).proposal_lines()
        self.assertIn("add_strategy(wstETH/WETH Spark Looper): not yet active", line)
        self.assertIn("strategy asset matches the vault asset WETH", line)
        self.assertIn("add_to_queue=False: stays OUT of the default queue", line)
        self.assertIn("use_default_queue is True, so no withdrawal can pull from it", line)
        self.assertIn("strategy totalAssets 1,034 WETH", line)
        (line,) = _context([ADD_LOOPER], use_default_queue=False).proposal_lines()
        self.assertIn("unless a withdrawer passes a custom queue", line)

    def test_add_strategy_defaults_to_queue(self) -> None:
        # The one-argument overload leaves add_to_queue at its default of True.
        (line,) = _context([_call("add_strategy", ["address"], [LOOPER])]).proposal_lines()
        self.assertIn("add_to_queue=True: appended to the default queue (holds 1/10 before this call)", line)

    def test_add_strategy_with_mismatched_asset_reverts(self) -> None:
        context = _context([ADD_LOOPER])
        other = _strategy(LOOPER, "Looper", 0, 0, activation=0, asset=PEER)
        context = replace(context, other_strategies=(other,))
        (line,) = context.proposal_lines()
        self.assertIn("DOES NOT match — the call reverts", line)

    def test_max_debt_in_asset_units_relative_to_vault_and_peers(self) -> None:
        _, line = _context([ADD_LOOPER, CAP_LOOPER]).proposal_lines()
        self.assertIn("update_max_debt_for_strategy(wstETH/WETH Spark Looper): 10,000 WETH", line)
        self.assertIn("(added earlier in this batch)", line)
        self.assertIn("8.6× vault totalAssets (1,163.81 WETH)", line)
        self.assertIn("same max_debt as every other default-queue strategy", line)
        self.assertIn("equal to the vault's deposit_limit", line)

    def test_max_debt_on_active_strategy_shows_current_values(self) -> None:
        call = _call("update_max_debt_for_strategy", ["address", "uint256"], [PEER, 500 * E18])
        (line,) = _context([call]).proposal_lines()
        self.assertIn("500 WETH (current max_debt 10,000 WETH, current_debt 921 WETH)", line)
        self.assertIn("43.0% of vault totalAssets", line)

    def test_update_debt_with_no_idle_moves_nothing(self) -> None:
        # The vault is fully deployed (idle 0): raising the target can't deposit anything.
        call = _call("update_debt", ["address", "uint256"], [PEER, 1_000 * E18])
        (line,) = _context([call]).proposal_lines()
        self.assertIn("moves nothing — limited by the vault's available idle", line)
        self.assertIn("current_debt stays 921 WETH", line)

    def test_unlimited_cap(self) -> None:
        call = _call("update_max_debt_for_strategy", ["address", "uint256"], [PEER, MAX_UINT256])
        (line,) = _context([call]).proposal_lines()
        self.assertIn("unlimited (max uint256)", line)
        self.assertIn("no cap", line)

    def test_vault_level_setters(self) -> None:
        calls = [
            _call("set_deposit_limit", ["uint256"], [20_000 * E18]),
            _call("set_default_queue", ["address[]"], [[LOOPER]]),
        ]
        limit, queue = _context(calls).proposal_lines()
        self.assertEqual(limit, "set_deposit_limit: 10,000 WETH → 20,000 WETH.")
        self.assertIn("[Spark Accumulator] → [wstETH/WETH Spark Looper]", queue)
        self.assertIn("removed from the queue: Spark Accumulator", queue)


class TestAmountFormatting(unittest.TestCase):
    def test_truncates_to_two_decimals(self) -> None:
        context = _context([])
        self.assertEqual(context.amount(1_163_819_999_999_999_999_999), "1,163.81 WETH")
        self.assertEqual(context.amount(500 * E18), "500 WETH")
        self.assertEqual(context.amount(1), "<0.01 WETH")
        self.assertEqual(context.amount(0), "0 WETH")


class TestRendering(unittest.TestCase):
    def test_prompt_states_units_as_verified(self) -> None:
        prompt = format_yearn_v3_prompt([_context([ADD_LOOPER, CAP_LOOPER])])
        self.assertIn("WETH-2 yVault (yvWETH-2), API 3.0.2", prompt)
        self.assertIn("denominated in its asset WETH (18 decimals) — verified, do not hedge", prompt)
        self.assertIn("update_debt(strategy, target_debt) is a TARGET, not an amount", prompt)
        self.assertIn("False only permits custom queues — the default queue still applies", prompt)
        self.assertIn(f"1. Spark Accumulator {PEER}: current_debt 921 WETH / max_debt 10,000 WETH", prompt)

    def test_report_links_queue_and_lists_proposals(self) -> None:
        report = format_yearn_v3_report([_context([ADD_LOOPER, CAP_LOOPER])], 1, {})
        self.assertIn(f"https://etherscan.io/address/{VAULT}", report)
        self.assertIn(f"https://etherscan.io/address/{PEER}", report)
        self.assertIn("- **Proposed:**", report)
        self.assertIn("update_max_debt_for_strategy(wstETH/WETH Spark Looper): 10,000 WETH", report)

    def test_labels_name_vault_and_strategies(self) -> None:
        labels = _context([]).labels
        self.assertEqual(labels[VAULT], "WETH-2 yVault (yvWETH-2)")
        self.assertEqual(labels[LOOPER], "wstETH/WETH Spark Looper")


class TestResolve(unittest.TestCase):
    """The adapter keys on call shape, not protocol, and never blocks an alert."""

    @patch.object(yearn_v3_context, "_read_vault_context")
    def test_ignores_unrelated_calls_without_rpc(self, mock_read: MagicMock) -> None:
        calls = [(VAULT, _call("setOwner", ["address"], [PEER]))]
        self.assertEqual(resolve_yearn_v3_context("aave", 1, calls), [])
        mock_read.assert_not_called()

    @patch.object(yearn_v3_context, "_read_vault_context")
    def test_groups_calls_per_vault_for_any_protocol(self, mock_read: MagicMock) -> None:
        sentinel = MagicMock()
        mock_read.return_value = sentinel
        calls = [(VAULT, ADD_LOOPER), (VAULT, CAP_LOOPER)]
        self.assertEqual(resolve_yearn_v3_context("some-curator", 1, calls), [sentinel])
        mock_read.assert_called_once_with(1, VAULT, [ADD_LOOPER, CAP_LOOPER])

    @patch.object(yearn_v3_context, "_read_vault_context", side_effect=RuntimeError("rpc down"))
    def test_read_failure_is_swallowed(self, _mock_read: MagicMock) -> None:
        self.assertEqual(resolve_yearn_v3_context("yearn", 1, [(VAULT, ADD_LOOPER)]), [])


def _client(batches: Sequence[Sequence[object]]) -> MagicMock:
    client = MagicMock()
    client.batch_requests.return_value.__enter__.return_value = MagicMock()
    client.batch_requests.return_value.__exit__.return_value = False
    client.execute_batch.side_effect = batches
    return client


class TestReadVaultContext(unittest.TestCase):
    @patch.object(yearn_v3_context, "ChainManager")
    def test_non_v3_vault_is_skipped(self, mock_cm: MagicMock) -> None:
        # A V2 vault answers apiVersion() with 0.4.x.
        mock_cm.get_client.return_value = _client([["0.4.6", WETH, "yvWETH", "yvWETH", 0, 0, 0, [], False]])
        self.assertIsNone(yearn_v3_context._read_vault_context(1, VAULT, [ADD_LOOPER]))

    @patch.object(yearn_v3_context, "ChainManager")
    def test_non_vault_target_is_skipped(self, mock_cm: MagicMock) -> None:
        client = _client([])
        client.execute_batch.side_effect = ValueError("execution reverted")
        mock_cm.get_client.return_value = client
        self.assertIsNone(yearn_v3_context._read_vault_context(1, VAULT, [ADD_LOOPER]))

    @patch.object(yearn_v3_context, "_read_strategy_details")
    @patch.object(yearn_v3_context, "fetch_erc20_metadata")
    @patch.object(yearn_v3_context, "ChainManager")
    def test_reads_queue_and_named_strategies(
        self, mock_cm: MagicMock, mock_meta: MagicMock, mock_details: MagicMock
    ) -> None:
        identity = ["3.0.2", WETH, "WETH-2 yVault", "yvWETH-2", 1_000 * E18, 1_000 * E18, 0, [PEER], False]
        params = [[1, 1, 900 * E18, 10_000 * E18], [0, 0, 0, 0]]
        client = _client([identity, params])
        contract = client.get_contract.return_value
        contract.functions.deposit_limit.return_value.call.return_value = 10_000 * E18
        contract.functions.minimum_total_idle.return_value.call.return_value = 0
        contract.functions.use_default_queue.return_value.call.return_value = True
        mock_cm.get_client.return_value = client
        names = {WETH: ERC20Metadata("WETH", 18, "Wrapped Ether"), PEER: ERC20Metadata("ysA", 18, "Accumulator")}
        mock_meta.side_effect = lambda _chain, address: names.get(address, ERC20Metadata("ysWETH", 18, "Looper"))
        mock_details.return_value = {"asset": WETH, "total_assets": 5 * E18, "is_vault": False, "sub_strategies": ()}

        context = yearn_v3_context._read_vault_context(1, VAULT, [ADD_LOOPER, CAP_LOOPER])

        assert context is not None
        self.assertEqual(context.asset_symbol, "WETH")
        self.assertEqual([s.name for s in context.default_queue], ["Accumulator"])
        self.assertEqual([s.address for s in context.other_strategies], [LOOPER])
        self.assertFalse(context.other_strategies[0].active)
        self.assertEqual(context.deposit_limit, 10_000 * E18)
        self.assertTrue(context.use_default_queue)
        # Only strategies the calls name get the extra detail reads.
        mock_details.assert_called_once_with(client, 1, LOOPER, VAULT)


# capUSDC (Cap), Safe nonce 252: exit two Morpho strategies, add OndoHolder, fund
# Aave with a 50M target while the vault holds 26.93M.
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
CAP_VAULT = "0x3Ed6aa32c930253fc990dE58fF882B9186cd0072"
STEAK = "0xBAed9839573d349e42DFbF23a8916e5AB9cAf2E3"
GAUNT = "0x8092C20351CF4048B464DF2144Dc8a4DD49ce71D"
AAVE = "0x7D7F72d393F242DA6e22D3b970491C06742984Ff"
ONDO = "0x9939009295eAD3c67259aF3b93C284079ffE931e"
E6 = 10**6
UNLIMITED = MAX_UINT256


def _cap_context(calls: list[DecodedCall]) -> YearnV3VaultContext:
    steak = _strategy(STEAK, "Steakhouse", 26_297_013_480_000, 50_000_000 * E6, max_withdraw=26_297_705 * E6)
    gaunt = _strategy(GAUNT, "Gauntlet", 633_217_170_000, 51_000_000 * E6, max_withdraw=633_234 * E6)
    aave = _strategy(AAVE, "Aave", 0, 0, max_deposit=584_554_640 * E6)
    ondo = _strategy(ONDO, "OndoHolder", 0, 0, activation=0, asset=USDC, max_deposit=UNLIMITED)
    return YearnV3VaultContext(
        vault_address=CAP_VAULT,
        name="cap USDC",
        symbol="capUSDC",
        api_version="3.0.4",
        asset_address=USDC,
        asset_symbol="USDC",
        asset_decimals=6,
        total_assets=26_930_230_650_000,
        total_debt=26_930_230_650_000,
        total_idle=0,
        is_shutdown=False,
        deposit_limit=UNLIMITED,
        minimum_total_idle=0,
        use_default_queue=False,
        default_queue=(steak, gaunt, aave),
        other_strategies=(ondo,),
        calls=tuple(calls),
    )


def _debt(strategy: str, amount: int) -> DecodedCall:
    return _call("update_debt", ["address", "uint256"], [strategy, amount])


def _max(strategy: str, amount: int) -> DecodedCall:
    return _call("update_max_debt_for_strategy", ["address", "uint256"], [strategy, amount])


class TestBatchDebtAccounting(unittest.TestCase):
    """update_debt is a target: amounts come from the running batch state, capped like the vault."""

    CAP_BATCH = [
        _call("add_strategy", ["address"], [ONDO]),
        _max(STEAK, 0),
        _debt(STEAK, 0),
        _max(GAUNT, 0),
        _debt(GAUNT, 0),
        _max(AAVE, 50_000_000 * E6),
        _max(ONDO, 15_000_000 * E6),
        _debt(ONDO, 1_000 * E6),
        _debt(AAVE, 50_000_000 * E6),
    ]

    def test_cap_batch_allocates_what_the_vault_holds_not_the_target(self) -> None:
        lines = _cap_context(self.CAP_BATCH).proposal_lines()
        steak, gaunt, ondo, aave = (line for line in lines if line.startswith("update_debt("))
        self.assertIn("withdraws 26,297,013.48 USDC", steak)
        self.assertIn("vault idle 0 USDC → 26,297,013.48 USDC", steak)
        self.assertIn("vault idle 26,297,013.48 USDC → 26,930,230.65 USDC", gaunt)
        self.assertIn("deposits 1,000 USDC;", ondo)
        self.assertIn("target_debt 50,000,000 USDC: MOVES FUNDS NOW — deposits 26,929,230.65 USDC", aave)
        self.assertIn("limited by the vault's available idle", aave)
        self.assertIn("vault idle 26,929,230.65 USDC → 0 USDC", aave)
        self.assertNotIn("deposits 50,000,000", "\n".join(lines))
        aave_cap = next(line for line in lines if line.startswith("update_max_debt_for_strategy(Aave)"))
        self.assertIn("a ceiling above the vault's size", aave_cap)

    def test_max_debt_line_uses_running_debt(self) -> None:
        # After the withdrawal, a later cap change sees current_debt 0, not the pre-batch debt.
        lines = _cap_context([_debt(STEAK, 0), _max(STEAK, 1)]).proposal_lines()
        self.assertIn("current max_debt 50,000,000 USDC, current_debt 0 USDC", lines[1])

    def test_withdrawal_limited_by_strategy_liquidity(self) -> None:
        context = _cap_context([_debt(GAUNT, 0)])
        gaunt = _strategy(GAUNT, "Gauntlet", 633_217 * E6, 51_000_000 * E6, max_withdraw=100_000 * E6)
        context = replace(context, default_queue=(context.default_queue[0], gaunt))
        (line,) = context.proposal_lines()
        self.assertIn("withdraws 100,000 USDC, limited by what the strategy can redeem now", line)

    def test_deposit_limited_by_max_debt(self) -> None:
        (line,) = _cap_context([_debt(AAVE, 1_000 * E6)]).proposal_lines()
        self.assertIn("moves nothing — limited by the strategy's max_debt", line)

    def test_equal_target_reverts(self) -> None:
        (line,) = _cap_context([_debt(AAVE, 0)]).proposal_lines()
        self.assertIn("REVERTS — new debt equals current debt", line)

    def test_force_revoke_after_withdrawal_writes_off_the_remainder(self) -> None:
        lines = _cap_context([_debt(GAUNT, 0), _call("force_revoke_strategy", ["address"], [GAUNT])]).proposal_lines()
        self.assertIn("WRITES OFF its current_debt 0 USDC as a loss", lines[1])


RECOVERY = "0xd7a540ba3626c0aa66e7DB4088971d0CD64695B6"
WETH1 = "0xc56413869c6CDf96496f2b1eF801fEDBdFA7dDB0"
FLEX = "0xfaC55fAFD0b55BFb8dD41F735EfCc195adA9891F"
FLEX_OTHER = "0x7E4a6A89583e117C641aB3ce8897209800A3F2E3"
WETH1_DEBT = 1_957_483_253_785_268_466_043


def _recovery_context(calls: list[DecodedCall]) -> YearnV3VaultContext:
    """yETH recovery vault as Safe nonce 3356 found it, with Flex not yet registered."""
    flex = _strategy(
        FLEX,
        "Flex WETH yVault",
        0,
        0,
        activation=0,
        asset=WETH,
        total_assets=10 * E18,
        is_vault=True,
        sub_strategies=("WETH-1 yVault", "Other WETH strategy"),
        sub_strategy_addresses=(WETH1, FLEX_OTHER),
        auto_allocate=True,
        max_deposit=MAX_UINT256,
    )
    return YearnV3VaultContext(
        vault_address=RECOVERY,
        name="Yearn yETH Recovery Vault",
        symbol="yETH-Recovery",
        api_version="3.0.4",
        asset_address=WETH,
        asset_symbol="WETH",
        asset_decimals=18,
        total_assets=WETH1_DEBT + 489_320_000_000_000_000_000,
        total_debt=WETH1_DEBT + 489_320_000_000_000_000_000,
        total_idle=0,
        is_shutdown=False,
        deposit_limit=3_000 * E18,
        minimum_total_idle=0,
        use_default_queue=False,
        default_queue=(
            _strategy(WETH1, "WETH-1 yVault", WETH1_DEBT, MAX_UINT256, max_withdraw=WETH1_DEBT),
            _strategy(VAULT, "WETH-2 yVault", 489_320_000_000_000_000_000, 500 * E18),
        ),
        other_strategies=(flex,),
        calls=tuple(calls),
    )


class TestNestedRegistrationBatch(unittest.TestCase):
    """Safe nonce 3356: a timelock batch registers Flex, then top-level calls fund it and reset the queue."""

    def setUp(self) -> None:
        calls = [
            _call("add_strategy", ["address", "bool"], [FLEX, True]),
            _call("update_max_debt_for_strategy", ["address", "uint256"], [FLEX, 500 * E18]),
            _call("update_debt", ["address", "uint256"], [WETH1, WETH1_DEBT - 100 * E18]),
            _call("update_debt", ["address", "uint256"], [FLEX, 100 * E18]),
            _call("set_default_queue", ["address[]"], [[WETH1, VAULT]]),
        ]
        self.lines = _recovery_context(calls).proposal_lines()

    def test_funding_after_registration_moves_funds(self) -> None:
        # Without the nested add_strategy this line read "REVERTS — inactive strategy".
        self.assertIn(
            "update_debt(Flex WETH yVault) target_debt 100 WETH: MOVES FUNDS NOW — deposits 100 WETH", self.lines[3]
        )

    def test_allocator_strategy_overlaps_parent_exposure(self) -> None:
        self.assertIn("auto_allocate is True, so every deposit into it is forwarded at once", self.lines[0])
        self.assertIn("it allocates to WETH-1 yVault, which this vault already funds directly", self.lines[0])

    def test_queue_reset_drops_the_strategy_added_in_the_batch(self) -> None:
        line = self.lines[4]
        self.assertIn("[WETH-1 yVault, WETH-2 yVault, Flex WETH yVault] → [WETH-1 yVault, WETH-2 yVault]", line)
        self.assertIn("includes changes made earlier in this batch", line)
        self.assertIn("removed from the queue: Flex WETH yVault — still holds 100 WETH of debt", line)


if __name__ == "__main__":
    unittest.main()
