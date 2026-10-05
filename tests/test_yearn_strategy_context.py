"""Tests for the Yearn tokenized-strategy protocol-context adapter."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.erc20_metadata import ERC20Metadata
from utils.llm import yearn_strategy_context
from utils.llm.yearn_strategy_context import (
    format_yearn_strategy_prompt,
    format_yearn_strategy_report,
    resolve_yearn_strategy_context,
)

# Strategist Safe nonce 3359, values read at block 26127631.
GROVE = "0xe060B80438771f13078048c3b0d930efECA6E622"
SPARK = "0xc9f01b5c6048B064E6d925d1c2d7206d4fEeF8a3"
VAULT = "0x182863131F9a4630fF9E27830d945B1413e347E8"
BRAIN = "0x16388463d60FFE0661Cf7F1f31a7D658aC790ff7"
RELAYER = "0x604e586F17cE106B64185A7a0d2c1Da5bAce711E"
SAM = "0xe5e2Baf96198c56380dDD5E992D7d1ADa0e989c0"
ACCOUNTANT = "0x5A74Cb32D36f2f517DB6f7b0A0591e09b22cDE69"
COMMON_TRIGGER = "0xf8dF17a35c88AbB25e83C92f9D293B4368b9D52D"
FIXED_TRIGGER = "0xb9F57B62Cbe9463da16E5b75e3B809321a0eA871"
USDS = "0xdC035D45d973E3EC169d2276DDab16f1e407384F"
ZERO = "0x0000000000000000000000000000000000000000"
NOW = 1_791_220_955
E18 = 10**18

LABELS = {
    BRAIN: "Strategist Multisig (brain.ychad.eth)",
    SAM: "GnosisSafeProxy",
    ACCOUNTANT: "Accountant",
    FIXED_TRIGGER: "StrategyFixedReportTrigger",
}


def _strategy(**overrides: object) -> dict[str, object]:
    reads: dict[str, object] = {
        "apiVersion()": "3.1.0",
        "name()": "Grove USDS Compounder",
        "asset()": USDS,
        "management()": BRAIN,
        "keeper()": RELAYER,
        "emergencyAdmin()": BRAIN,
        "performanceFeeRecipient()": BRAIN,
        "performanceFee()": 0,
        "profitMaxUnlockTime()": 172_800,
        "lastReport()": 1_791_208_547,
        "totalAssets()": 4_766_985_299436699127602788,
        "totalSupply()": 4_756_619_815804272750024760,
        "balanceOf(address)": 5_644_714279396621190865,
        "unlockedShares()": 634_658246604263177442,
        "isShutdown()": False,
        "open()": False,
    }
    reads.update(overrides)
    return reads


CHAIN: dict[str, dict[str, object]] = {
    GROVE: _strategy(),
    SPARK: _strategy(
        **{
            "apiVersion()": "3.0.4",
            "name()": "Spark USDS Compounder",
            "emergencyAdmin()": SAM,
            "performanceFeeRecipient()": ACCOUNTANT,
            "profitMaxUnlockTime()": 345_600,
            "open()": None,
            "openDeposits()": True,
        }
    ),
    # A V3 vault answers apiVersion() but has no management()/lastReport().
    VAULT: {"apiVersion()": "3.0.3", "profitMaxUnlockTime()": 0},
    COMMON_TRIGGER: {"customStrategyTrigger(address)": ZERO},
    FIXED_TRIGGER: {"minReportDelay()": 259_200},
}


def _view(client: object, address: str, signature: str, output: str, args: tuple = ()) -> object | None:
    return CHAIN.get(address, {}).get(signature)


def _call(name: str, signature: str, *params: tuple[str, object]) -> DecodedCall:
    return DecodedCall(name, signature, list(params))


def _unlock(seconds: int) -> DecodedCall:
    return _call("setProfitMaxUnlockTime", "setProfitMaxUnlockTime(uint256)", ("uint256", seconds))


def _trigger(strategy: str) -> DecodedCall:
    return _call(
        "setCustomStrategyTrigger",
        "setCustomStrategyTrigger(address,address)",
        ("address", strategy),
        ("address", FIXED_TRIGGER),
    )


REPORT = _call("report", "report()")


@patch.object(yearn_strategy_context, "get_contract_label", side_effect=lambda chain_id, a: LABELS.get(a, ""))
@patch.object(yearn_strategy_context, "fetch_erc20_metadata", return_value=ERC20Metadata("USDS", 18, "USDS Stablecoin"))
@patch.object(yearn_strategy_context, "_view", side_effect=_view)
@patch.object(yearn_strategy_context, "ChainManager")
class TestYearnStrategyContext(unittest.TestCase):
    def _resolve(self, mock_cm: MagicMock, calls: list[tuple[str, DecodedCall]]) -> list:
        mock_cm.get_client.return_value.eth.get_block.return_value = {"timestamp": NOW}
        return resolve_yearn_strategy_context("YEARN_MS", 1, calls)

    def test_grove_batch_shows_old_to_new_and_the_unlock_burn(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        calls = [
            (GROVE, _call("setEmergencyAdmin", "setEmergencyAdmin(address)", ("address", SAM))),
            (
                GROVE,
                _call("setPerformanceFeeRecipient", "setPerformanceFeeRecipient(address)", ("address", ACCOUNTANT)),
            ),
            (GROVE, _unlock(0)),
            (COMMON_TRIGGER, _trigger(GROVE)),
        ]
        (context,) = self._resolve(mock_cm, calls)
        prompt = format_yearn_strategy_prompt([context])

        self.assertIn("profitMaxUnlockTime 2d (172800 s)", prompt)
        self.assertIn("deposits are allowlist-only (`open` = false)", prompt)
        self.assertIn(
            f"setEmergencyAdmin: {BRAIN} (Strategist Multisig (brain.ychad.eth)) → {SAM} (GnosisSafeProxy)", prompt
        )
        self.assertIn("The performance fee is 0, so the recipient receives nothing", prompt)
        self.assertIn("setProfitMaxUnlockTime: 2d (172800 s) → 0 (instant)", prompt)
        self.assertIn("6,279.37 shares (5,644.71 shares still locking, 634.66 shares already unlocked)", prompt)
        self.assertIn("~5,657.01 USDS of not-yet-unlocked profit is credited to holders at once", prompt)
        self.assertIn("(price per share +0.119%)", prompt)
        self.assertIn(f"default trigger → {FIXED_TRIGGER} (StrategyFixedReportTrigger)", prompt)
        self.assertIn("report only once 3d (259200 s) have passed since lastReport (last report 3h 26m ago)", prompt)

        report = format_yearn_strategy_report([context], 1, {})
        self.assertIn(f"[`{SAM}`](https://etherscan.io/address/{SAM}) (GnosisSafeProxy)", report)

    def test_report_timing_follows_the_unlock_time_set_earlier_in_the_batch(
        self, mock_cm: MagicMock, *_: MagicMock
    ) -> None:
        (after,) = self._resolve(mock_cm, [(GROVE, _unlock(0)), (GROVE, REPORT)])
        (before,) = self._resolve(mock_cm, [(GROVE, REPORT), (GROVE, _unlock(0))])
        render = str
        self.assertIn("credited to holders immediately", after.proposal_lines(render)[1])
        self.assertIn("unlocked to holders linearly over 2d", before.proposal_lines(render)[0])
        self.assertIn("as of before this transaction (a report earlier in the batch", before.proposal_lines(render)[1])

    def test_closing_spark_deposits(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        close = _call("setOpenDeposits", "setOpenDeposits(bool)", ("bool", False))
        (context,) = self._resolve(mock_cm, [(SPARK, _unlock(0)), (SPARK, close)])
        lines = context.proposal_lines(str)
        self.assertIn("deposits are open to anyone (`openDeposits` = true) before this batch", lines[0])
        self.assertIn(
            "deposits are open to anyone (`openDeposits` = true) → deposits are allowlist-only (`openDeposits` = false)",
            lines[1],
        )
        self.assertIn("Withdrawals are unaffected", lines[1])

    def test_vaults_and_unrelated_calls_are_ignored(self, mock_cm: MagicMock, *_: MagicMock) -> None:
        self.assertEqual(self._resolve(mock_cm, [(VAULT, _unlock(0))]), [])
        mock_cm.reset_mock()
        transfer = _call("transfer", "transfer(address,uint256)", ("address", BRAIN), ("uint256", E18))
        self.assertEqual(resolve_yearn_strategy_context("YEARN_MS", 1, [(GROVE, transfer)]), [])
        mock_cm.get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
