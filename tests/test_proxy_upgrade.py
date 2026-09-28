"""Tests for utils/proxy.detect_proxy_upgrade."""

import unittest
from unittest.mock import patch

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector
from eth_utils import to_checksum_address as _cs

from tests.test_calldata_wrappers import (
    CUSD,
    NEW_CUSD_IMPL,
    NEW_ORACLE_IMPL,
    ORACLE,
    ZERO32,
    execute_batch,
    upgrade_to_and_call,
)
from utils.proxy import (
    EIP1967_BEACON_SLOT,
    EIP1967_IMPL_SLOT,
    ZEPPELINOS_IMPL_SLOT,
    ProxyUpgrade,
    detect_proxy_upgrade,
    find_proxy_upgrades,
    format_upgrade_lines,
    get_current_implementation,
    minimal_proxy_implementation,
)


def encode_call(sig: str, types: list[str], vals: list) -> str:
    selector = function_signature_to_4byte_selector(sig).hex()
    encoded = encode(types, vals).hex()
    return "0x" + selector + encoded


PROXY_ADDR = _cs("0x40a2accbd92bca938b02010e17a5b8929b49130d")
NEW_IMPL = _cs("0x2038a35264815ce78bd57787de119dda4f57e216")


class TestDetectProxyUpgrade(unittest.TestCase):
    def test_upgrade_to(self) -> None:
        data = encode_call("upgradeTo(address)", ["address"], [NEW_IMPL])
        result = detect_proxy_upgrade(data, PROXY_ADDR)
        self.assertEqual(result, ProxyUpgrade(proxy_address=PROXY_ADDR, new_implementation=NEW_IMPL))

    def test_upgrade_to_and_call(self) -> None:
        data = encode_call("upgradeToAndCall(address,bytes)", ["address", "bytes"], [NEW_IMPL, b""])
        result = detect_proxy_upgrade(data, PROXY_ADDR)
        assert result is not None
        self.assertEqual(result.new_implementation, NEW_IMPL)
        self.assertEqual(result.proxy_address, PROXY_ADDR)

    def test_proxy_admin_upgrade_and_call(self) -> None:
        # ProxyAdmin pattern: proxy is arg 0, new impl is arg 1
        data = encode_call(
            "upgradeAndCall(address,address,bytes)",
            ["address", "address", "bytes"],
            [PROXY_ADDR, NEW_IMPL, b""],
        )
        # Target is the ProxyAdmin itself; proxy address comes from calldata
        admin = _cs("0xecda55c32966b00592ed3922e386063e1bc752c2")
        result = detect_proxy_upgrade(data, admin)
        assert result is not None
        self.assertEqual(result.proxy_address, PROXY_ADDR)
        self.assertEqual(result.new_implementation, NEW_IMPL)

    def test_non_upgrade_returns_none(self) -> None:
        data = encode_call("transfer(address,uint256)", ["address", "uint256"], [NEW_IMPL, 1])
        self.assertIsNone(detect_proxy_upgrade(data, PROXY_ADDR))

    def test_empty_calldata(self) -> None:
        self.assertIsNone(detect_proxy_upgrade("0x", PROXY_ADDR))
        self.assertIsNone(detect_proxy_upgrade("", PROXY_ADDR))

    def test_missing_target_for_direct_upgrade(self) -> None:
        # When upgrade is called on the proxy itself, target is needed
        data = encode_call("upgradeTo(address)", ["address"], [NEW_IMPL])
        self.assertIsNone(detect_proxy_upgrade(data, ""))

    def test_works_offline_for_all_proxy_selectors(self) -> None:
        """Regression: detect_proxy_upgrade must not depend on the Sourcify 4byte
        lookup for proxy upgrade selectors — those are in KNOWN_SELECTORS so the
        decode resolves locally even when the network is unreachable."""
        from unittest.mock import patch

        cases = [
            (
                "upgradeTo(address)",
                ["address"],
                [NEW_IMPL],
                PROXY_ADDR,
            ),
            (
                "upgradeToAndCall(address,bytes)",
                ["address", "bytes"],
                [NEW_IMPL, b""],
                PROXY_ADDR,
            ),
            (
                "upgradeAndCall(address,address,bytes)",
                ["address", "address", "bytes"],
                [PROXY_ADDR, NEW_IMPL, b""],
                _cs("0xecda55c32966b00592ed3922e386063e1bc752c2"),
            ),
        ]
        # Patch the 4byte lookup so any call to it would raise — proving we
        # never hit the network.
        with patch("utils.calldata.decoder.fetch_json") as mock_fetch:
            mock_fetch.side_effect = AssertionError("4byte fetch must not be called for known proxy selectors")
            for sig, types, vals, tx_target in cases:
                with self.subTest(sig=sig):
                    data = encode_call(sig, types, vals)
                    result = detect_proxy_upgrade(data, tx_target)
                    self.assertIsNotNone(result, f"detection failed offline for {sig}")
                    assert result is not None
                    self.assertEqual(result.new_implementation, NEW_IMPL)

    def test_non_upgrade_short_circuits_before_decode(self) -> None:
        """Perf regression guard: a non-upgrade selector must NOT trigger a
        Sourcify lookup. Without the early-return guard, every alert call
        could wait on a 30s timeout for unknown selectors."""
        from unittest.mock import patch

        # Random non-upgrade selector + arbitrary bytes — looks like unknown data
        data = "0xdeadbeef" + "00" * 32
        with patch("utils.calldata.decoder.fetch_json") as mock_fetch:
            mock_fetch.side_effect = AssertionError("Sourcify lookup triggered on non-upgrade selector")
            result = detect_proxy_upgrade(data, PROXY_ADDR)
        self.assertIsNone(result)
        mock_fetch.assert_not_called()


class TestGetCurrentImplementation(unittest.TestCase):
    """Tests for reading the implementation slot (EIP-1967 + legacy fallback)."""

    @staticmethod
    def _slot_word(addr: str | None) -> bytes:
        """32-byte storage word holding ``addr`` in its low 20 bytes (zero if None)."""
        if addr is None:
            return bytes(32)
        return bytes(12) + bytes.fromhex(addr[2:])

    def _run(
        self,
        slot_values: dict[int, str | None],
        getter_addr: str | None = None,
        getters_by_address: dict[str, str] | None = None,
    ) -> str | None:
        from unittest.mock import MagicMock, patch

        client = MagicMock()
        client.eth.get_storage_at.side_effect = lambda _addr, slot: self._slot_word(slot_values.get(slot))
        # eth.call backs the impl-getter fallback (implementation() etc.); keyed by
        # callee when a test needs to tell the proxy's getter from the beacon's.
        if getters_by_address is None:
            client.eth.call.return_value = self._slot_word(getter_addr)
        else:
            client.eth.call.side_effect = lambda tx: self._slot_word(getters_by_address.get(tx["to"]))
        with patch("utils.web3_wrapper.ChainManager.get_client", return_value=client):
            return get_current_implementation("0x" + "ab" * 20, chain_id=1)

    def test_reads_eip1967_slot(self) -> None:
        impl = _cs("0x" + "11" * 20)
        self.assertEqual(self._run({EIP1967_IMPL_SLOT: impl}), impl)

    def test_falls_back_to_zeppelinos_slot(self) -> None:
        impl = _cs("0x" + "22" * 20)
        self.assertEqual(self._run({EIP1967_IMPL_SLOT: None, ZEPPELINOS_IMPL_SLOT: impl}), impl)

    def test_falls_back_to_impl_getter(self) -> None:
        impl = _cs("0x" + "33" * 20)
        # Both slots empty → resolves via the implementation() getter.
        self.assertEqual(self._run({}, getter_addr=impl), impl)

    def test_eip1967_takes_precedence(self) -> None:
        eip = _cs("0x" + "11" * 20)
        result = self._run({EIP1967_IMPL_SLOT: eip, ZEPPELINOS_IMPL_SLOT: _cs("0x" + "22" * 20)})
        self.assertEqual(result, eip)

    def test_follows_beacon_to_its_implementation(self) -> None:
        beacon = _cs("0x" + "44" * 20)
        impl = _cs("0x" + "55" * 20)
        # The proxy itself answers implementation() with nothing; only the beacon knows.
        result = self._run({EIP1967_BEACON_SLOT: beacon}, getters_by_address={beacon: impl})
        self.assertEqual(result, impl)

    def test_beacon_without_getter_returns_none(self) -> None:
        # A beacon that answers no getter must not fall back to reading the proxy's own.
        proxy_getter = _cs("0x" + "66" * 20)
        beacon = _cs("0x" + "44" * 20)
        result = self._run({EIP1967_BEACON_SLOT: beacon}, getters_by_address={_cs("0x" + "ab" * 20): proxy_getter})
        self.assertIsNone(result)

    def test_impl_slot_takes_precedence_over_beacon(self) -> None:
        impl = _cs("0x" + "11" * 20)
        beacon = _cs("0x" + "44" * 20)
        result = self._run(
            {EIP1967_IMPL_SLOT: impl, EIP1967_BEACON_SLOT: beacon},
            getters_by_address={beacon: _cs("0x" + "55" * 20)},
        )
        self.assertEqual(result, impl)

    def test_returns_none_when_nothing_resolves(self) -> None:
        self.assertIsNone(self._run({}, getter_addr=None))


class TestMinimalProxyImplementation(unittest.TestCase):
    """EIP-1167 runtime bytecode embeds the implementation address."""

    IMPL = _cs("0xd8063123bba3b480569244ae66bfe72b6c84b00d")
    CODE = "363d3d373d3d3d363d73" + IMPL[2:].lower() + "5af43d82803e903d91602b57fd5bf3"

    def test_extracts_implementation_from_hex_and_bytes(self) -> None:
        self.assertEqual(minimal_proxy_implementation("0x" + self.CODE), self.IMPL)
        self.assertEqual(minimal_proxy_implementation(bytes.fromhex(self.CODE)), self.IMPL)

    def test_rejects_other_bytecode(self) -> None:
        self.assertIsNone(minimal_proxy_implementation(""))
        self.assertIsNone(minimal_proxy_implementation("0x6080604052"))
        # Right prefix, wrong length (e.g. a longer contract that starts the same way).
        self.assertIsNone(minimal_proxy_implementation(self.CODE + "00"))
        self.assertIsNone(minimal_proxy_implementation(self.CODE[:-2] + "00"))


TIMELOCK = _cs("0xd8236031d8279d82e615af2bfab5fc0127a329ab")
OLD_IMPL = _cs("0xbfe4d64e61a3a02c4781a65fa343007de7ea9f14")


def execute(target: str, payload: str) -> str:
    return encode_call(
        "execute(address,uint256,bytes,bytes32,bytes32)",
        ["address", "uint256", "bytes", "bytes32", "bytes32"],
        [target, 0, bytes.fromhex(payload[2:]), ZERO32, ZERO32],
    )


class TestFindProxyUpgrades(unittest.TestCase):
    def test_direct_upgrade_has_no_via(self) -> None:
        self.assertEqual(
            find_proxy_upgrades(upgrade_to_and_call(NEW_ORACLE_IMPL), ORACLE),
            [ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL)],
        )

    def test_upgrades_inside_a_timelock_execute_batch(self) -> None:
        """The CAP Safe tx: the Safe calls executeBatch on the timelock, the upgrades are inside."""
        data = execute_batch([ORACLE, CUSD], [upgrade_to_and_call(NEW_ORACLE_IMPL), upgrade_to_and_call(NEW_CUSD_IMPL)])
        self.assertEqual(
            find_proxy_upgrades(data, TIMELOCK),
            [
                ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL, via="executeBatch call 1"),
                ProxyUpgrade(CUSD, NEW_CUSD_IMPL, via="executeBatch call 2"),
            ],
        )

    def test_non_upgrade_inner_calls_are_skipped(self) -> None:
        transfer = encode_call("transfer(address,uint256)", ["address", "uint256"], [NEW_IMPL, 1])
        data = execute_batch([CUSD, ORACLE], [transfer, upgrade_to_and_call(NEW_ORACLE_IMPL)])
        self.assertEqual(
            find_proxy_upgrades(data, TIMELOCK), [ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL, via="executeBatch call 2")]
        )

    def test_two_wrappers_deep(self) -> None:
        inner = execute(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL))
        outer = execute(TIMELOCK, inner)
        self.assertEqual(
            find_proxy_upgrades(outer, TIMELOCK), [ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL, via="execute → execute")]
        )

    def test_nesting_past_the_depth_limit_is_not_followed(self) -> None:
        data = execute(TIMELOCK, execute(TIMELOCK, execute(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL))))
        self.assertEqual(find_proxy_upgrades(data, TIMELOCK), [])

    def test_repeated_upgrade_is_reported_once(self) -> None:
        payload = upgrade_to_and_call(NEW_ORACLE_IMPL)
        data = execute_batch([ORACLE, ORACLE], [payload, payload])
        self.assertEqual(len(find_proxy_upgrades(data, TIMELOCK)), 1)

    def test_plain_non_upgrade_call(self) -> None:
        self.assertEqual(find_proxy_upgrades("0xdeadbeef" + "00" * 32, ORACLE), [])


class TestFormatUpgradeLines(unittest.TestCase):
    @patch("utils.proxy.get_current_implementation", return_value=OLD_IMPL)
    def test_direct_upgrade_keeps_the_timelock_alert_format(self, _impl) -> None:
        lines = format_upgrade_lines(ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL), ORACLE, 1)
        self.assertEqual(
            lines,
            [
                f"🔄 Upgrade: `{OLD_IMPL}` → `{NEW_ORACLE_IMPL}`",
                f"📊 [Diff](https://etherscan.io/contractdiffchecker?a1={OLD_IMPL}&a2={NEW_ORACLE_IMPL})",
            ],
        )

    @patch("utils.proxy.get_current_implementation", return_value=OLD_IMPL)
    def test_nested_upgrade_names_the_proxy_and_the_path(self, _impl) -> None:
        lines = format_upgrade_lines(ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL, via="executeBatch call 1"), TIMELOCK, 1)
        self.assertEqual(lines[0], f"🅿️ Proxy: `{ORACLE}` (via executeBatch call 1)")
        self.assertEqual(lines[1], f"🔄 Upgrade: `{OLD_IMPL}` → `{NEW_ORACLE_IMPL}`")

    @patch("utils.proxy.get_current_implementation", return_value=None)
    def test_unknown_current_implementation(self, _impl) -> None:
        lines = format_upgrade_lines(ProxyUpgrade(ORACLE, NEW_ORACLE_IMPL), ORACLE, 1)
        self.assertEqual(lines, [f"🔄 New impl: `{NEW_ORACLE_IMPL}`"])


if __name__ == "__main__":
    unittest.main()
