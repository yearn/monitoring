"""Tests for utils/proxy.detect_proxy_upgrade."""

import unittest

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector
from eth_utils import to_checksum_address as _cs

from utils.proxy import (
    EIP1967_IMPL_SLOT,
    ZEPPELINOS_IMPL_SLOT,
    ProxyUpgrade,
    detect_proxy_upgrade,
    get_current_implementation,
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

    def _run(self, slot_values: dict[int, str | None], getter_addr: str | None = None) -> str | None:
        from unittest.mock import MagicMock, patch

        client = MagicMock()
        client.eth.get_storage_at.side_effect = lambda _addr, slot: self._slot_word(slot_values.get(slot))
        # eth.call backs the impl-getter fallback (implementation() etc.).
        client.eth.call.return_value = self._slot_word(getter_addr)
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

    def test_returns_none_when_nothing_resolves(self) -> None:
        self.assertIsNone(self._run({}, getter_addr=None))


if __name__ == "__main__":
    unittest.main()
