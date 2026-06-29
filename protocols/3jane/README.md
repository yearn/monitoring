# 3Jane USD3/sUSD3 Monitoring

## What it monitors

3Jane is a credit-based money market on Ethereum (modified Morpho Blue fork) with unsecured lending. USD3 is the senior tranche ERC-4626 vault backed by USDC deposits. sUSD3 is the junior (first-loss) tranche created by staking USD3.

- **PPS (Price Per Share):** `convertToAssets(1e6)` on USD3 and sUSD3 vs cached prior run. Alerts on any decrease — indicates loan markdowns or defaults (critical since loans are unsecured).
- **TVL (Total Value Locked):** `totalAssets()` on both vaults vs cached prior run. Alerts when absolute change is **≥15%**.
- **Junior Buffer Ratio:** USD3 held by sUSD3, valued in USDC, as a percentage of deployed credit (`getMarketLiquidity().totalBorrowAssets` converted from waUSDC to USDC). Alerts below **15%** — thin first-loss coverage puts the senior tranche at risk. This matches the 3Jane backing UI's `sUSD3 / Deployed` loss-buffer metric.
- **USD3 OC:** Deployed credit divided by senior at-risk credit after sUSD3 absorbs first loss: `Deployed / (Deployed - sUSD3)`. Alerts below the **111%** target as HIGH and below **106%** as CRITICAL. This excludes indirect enhancement from underlying credit-line assets and warehouse equity slices.
- **Insurance Fund:** Tracks the fund's raw waUSDC share balance and alerts when an outflow is worth **≥$50k USDC**. Caching shares instead of asset value prevents waUSDC yield from masking withdrawals.
- **Withdraw Liquidity:** `availableWithdrawLimit()` on the USD3 vault. Alerts when it falls below **$4M** — low withdraw liquidity means senior-tranche withdrawals may queue or stall.
- **Vault Shutdown:** `isShutdown()` on both vaults. Alert-once when either vault enters emergency shutdown.
- **Debt Cap:** `ProtocolConfig.getDebtCap()` vs cached prior. Alerts on any change — signals governance scaling the protocol up or down.
- **Nominal sUSD3 Backing Floor:** `ProtocolConfig.config(keccak256("SUSD3_NOMINAL_BACKING_FLOOR"))` vs cached prior. Alerts on any change (governance lever). Separate alert-once when the floor exceeds sUSD3's USD3 holdings valued in USDC — sUSD3 redemptions can be blocked while floor > backing.
- **Protocol Pause:** `ProtocolConfig.config(keccak256("IS_PAUSED"))`. Alert-once on transition to true. Distinct from per-vault `isShutdown()` — pauses the underlying credit market.

## Key Contracts

| Contract | Address | Purpose |
|----------|---------|---------|
| USD3 Vault | [`0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc`](https://etherscan.io/address/0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc) | Senior tranche ERC-4626 vault |
| sUSD3 Vault | [`0xf689555121e529Ff0463e191F9Bd9d1E496164a7`](https://etherscan.io/address/0xf689555121e529Ff0463e191F9Bd9d1E496164a7) | Junior (first-loss) tranche |
| ProtocolConfig | [`0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E`](https://etherscan.io/address/0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E) | Governance config: debt cap, pause, sUSD3 floor |
| Insurance Fund | [`0x4507B5B23340D248457d955a211C8B0634D29935`](https://etherscan.io/address/0x4507B5B23340D248457d955a211C8B0634D29935) | waUSDC reserve used for debt settlement |

## Alert Thresholds

| Metric | Threshold | Severity |
|--------|-----------|----------|
| USD3 PPS decrease | Any decrease vs cached prior | CRITICAL |
| sUSD3 PPS decrease | Any decrease vs cached prior | HIGH |
| TVL change | ≥15% absolute change vs prior run | LOW |
| Junior buffer ratio | sUSD3 backing < 15% of deployed credit | HIGH |
| USD3 OC low | OC < 111% | HIGH |
| USD3 OC critical | OC < 106% | CRITICAL |
| Insurance fund outflow | ≥$50k USDC since prior run | MEDIUM |
| Withdraw liquidity low | `availableWithdrawLimit()` < $4M | MEDIUM |
| Vault shutdown | `isShutdown()` transitions to true (alert-once) | CRITICAL |
| Debt cap change | Any change to `getDebtCap()` | LOW |
| Nominal backing floor change | Any change to `SUSD3_NOMINAL_BACKING_FLOOR` | MEDIUM |
| Nominal floor breach | Floor > sUSD3 backing valued in USDC (alert-once) | MEDIUM |
| Protocol paused | `IS_PAUSED` transitions to true (alert-once) | CRITICAL |
| Monitoring run failure | Uncaught exception in `main()` | LOW |

## Alert dispatch

Alerts use the structured `send_alert` path. HIGH and CRITICAL alerts invoke the default emergency-dispatch hook after Telegram delivery, and `3jane` is enabled in `utils.dispatch.DISPATCHABLE_PROTOCOLS`.

The sender posts a signed `emergency_withdrawal` webhook using protocol key `3jane`. Dispatch requires `LIQUIDITY_WEBHOOK_SECRET`, is skipped in `LOG_LEVEL=DEBUG`, and has a 60-minute per-protocol cooldown. The receiving liquidity-monitoring deployment must independently map `3jane` to the vaults, collateral names, and markets whose caps should be zeroed.

Only HIGH and CRITICAL alerts dispatch. LOW and MEDIUM alerts—including insurance-fund outflows—remain Telegram/database alerts only.

## Governance

[Internal timelock monitoring](../timelock/README.md) covers CallScheduled events from the [3Jane 24-hour timelock](https://etherscan.io/address/0x1dccd4628d48a50c1a7adea3848bcc869f08f8c2) and [7-day upgrade timelock](https://etherscan.io/address/0x3d3c41419ab401cd25055e8f9421d7d96d887885) on Mainnet.

## Running

```bash
uv run 3jane/main.py
```

## Frequency

Runs hourly via the [monitoring runner](../automation/jobs.yaml).
