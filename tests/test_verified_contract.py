"""Tests for utils/verified_contract.py."""

import json
import unittest

from utils.verified_contract import (
    CACHE_SCHEMA_VERSION,
    VerifiedContract,
    parse_abi,
    parse_etherscan_entry,
    resolve_contract_file,
)

ABI_JSON = json.dumps([{"type": "function", "name": "cap", "inputs": [], "outputs": [], "stateMutability": "view"}])

STANDARD_JSON = json.dumps(
    {
        "language": "Solidity",
        "sources": {
            "src/Vault.sol": {"content": "contract Vault { uint256 public cap; }"},
            "src/IVault.sol": {"content": "interface IVault { function cap() external view returns (uint256); }"},
        },
        "settings": {"optimizer": {"enabled": True, "runs": 200}},
    }
)


def _entry(source: str, name: str = "Vault") -> dict:
    return {
        "SourceCode": source,
        "ABI": ABI_JSON,
        "ContractName": name,
        "CompilerVersion": "v0.8.22+commit.4fc1097e",
    }


class TestParseEtherscanEntry(unittest.TestCase):
    def test_single_file_source(self) -> None:
        contract = parse_etherscan_entry(_entry("contract Vault { uint256 public cap; }"))
        assert contract is not None
        self.assertEqual(contract.contract_name, "Vault")
        self.assertEqual(list(contract.sources), ["Vault.sol"])
        self.assertEqual(contract.contract_file, "Vault.sol")

    def test_double_brace_standard_json(self) -> None:
        contract = parse_etherscan_entry(_entry("{" + STANDARD_JSON + "}"))
        assert contract is not None
        self.assertEqual(sorted(contract.sources), ["src/IVault.sol", "src/Vault.sol"])
        self.assertEqual(contract.settings["optimizer"]["runs"], 200)
        self.assertEqual(contract.compilation_target, ("src/Vault.sol", "Vault"))

    def test_flat_path_to_content_map(self) -> None:
        raw = json.dumps({"Vault.sol": {"content": "contract Vault {}"}})
        contract = parse_etherscan_entry(_entry(raw))
        assert contract is not None
        self.assertEqual(list(contract.sources), ["Vault.sol"])

    def test_unparseable_json_falls_back_to_raw_text(self) -> None:
        contract = parse_etherscan_entry(_entry("{not valid json}"))
        assert contract is not None
        self.assertEqual(contract.sources, {"Vault.sol": "{not valid json}"})

    def test_unverified_entry_returns_none(self) -> None:
        self.assertIsNone(parse_etherscan_entry({"SourceCode": "", "ABI": "Contract source code not verified"}))

    def test_target_source_is_only_the_deployed_file(self) -> None:
        contract = parse_etherscan_entry(_entry("{" + STANDARD_JSON + "}"))
        assert contract is not None
        self.assertIn("contract Vault", contract.target_source)
        self.assertNotIn("interface IVault", contract.target_source)
        # The search helper still sees everything, and is named for it.
        self.assertIn("interface IVault", contract.concatenated_source())


class TestResolveContractFile(unittest.TestCase):
    def test_prefers_compilation_target_setting(self) -> None:
        sources = {"a.sol": "contract Vault {}", "b.sol": "contract Vault {}"}
        settings = {"compilationTarget": {"b.sol": "Vault"}}
        self.assertEqual(resolve_contract_file("Vault", sources, settings), "b.sol")

    def test_falls_back_to_unique_declaration(self) -> None:
        sources = {"a.sol": 'import "./b.sol"; contract Strategy is Vault {}', "b.sol": "contract Vault {}"}
        self.assertEqual(resolve_contract_file("Vault", sources, {}), "b.sol")

    def test_ambiguous_declaration_returns_none(self) -> None:
        sources = {"a.sol": "contract Vault {}", "b.sol": "contract Vault {}"}
        self.assertIsNone(resolve_contract_file("Vault", sources, {}))

    def test_missing_declaration_returns_none(self) -> None:
        self.assertIsNone(resolve_contract_file("Vault", {"a.sol": "contract Other {}"}, {}))


class TestCacheRoundTrip(unittest.TestCase):
    def test_round_trip(self) -> None:
        entry = {**_entry("{" + STANDARD_JSON + "}"), "ConstructorArguments": "0xABCD"}
        contract = parse_etherscan_entry(entry)
        assert contract is not None
        self.assertEqual(contract.constructor_arguments, "abcd")
        restored = VerifiedContract.from_cache_dict(json.loads(json.dumps(contract.to_cache_dict())))
        self.assertEqual(restored, contract)

    def test_similar_match_constructor_arguments_are_unavailable(self) -> None:
        """Etherscan does not verify constructor arguments for Similar Match."""
        entry = {
            **_entry("contract Vault {}"),
            "ConstructorArguments": "",
            "SimilarMatch": "0x" + "a" * 40,
        }
        contract = parse_etherscan_entry(entry)
        assert contract is not None
        self.assertIsNone(contract.constructor_arguments)
        restored = VerifiedContract.from_cache_dict({**contract.to_cache_dict(), "constructor_arguments": "dead"})
        self.assertEqual(restored, contract)

    def test_blank_legacy_constructor_arguments_with_inputs_are_unknown(self) -> None:
        """A blank field cannot represent encoded values for a constructor with inputs."""
        entry = {
            **_entry("contract Vault { constructor(uint256) {} }"),
            "ABI": json.dumps([{"type": "constructor", "inputs": [{"type": "uint256"}]}]),
            "ConstructorArguments": "",
        }
        contract = parse_etherscan_entry(entry)
        assert contract is not None
        self.assertIsNone(contract.constructor_arguments)
        cached = {**contract.to_cache_dict(), "constructor_arguments": ""}
        cached.pop("provider")
        cached.pop("match_quality")
        self.assertEqual(VerifiedContract.from_cache_dict(cached), contract)

    def test_deployment_without_constructor_arguments_keeps_empty_value(self) -> None:
        """A verified no-argument deployment records a known empty value."""
        contract = parse_etherscan_entry({**_entry("contract Vault {}"), "ConstructorArguments": ""})
        assert contract is not None
        self.assertEqual(contract.constructor_arguments, "")
        self.assertEqual(VerifiedContract.from_cache_dict(contract.to_cache_dict()), contract)

    def test_older_schema_is_rejected(self) -> None:
        """A flattened record from the previous cache version must not be reused."""
        self.assertIsNone(VerifiedContract.from_cache_dict(["Vault", "contract Vault {}", ABI_JSON]))
        self.assertIsNone(VerifiedContract.from_cache_dict({"schema": CACHE_SCHEMA_VERSION - 1, "sources": {}}))


class TestParseAbi(unittest.TestCase):
    def test_parses_list(self) -> None:
        self.assertEqual(len(parse_abi(ABI_JSON) or []), 1)

    def test_rejects_unverified_and_malformed(self) -> None:
        self.assertIsNone(parse_abi("Contract source code not verified"))
        self.assertIsNone(parse_abi("{"))
        self.assertIsNone(parse_abi(""))


if __name__ == "__main__":
    unittest.main()
