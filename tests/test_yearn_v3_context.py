"""Tests for the Yearn V3 vault protocol-context adapter."""

import unittest
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


def _strategy(address: str, name: str, current: int, max_debt: int, **kwargs: object) -> StrategyState:
    return StrategyState(
        address=address,
        name=name,
        activation=kwargs.pop("activation", 1),  # type: ignore[arg-type]
        current_debt=current,
        max_debt=max_debt,
        in_default_queue=kwargs.pop("in_default_queue", True),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
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
        in_default_queue=False,
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

    def test_add_strategy_out_of_queue_with_forced_default_queue(self) -> None:
        (line,) = _context([ADD_LOOPER]).proposal_lines()
        self.assertIn("add_strategy(wstETH/WETH Spark Looper): not yet active", line)
        self.assertIn("strategy asset matches the vault asset WETH", line)
        self.assertIn("add_to_queue=False: stays OUT of the default queue", line)
        self.assertIn("use_default_queue is True, so no withdrawal can pull from it", line)
        self.assertIn("strategy totalAssets 1,034 WETH", line)

    def test_add_strategy_out_of_queue_allows_custom_queue(self) -> None:
        (line,) = _context([ADD_LOOPER], use_default_queue=False).proposal_lines()
        self.assertIn("unless a withdrawer passes a custom queue", line)

    def test_add_strategy_defaults_to_queue(self) -> None:
        # The one-argument overload leaves add_to_queue at its default of True.
        (line,) = _context([_call("add_strategy", ["address"], [LOOPER])]).proposal_lines()
        self.assertIn("add_to_queue=True: appended to the default queue (holds 1/10 before this batch)", line)

    def test_add_strategy_with_mismatched_asset_reverts(self) -> None:
        context = _context([ADD_LOOPER])
        other = _strategy(LOOPER, "Looper", 0, 0, activation=0, in_default_queue=False, asset=PEER)
        context = YearnV3VaultContext(**{**context.__dict__, "other_strategies": (other,)})
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

    def test_update_debt_moves_funds_now(self) -> None:
        call = _call("update_debt", ["address", "uint256"], [PEER, 1_000 * E18])
        (line,) = _context([call]).proposal_lines()
        self.assertIn("MOVES FUNDS NOW", line)
        self.assertIn("921 WETH → 1,000 WETH (deposits 79 WETH", line)

    def test_force_revoke_writes_off_debt(self) -> None:
        call = _call("force_revoke_strategy", ["address"], [PEER])
        (line,) = _context([call]).proposal_lines()
        self.assertIn("WRITES OFF its current_debt 921 WETH as a loss", line)

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


def _client(batches: list[list[object]]) -> MagicMock:
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
        mock_details.assert_called_once_with(client, 1, LOOPER)


if __name__ == "__main__":
    unittest.main()
