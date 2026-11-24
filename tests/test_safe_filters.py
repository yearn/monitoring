"""Tests for protocols/safe/filters.py — which queued Safe txs each protocol alerts on."""

import unittest

from eth_abi import encode

from protocols.safe.filters import has_privileged_call, should_alert
from tests.test_calldata_wrappers import (
    NEW_ORACLE_IMPL,
    ORACLE,
    encode_call,
    execute_batch,
    pendle_aggregate,
    upgrade_to_and_call,
)
from tests.test_safe_multisend import MULTISEND_CALL_ONLY, pack_multisend

GOVERNANCE = "0x" + "11" * 20  # stand-in for the governance contract the Safe calls
PROXY_ADMIN = "0x" + "22" * 20  # stand-in ProxyAdmin
PAUSE = "0x8456cb59"


def proxy_admin_upgrade_and_call() -> str:
    return encode_call(
        "upgradeAndCall(address,address,bytes)", ["address", "address", "bytes"], [ORACLE, NEW_ORACLE_IMPL, b""]
    )


def transfer_ownership() -> str:
    return encode_call("transferOwnership(address)", ["address"], [NEW_ORACLE_IMPL])


def call_tx(data: str | None, to: str = GOVERNANCE) -> dict:
    return {"to": to.lower(), "operation": 0, "data": data}


def multisend_tx(calls: list[tuple[int, str, int, str]]) -> dict:
    return {"to": MULTISEND_CALL_ONLY.lower(), "operation": 1, "data": pack_multisend(calls)}


def pendle_alerts(tx: dict) -> bool:
    return should_alert("PENDLE", tx, tx["to"], tx["data"] or "0x")


class TestPendleRule(unittest.TestCase):
    def test_direct_upgrade_to_and_call_alerts(self) -> None:
        self.assertTrue(pendle_alerts(call_tx(upgrade_to_and_call(NEW_ORACLE_IMPL), to=ORACLE)))

    def test_proxy_admin_upgrade_and_call_alerts(self) -> None:
        # Missed by the old substring filter: the calldata never holds the proxy's own upgrade selector.
        self.assertTrue(pendle_alerts(call_tx(proxy_admin_upgrade_and_call(), to=PROXY_ADMIN)))

    def test_proxy_admin_upgrade_and_change_admin_alert(self) -> None:
        upgrade = encode_call("upgrade(address,address)", ["address", "address"], [ORACLE, NEW_ORACLE_IMPL])
        change_admin = encode_call("changeProxyAdmin(address,address)", ["address", "address"], [ORACLE, ORACLE])
        self.assertTrue(pendle_alerts(call_tx(upgrade, to=PROXY_ADMIN)))
        self.assertTrue(pendle_alerts(call_tx(change_admin, to=PROXY_ADMIN)))

    def test_transfer_ownership_alerts(self) -> None:
        self.assertTrue(pendle_alerts(call_tx(transfer_ownership(), to=ORACLE)))

    def test_upgrade_inside_aggregate_alerts(self) -> None:
        data = pendle_aggregate([(ORACLE, 0, PAUSE), (ORACLE, 0, upgrade_to_and_call(NEW_ORACLE_IMPL))])
        self.assertTrue(pendle_alerts(call_tx(data)))

    def test_upgrade_inside_multisend_alerts(self) -> None:
        tx = multisend_tx([(0, ORACLE, 0, PAUSE), (0, PROXY_ADMIN, 0, proxy_admin_upgrade_and_call())])
        self.assertTrue(pendle_alerts(tx))

    def test_upgrade_inside_timelock_inside_aggregate_alerts(self) -> None:
        timelock_call = execute_batch([ORACLE], [upgrade_to_and_call(NEW_ORACLE_IMPL)])
        data = pendle_aggregate([(ORACLE, 0, timelock_call)])
        self.assertTrue(pendle_alerts(call_tx(data)))

    def test_routine_aggregate_is_skipped(self) -> None:
        data = pendle_aggregate([(ORACLE, 0, PAUSE), (ORACLE, 0, "0x3f4ba83a")])
        self.assertFalse(pendle_alerts(call_tx(data)))

    def test_routine_multisend_is_skipped(self) -> None:
        self.assertFalse(pendle_alerts(multisend_tx([(0, ORACLE, 0, PAUSE)])))

    def test_selector_bytes_inside_an_argument_are_skipped(self) -> None:
        # The old filter matched "4f1ef286" anywhere in the hex; only decoded selectors count now.
        data = "0xa9059cbb" + encode(["bytes32", "uint256"], [bytes.fromhex("4f1ef286" * 8), 1]).hex()
        self.assertIn("4f1ef286", data[10:])
        self.assertFalse(pendle_alerts(call_tx(data)))

    def test_null_and_empty_calldata_are_skipped(self) -> None:
        self.assertFalse(pendle_alerts(call_tx(None)))
        self.assertFalse(pendle_alerts(call_tx("0x")))

    def test_malformed_aggregate_alerts(self) -> None:
        data = pendle_aggregate([(ORACLE, 0, PAUSE)])[:90]
        self.assertTrue(pendle_alerts(call_tx(data)))

    def test_malformed_multisend_alerts(self) -> None:
        tx = multisend_tx([(0, ORACLE, 0, PAUSE)])
        tx["data"] = tx["data"][:100]
        self.assertTrue(pendle_alerts(tx))

    def test_inner_delegatecall_in_multisend_alerts(self) -> None:
        self.assertTrue(pendle_alerts(multisend_tx([(1, ORACLE, 0, PAUSE)])))

    def test_delegatecall_into_unknown_contract_alerts(self) -> None:
        tx = {"to": ORACLE.lower(), "operation": 1, "data": PAUSE}
        self.assertTrue(has_privileged_call(tx, PAUSE))

    def test_wrappers_nested_past_the_depth_limit_alert(self) -> None:
        innermost = pendle_aggregate([(ORACLE, 0, PAUSE)])
        data = pendle_aggregate([(ORACLE, 0, pendle_aggregate([(ORACLE, 0, innermost)]))])
        self.assertTrue(pendle_alerts(call_tx(data)))


class TestEulerRule(unittest.TestCase):
    VAULT = "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9"

    def test_vault_target_matches_in_any_case(self) -> None:
        for target in (self.VAULT, self.VAULT.lower()):
            with self.subTest(target=target):
                self.assertTrue(should_alert("EULER", call_tx("0x", to=target), target, "0x"))

    def test_other_target_is_skipped(self) -> None:
        other = "0x0000000000000000000000000000000000000bad"
        self.assertFalse(should_alert("EULER", call_tx("0x", to=other), other, "0x"))


class TestDefaultRule(unittest.TestCase):
    def test_protocols_without_a_rule_alert_on_everything(self) -> None:
        self.assertTrue(should_alert("LIDO", call_tx(None, to=ORACLE), ORACLE, "0x"))


if __name__ == "__main__":
    unittest.main()
