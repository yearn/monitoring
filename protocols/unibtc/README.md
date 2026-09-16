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
| Reserves below supply | Chainlink PoR `latestRoundData().answer` / Bedrock API `data.total_supply` | < 100% | CRITICAL |
| Reserves thin | Same ratio | < 101% | HIGH |
| PoR stale | `latestRoundData().updatedAt` | Older than the live Vault `feederHeartbeat`, capped at 86,400s (Vault `mint()` reverts) | HIGH |
| Supply feeder wrong | Feeder `totalTokenSupply()` / API `total_supply` ratio | Ratio moves > 5% off its anchor | HIGH |
| Supply feeder stale | Feeder `totalTokenSupply()` | Unchanged for 48h (normally updates daily) | HIGH |
| Redemptions underfunded | Router `tokenDebts(WBTC)` vs WBTC `balanceOf(Vault)` | Uncleared > Vault WBTC for > 24h and growing | HIGH |
| Peg | DeFiLlama uniBTC/USD (`coingecko:universal-btc`, fallback: Ethereum token) divided by Ethereum WBTC/USD | < 0.98 WBTC per uniBTC | HIGH |

PoR-staleness, feeder-ratio, feeder-stale, and peg alerts fire once while the condition holds and re-arm on recovery. Pause and reserve-gate alerts are keyed on *which* components are flagged, so a second tampered field or a newly paused component re-alerts instead of being hidden by the first alert. PoR coverage alerts on entering a worse band (HIGH then CRITICAL). Minting alerts fire once per mint and repeat only once supply grows by another full threshold; a missing baseline re-arms the marker, so a stale one cannot suppress a real mint after a polling gap.

The feeder and the Bedrock dashboard cover different chain sets, so their absolute levels differ by a large steady-state factor (~0.85 at the time of writing) — only a *move* in that ratio signals a wrong or hijacked feeder. Comparison is against an anchor taken on first run and re-taken at most weekly, and only from a reading inside the band. Re-anchoring to every in-band reading instead would let the ratio ratchet: repeated sub-threshold steps each move the anchor, accumulating unlimited drift without ever tripping.

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

Reserve API: `https://affiliate-api-eosin.vercel.app/api/v1/third/stats/unibtc`

## Gaps from skipping events

- Detection comes up to an hour after an EOA action or mint, not in real time.
- A supply jump shows that minting happened, not which minter did it.
- Safe transactions executed without being queued in the Safe tx service are not seen beforehand; role grants made that way show up only through the supply-delta check.

## Usage

```bash
uv run protocols/unibtc/main.py
```

Scheduled hourly via [`automation/jobs.yaml`](../../automation/jobs.yaml).
