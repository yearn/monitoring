# Bedrock uniBTC

Hourly state polling for [Bedrock uniBTC](https://www.bedrock.technology/) on Ethereum. There is no event scanning: detection can lag up to an hour after an EOA action or mint, and a supply jump does not name the minter.

Queued Safe transactions are covered by the [Safe monitor](../safe/main.py). This script polls live state for the paths those queues cannot see — including the single-EOA unbacked-mint path through the legacy withdrawal router.

[Risk assessment](https://github.com/yearn/risk-score/blob/master/reports/report/bedrock-unibtc.md) · [Issue #361](https://github.com/yearn/monitoring/issues/361)

## Why this exists

EOA [`0x3eea50ba10952e5e0dfaa50ecfcc5ab19ad591ef`](https://etherscan.io/address/0x3eea50ba10952e5e0dfaa50ecfcc5ab19ad591ef) is the proxy admin of the legacy withdrawal router, which still holds Vault `OPERATOR_ROLE`. Upgrading that router lets the EOA call `Vault.execute` → `uniBTC.mint` for any amount, with no reserve check. Other operational EOAs can change the reserve gate, supply feeder, and CCIP limits without a Safe transaction.

## What it monitors

| Check | Source | Alert when | Severity |
|---|---|---|---|
| Unexpected minting | uniBTC `totalSupply()` delta | +10 uniBTC in ~1h | CRITICAL |
| Unexpected minting | uniBTC `totalSupply()` delta | +2 uniBTC in ~24h | HIGH |
| Reserve gate | Vault `adequacyRatio`, `chainlinkReserveFeeder`, `uniBTCSupplyFeeder`, `feederHeartbeat` | Any change from 900 / PoR feed / supply feeder / 86400 | CRITICAL |
| Vault or router paused | Vault `outOfService()`, `paused()`; live router `paused()` | Any `true` | HIGH |
| Reserves below supply | Chainlink PoR `latestRoundData().answer` / validated Bedrock API `data.total_supply` | < 100% | CRITICAL |
| Reserves thin | Same ratio | < 101% | HIGH |
| PoR stale | `latestRoundData().updatedAt` | Older than the live Vault `feederHeartbeat`, capped at 86,400s (Vault `mint()` reverts) | HIGH |
| Supply feeder zero | Feeder `totalTokenSupply()` | Returns 0 (Vault reserve check passes for any mint); no API needed | CRITICAL |
| Supply feeder wrong | Feeder `totalTokenSupply()` vs validated API `total_supply` | Gap > 2%; names the chain whose supply matches the gap | HIGH |
| Supply feeder stale | Feeder `totalTokenSupply()` | Unchanged for 48h (normally updates daily) | HIGH |
| Redemptions underfunded | Router `tokenDebts(WBTC)` vs WBTC `balanceOf(Vault)` | Uncleared > Vault WBTC for > 24h and growing | HIGH |
| Peg | DeFiLlama uniBTC/USD (`coingecko:universal-btc`, fallback: Ethereum token) divided by Ethereum WBTC/USD | < 0.985 WBTC per uniBTC (HIGH), < 0.97 (CRITICAL) | HIGH / CRITICAL |

PoR-staleness, feeder-zero, feeder-gap, and feeder-stale alerts fire once while the condition holds and re-arm on recovery. Pause and reserve-gate alerts are keyed on *which* components are flagged, so a second tampered field or a newly paused component re-alerts instead of being hidden by the first alert. PoR coverage and peg alert on entering a worse band (HIGH then CRITICAL); moving to a better band is silent, and recovering fully re-arms. Peg levels come from a year of uniBTC/WBTC prices: median 0.9945, below 0.99 about 9% of the time (routine), below 0.97 only in four stress episodes. Minting alerts fire once per mint and repeat only once supply grows by another full threshold; a missing baseline re-arms the marker, so a stale one cannot suppress a real mint after a polling gap.

A healthy feeder tracks the API total supply (ratio ~1.00 through 2026-09-12). Since 2026-09-13 the updater [`0x2C62803181243Fa99C659DE0d2A0530879a79911`](https://etherscan.io/address/0x2C62803181243Fa99C659DE0d2A0530879a79911) has, on alternating days, written a value about 701 uniBTC low — the BOB chain's supply — so the gap alert names the chain whose supply matches the gap.

## Bedrock reserve API

Total supply across chains has no on-chain or Chainlink source, so it comes from `https://affiliate-api-eosin.vercel.app/api/v1/third/stats/unibtc`. It is the undocumented backend of Bedrock's own dashboard ([app.bedrock.technology](https://app.bedrock.technology) loads it), so it is the issuer's figure rather than independent evidence, and it has been observed dropping a whole chain from `supplies` (BOB on 2026-09-16), understating total supply by ~15%.

An understated total is worse than no data: it inflates PoR coverage and hides a feeder that omits the same chain. Every response is therefore validated, and the PoR-coverage and feeder-gap checks are skipped for the run (with an error message) unless all of these hold:

- `time` is at most 1 hour older than the pinned block.
- Every chain holding meaningful supply is present with a positive value: Ethereum (1), BSC (56), Base (8453), BOB (60808), Berachain (80094) — 99.4% of supply on 2026-09-16.
- The Ethereum entry matches the block-pinned on-chain `totalSupply()` within 1%.
- `total_supply` equals the sum of `supplies` within 0.01 uniBTC. The checks use the total, so the per-chain safeguards above only protect it when the two agree.

The required-chain list is static; a new chain gaining material supply must be added to `API_REQUIRED_CHAINS`.

## Safe monitor

These Safes are registered in [`protocols/safe/addresses.py`](../safe/addresses.py) and polled every 10 minutes:

| Safe | Address | What queued txs reveal |
|---|---|---|
| uniBTC ops Safe (3/5) | [`0xC9dA980fFABbE2bbe15d4734FDae5761B86b5Fc3`](https://etherscan.io/address/0xC9dA980fFABbE2bbe15d4734FDae5761B86b5Fc3) | ProxyAdmin upgrades and ownership; token `MINTER_ROLE` / `DEFAULT_ADMIN_ROLE` / `FREEZER_ROLE`; token freezes (`freezeUsers`, `setFreezeToRecipient`, which emit no events); Vault admin; live router fees/delays/quotas/blacklist; owner/threshold changes |
| Bedrock admin Safe (3/5) | [`0xAeE017052DF6Ac002647229D58B786E380B9721A`](https://etherscan.io/address/0xAeE017052DF6Ac002647229D58B786E380B9721A) | CCIP pool owner actions; BurnProxy / TransferProxy; CCIPPeer and directBTC admin; owner/threshold changes |

## Key contracts

| Contract | Address |
|---|---|
| uniBTC token | [`0x004E9C3EF86bc1ca1f0bB5C7662861Ee93350568`](https://etherscan.io/address/0x004E9C3EF86bc1ca1f0bB5C7662861Ee93350568) |
| uniBTC Vault | [`0x047D41F2544B7F63A8e991aF2068a363d210d6Da`](https://etherscan.io/address/0x047D41F2544B7F63A8e991aF2068a363d210d6Da) |
| Live DelayRedeemRouter | [`0xAA732c9c110A84d090a72da230eAe1E779f89246`](https://etherscan.io/address/0xAA732c9c110A84d090a72da230eAe1E779f89246) |
| Chainlink uniBTC PoR | [`0xc590D9fb8eE78a0909dFF341ccf717000b7b7fF2`](https://etherscan.io/address/0xc590D9fb8eE78a0909dFF341ccf717000b7b7fF2) |
| Supply feeder (`uniBTCRate`) | [`0xE542919E4b281f10b437F947c8Ba224DdfaBc716`](https://etherscan.io/address/0xE542919E4b281f10b437F947c8Ba224DdfaBc716) |

## Usage

```bash
uv run protocols/unibtc/main.py
```

Scheduled hourly via [`automation/jobs.yaml`](../../automation/jobs.yaml).
