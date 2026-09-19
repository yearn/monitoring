# Frozen Sourcify storage layouts

Gzipped `GET https://sourcify.dev/server/v2/contract/1/<address>?fields=storageLayout`
responses for the 3Jane USD3 / sUSD3 upgrade (yearn/monitoring#367), captured
2026-09-19 alongside the Etherscan bundles in `../etherscan/`.

| File | Sourcify `match` | Layout entries |
| --- | --- | ---: |
| `usd3_old_*.json.gz` | `match` | 13 |
| `usd3_new_*.json.gz` | `match` | 16 |
| `susd3_old_*.json.gz` | `match` | 8 |
| `susd3_new_*.json.gz` | `null` (no verified match) | — |

Between them these cover both branches of the storage verdict:

- **USD3 → COMPATIBLE.** Slots 0–62 keep their types, three new mappings/uints take
  slots 63–65, and the reserved gap moves from `uint256[40]` at slot 63 to
  `uint256[37]` at slot 66. It also exercises the two things that must *not* be
  read as conflicts: four variables renamed to `__deprecated_*` at unchanged slots,
  and `morphoCredit`'s compiler type id changing (`t_contract(IMorpho)6874` →
  `…6876`) for an identical type.
- **sUSD3 → UNKNOWN.** The new implementation is verified on Etherscan but absent
  from Sourcify, which returns HTTP 200 with `match: null` — one-sided coverage
  must never produce a compatibility verdict.
