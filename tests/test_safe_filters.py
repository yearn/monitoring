"""Tests for protocols/safe/filters.py — which queued Safe txs each protocol alerts on."""

import unittest

from eth_abi import encode

from protocols.safe.filters import should_alert
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
PAUSE = "0x8456cb59"
UPGRADE = upgrade_to_and_call(NEW_ORACLE_IMPL)
EULER_VAULT = "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9"


def call_tx(data: str | None, to: str = GOVERNANCE, operation: int = 0) -> dict:
    return {"to": to.lower(), "operation": operation, "data": data}


def multisend_tx(calls: list[tuple[int, str, int, str]]) -> dict:
    return call_tx(pack_multisend(calls), to=MULTISEND_CALL_ONLY, operation=1)


def alerts(protocol: str, tx: dict) -> bool:
    return should_alert(protocol, tx, tx["to"], tx["data"] or "0x")


def _privileged_calls() -> dict[str, str]:
    """One call per privileged selector the Pendle rule must catch."""
    address_pair = ["address", "address"]
    return {
        "upgradeToAndCall": UPGRADE,
        # Missed by the old substring filter: the calldata never holds the proxy's own upgrade selector.
        "ProxyAdmin upgradeAndCall": encode_call(
            "upgradeAndCall(address,address,bytes)", [*address_pair, "bytes"], [ORACLE, NEW_ORACLE_IMPL, b""]
        ),
        "ProxyAdmin upgrade": encode_call("upgrade(address,address)", address_pair, [ORACLE, NEW_ORACLE_IMPL]),
        "changeProxyAdmin": encode_call("changeProxyAdmin(address,address)", address_pair, [ORACLE, ORACLE]),
        "transferOwnership": encode_call("transferOwnership(address)", ["address"], [NEW_ORACLE_IMPL]),
    }


class TestPendleRule(unittest.TestCase):
    def test_privileged_calls_alert_at_every_level(self) -> None:
        wrapped = {
            "direct": lambda data: call_tx(data, to=ORACLE),
            "aggregate": lambda data: call_tx(pendle_aggregate([(ORACLE, 0, PAUSE), (ORACLE, 0, data)])),
            "aggregateWithScopedAccess": lambda data: call_tx(
                pendle_aggregate([(ORACLE, 0, data)], name="aggregateWithScopedAccess")
            ),
            "multisend": lambda data: multisend_tx([(0, ORACLE, 0, PAUSE), (0, ORACLE, 0, data)]),
            "timelock in aggregate": lambda data: call_tx(
                pendle_aggregate([(ORACLE, 0, execute_batch([ORACLE], [data]))])
            ),
        }
        for call_name, data in _privileged_calls().items():
            for level, make_tx in wrapped.items():
                with self.subTest(call=call_name, level=level):
                    self.assertTrue(alerts("PENDLE", make_tx(data)))

    def test_uninspectable_calldata_fails_open(self) -> None:
        malformed_multisend = multisend_tx([(0, ORACLE, 0, PAUSE)])
        malformed_multisend["data"] = malformed_multisend["data"][:100]
        innermost = pendle_aggregate([(ORACLE, 0, PAUSE)])
        cases = {
            "malformed aggregate": call_tx(pendle_aggregate([(ORACLE, 0, PAUSE)])[:90]),
            "malformed multisend": malformed_multisend,
            "inner DELEGATECALL in multisend": multisend_tx([(1, ORACLE, 0, PAUSE)]),
            "DELEGATECALL into unknown contract": call_tx(PAUSE, to=ORACLE, operation=1),
            "wrappers past the depth limit": call_tx(
                pendle_aggregate([(ORACLE, 0, pendle_aggregate([(ORACLE, 0, innermost)]))])
            ),
        }
        for name, tx in cases.items():
            with self.subTest(name):
                self.assertTrue(alerts("PENDLE", tx))

    def test_routine_txs_are_skipped(self) -> None:
        # The old filter matched "4f1ef286" anywhere in the hex; only decoded selectors count now.
        selector_in_argument = "0xa9059cbb" + encode(["bytes32", "uint256"], [bytes.fromhex("4f1ef286" * 8), 1]).hex()
        cases = {
            "null calldata": call_tx(None),
            "empty calldata": call_tx("0x"),
            "routine aggregate": call_tx(pendle_aggregate([(ORACLE, 0, PAUSE), (ORACLE, 0, "0x3f4ba83a")])),
            "routine aggregateWithScopedAccess": call_tx(
                pendle_aggregate([(ORACLE, 0, PAUSE)], name="aggregateWithScopedAccess")
            ),
            "empty aggregate": call_tx(pendle_aggregate([])),
            "empty aggregateWithScopedAccess": call_tx(pendle_aggregate([], name="aggregateWithScopedAccess")),
            "routine multisend": multisend_tx([(0, ORACLE, 0, PAUSE)]),
            "selector bytes inside an argument": call_tx(selector_in_argument),
        }
        for name, tx in cases.items():
            with self.subTest(name):
                self.assertFalse(alerts("PENDLE", tx))


class TestOtherRules(unittest.TestCase):
    def test_euler_alerts_only_on_its_vault_in_any_case(self) -> None:
        self.assertTrue(alerts("EULER", call_tx("0x", to=EULER_VAULT)))
        self.assertTrue(alerts("EULER", call_tx("0x", to=EULER_VAULT.lower())))
        self.assertFalse(alerts("EULER", call_tx("0x", to="0x0000000000000000000000000000000000000bad")))

    def test_protocols_without_a_rule_alert_on_everything(self) -> None:
        self.assertTrue(alerts("LIDO", call_tx(None, to=ORACLE)))


if __name__ == "__main__":
    unittest.main()
