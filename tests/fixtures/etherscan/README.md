# Frozen verified-source responses

Gzipped `contract/getsourcecode` responses for the 3Jane USD3 / sUSD3 upgrade that
motivated the implementation-diff rewrite (yearn/monitoring#367).

| File | Contract | Address |
| --- | --- | --- |
| `usd3_old_*.json.gz` | USD3 (old impl) | [0xB606fB370Eaaad03d71B49aE5E42AA4aEC7458D9](https://etherscan.io/address/0xB606fB370Eaaad03d71B49aE5E42AA4aEC7458D9) |
| `usd3_new_*.json.gz` | USD3 (new impl) | [0xd1F1c3F485063712873285BF4ef25ab068f13893](https://etherscan.io/address/0xd1F1c3F485063712873285BF4ef25ab068f13893) |
| `susd3_old_*.json.gz` | sUSD3 (old impl) | [0x529cbf11fFbC272D63858ca40A2C7F2695712073](https://etherscan.io/address/0x529cbf11fFbC272D63858ca40A2C7F2695712073) |
| `susd3_new_*.json.gz` | sUSD3 (new impl) | [0x6093d95f6C102163D19F5681E7D84997060BBAed](https://etherscan.io/address/0x6093d95f6C102163D19F5681E7D84997060BBAed) |

Each file holds the response verbatim except that the `result[0]` object is trimmed
to the fields the fetch path reads (`SourceCode`, `ABI`, `ContractName`,
`CompilerVersion`, `OptimizationUsed`, `Runs`, `EVMVersion`). `SourceCode` keeps the
double-brace standard-json bundle exactly as returned — 35–38 files per contract,
which is what makes these useful: the failure they guard against is attributing an
imported file's code to the deployed contract.

Captured 2026-09-19 through the Etherscan-compatible
`api.routescan.io/v2/network/mainnet/evm/1/etherscan/api` endpoint (no API key needed,
same response shape). Verified source is immutable per address, so these do not go stale.
