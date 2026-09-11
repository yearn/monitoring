"""Tests for utils/calldata/role_names.py."""

import unittest
from unittest.mock import patch

from eth_utils import keccak

from utils.calldata.role_names import (
    DEFAULT_ADMIN_ROLE,
    harvest_role_names,
    normalize_role_hash,
    resolve_role_names,
)

# The two roles from InfiniFi's CoreRoles library that a LongTimelock batch
# granted to a new Outland vault. Neither is an OpenZeppelin standard role, so
# they only resolve by reading the contract's own source.
RECEIPT_TOKEN_MINTER = "0x615a688d53344290b742a2e72e4f187e5b88227c01f9d77ce2406d32f8bd0eda"
RECEIPT_TOKEN_BURNER = "0xc843f0b739b89f713c29d5349e88538222378b9e38ad958b090593af5e3d0fd9"

CORE_ROLES_SOURCE = """
library CoreRoles {
    /// @notice the all-powerful role.
    bytes32 internal constant GOVERNOR = keccak256("GOVERNOR");
    /// @notice can mint DebtToken arbitrarily
    bytes32 internal constant RECEIPT_TOKEN_MINTER = keccak256("RECEIPT_TOKEN_MINTER");
    /// @notice can burn DebtToken tokens
    bytes32 internal constant RECEIPT_TOKEN_BURNER = keccak256("RECEIPT_TOKEN_BURNER");
    bytes32 public constant FARM_MANAGER = keccak256("FARM_MANAGER");
}
"""


class TestNormalizeRoleHash(unittest.TestCase):
    """Tests for normalize_role_hash."""

    def test_accepts_prefixed_and_mixed_case(self) -> None:
        self.assertEqual(normalize_role_hash(RECEIPT_TOKEN_MINTER.upper().replace("0X", "0x")), RECEIPT_TOKEN_MINTER)

    def test_accepts_unprefixed(self) -> None:
        self.assertEqual(normalize_role_hash(RECEIPT_TOKEN_MINTER[2:]), RECEIPT_TOKEN_MINTER)

    def test_accepts_raw_bytes(self) -> None:
        self.assertEqual(normalize_role_hash(bytes.fromhex(RECEIPT_TOKEN_MINTER[2:])), RECEIPT_TOKEN_MINTER)

    def test_rejects_wrong_length_and_non_hex(self) -> None:
        for bad in ("0xdeadbeef", "", "0x" + "zz" * 32, "not-a-hash", b"short", None, 42):
            self.assertEqual(normalize_role_hash(bad), "")


class TestHarvestRoleNames(unittest.TestCase):
    """Tests for harvest_role_names."""

    def test_harvests_internal_and_public_constants(self) -> None:
        harvested = harvest_role_names(CORE_ROLES_SOURCE)
        self.assertEqual(harvested[RECEIPT_TOKEN_MINTER], "RECEIPT_TOKEN_MINTER")
        self.assertEqual(harvested[RECEIPT_TOKEN_BURNER], "RECEIPT_TOKEN_BURNER")
        self.assertEqual(harvested["0x" + keccak(text="FARM_MANAGER").hex()], "FARM_MANAGER")

    def test_reports_both_names_when_constant_and_preimage_differ(self) -> None:
        source = 'bytes32 internal constant ADMIN = keccak256("SUPER_ADMIN");'
        harvested = harvest_role_names(source)
        self.assertEqual(harvested["0x" + keccak(text="SUPER_ADMIN").hex()], 'ADMIN (keccak256("SUPER_ADMIN"))')

    def test_empty_source_is_safe(self) -> None:
        self.assertEqual(harvest_role_names(""), {})


class TestResolveRoleNames(unittest.TestCase):
    """Tests for resolve_role_names."""

    def test_static_table_resolves_without_network(self) -> None:
        minter = "0x" + keccak(text="MINTER_ROLE").hex()
        resolved = resolve_role_names([minter, DEFAULT_ADMIN_ROLE])
        self.assertEqual(resolved[minter], "MINTER_ROLE")
        self.assertEqual(resolved[DEFAULT_ADMIN_ROLE], "DEFAULT_ADMIN_ROLE")

    @patch("utils.proxy.get_current_implementation", return_value=None)
    @patch("utils.source_context.fetch_source")
    def test_falls_back_to_verified_source(self, mock_fetch, _mock_impl) -> None:
        mock_fetch.return_value = ("InfiniFiCore", CORE_ROLES_SOURCE)

        resolved = resolve_role_names([RECEIPT_TOKEN_MINTER, RECEIPT_TOKEN_BURNER], chain_id=1, target="0xF6d4")
        self.assertEqual(resolved[RECEIPT_TOKEN_MINTER], "RECEIPT_TOKEN_MINTER")
        self.assertEqual(resolved[RECEIPT_TOKEN_BURNER], "RECEIPT_TOKEN_BURNER")

    @patch("utils.proxy.get_current_implementation", return_value=None)
    @patch("utils.source_context.fetch_source")
    def test_accepts_raw_bytes_from_decoded_calldata(self, mock_fetch, _mock_impl) -> None:
        """`decode_calldata` yields 32 raw bytes, not hex — the common real case."""
        mock_fetch.return_value = ("InfiniFiCore", CORE_ROLES_SOURCE)

        raw = bytes.fromhex(RECEIPT_TOKEN_MINTER[2:])
        resolved = resolve_role_names([raw], chain_id=1, target="0xF6d4")
        self.assertEqual(resolved[RECEIPT_TOKEN_MINTER], "RECEIPT_TOKEN_MINTER")

    @patch("utils.proxy.get_current_implementation")
    @patch("utils.source_context.fetch_source")
    def test_follows_proxy_to_implementation_source(self, mock_fetch, mock_impl) -> None:
        """Role constants live in the implementation, not the proxy."""
        proxy, impl = "0xProxy", "0xImpl"
        mock_impl.return_value = impl
        mock_fetch.side_effect = lambda _chain, addr: {
            proxy: ("TransparentUpgradeableProxy", "contract Proxy { fallback() external {} }"),
            impl: ("InfiniFiCore", CORE_ROLES_SOURCE),
        }[addr]

        resolved = resolve_role_names([RECEIPT_TOKEN_MINTER], chain_id=1, target=proxy)
        self.assertEqual(resolved[RECEIPT_TOKEN_MINTER], "RECEIPT_TOKEN_MINTER")

    @patch("utils.proxy.get_current_implementation")
    @patch("utils.source_context.fetch_source")
    def test_skips_proxy_hop_when_target_source_resolves(self, mock_fetch, mock_impl) -> None:
        """A non-proxy target must not pay an RPC call to read an implementation slot."""
        mock_fetch.return_value = ("InfiniFiCore", CORE_ROLES_SOURCE)

        resolve_role_names([RECEIPT_TOKEN_MINTER], chain_id=1, target="0xF6d4")
        mock_impl.assert_not_called()

    @patch("utils.source_context.fetch_source")
    def test_skips_source_lookup_when_static_table_suffices(self, mock_fetch: unittest.mock.MagicMock) -> None:
        resolve_role_names(["0x" + keccak(text="PAUSER_ROLE").hex()], chain_id=1, target="0xF6d4")
        mock_fetch.assert_not_called()

    @patch("utils.proxy.get_current_implementation", return_value=None)
    @patch("utils.source_context.fetch_source")
    def test_unverified_contract_leaves_role_unresolved(self, mock_fetch, _mock_impl) -> None:
        mock_fetch.return_value = None
        self.assertEqual(resolve_role_names([RECEIPT_TOKEN_MINTER], chain_id=1, target="0xF6d4"), {})

    @patch("utils.source_context.fetch_source")
    def test_source_failure_never_raises(self, mock_fetch: unittest.mock.MagicMock) -> None:
        mock_fetch.side_effect = RuntimeError("etherscan down")
        self.assertEqual(resolve_role_names([RECEIPT_TOKEN_MINTER], chain_id=1, target="0xF6d4"), {})

    @patch("utils.proxy.get_current_implementation", side_effect=RuntimeError("rpc down"))
    @patch("utils.source_context.fetch_source", return_value=("Proxy", "contract Proxy {}"))
    def test_proxy_lookup_failure_never_raises(self, _mock_fetch, _mock_impl) -> None:
        self.assertEqual(resolve_role_names([RECEIPT_TOKEN_MINTER], chain_id=1, target="0xF6d4"), {})

    def test_no_chain_context_returns_static_matches_only(self) -> None:
        self.assertEqual(resolve_role_names([RECEIPT_TOKEN_MINTER]), {})

    def test_garbage_input_returns_empty(self) -> None:
        self.assertEqual(resolve_role_names(["not-a-hash", ""]), {})


if __name__ == "__main__":
    unittest.main()
