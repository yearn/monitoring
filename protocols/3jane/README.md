# 3Jane USD3/sUSD3 Monitoring

## What it monitors

3Jane is a credit-based money market on Ethereum (modified Morpho Blue fork) with unsecured lending. USD3 is the senior tranche ERC-4626 vault backed by USDC deposits. sUSD3 is the junior (first-loss) tranche created by staking USD3.

- **PPS (Price Per Share):** `convertToAssets(1e6)` on USD3 and sUSD3 vs cached prior run. Alerts on any decrease — indicates loan markdowns or defaults (critical since loans are unsecured).
- **TVL (Total Value Locked):** `totalAssets()` on both vaults vs cached prior run. Alerts when absolute change is **≥15%**.
- **Junior Buffer Ratio:** USD3 held by sUSD3, valued in USDC, as a percentage of deployed credit (`getMarketLiquidity().totalBorrowAssets` converted from waUSDC to USDC). Alerts below **15%** — thin first-loss coverage puts the senior tranche at risk. Deduped: re-alerts only when the ratio drops below the last alerted value; recovery above 15% re-arms. This matches the 3Jane backing UI's `sUSD3 / Deployed` loss-buffer metric.
- **USD3 OC:** Deployed credit divided by senior at-risk credit after sUSD3 absorbs first loss: `Deployed / (Deployed - sUSD3)`. Alerts below the **111%** target as HIGH and below **106%** as CRITICAL. Deduped: re-alerts only when OC drops below the last alerted value (e.g. crossing into critical); recovery above 111% re-arms. This excludes indirect enhancement from underlying credit-line assets and warehouse equity slices.
- **Insurance Fund:** Tracks the fund's raw waUSDC share balance and alerts when an outflow is worth **≥$50k USDC**. Caching shares instead of asset value prevents waUSDC yield from masking withdrawals.
- **Withdraw Liquidity:** `availableWithdrawLimit()` on the USD3 vault. Alerts when it falls below **$4M** — low withdraw liquidity means senior-tranche withdrawals may queue or stall. Deduped: re-alerts only when the limit drops below the last alerted value; recovery above $4M re-arms.
- **Vault Shutdown:** `isShutdown()` on both vaults. Alert-once when either vault enters emergency shutdown.
- **Debt Cap:** `ProtocolConfig.getDebtCap()` vs cached prior. Alerts on any change — signals governance scaling the protocol up or down.
- **Nominal sUSD3 Backing Floor:** `ProtocolConfig.config(keccak256("SUSD3_NOMINAL_BACKING_FLOOR"))` vs cached prior. Alerts on any change (governance lever). Separate alert-once when the floor exceeds sUSD3's USD3 holdings valued in USDC — sUSD3 redemptions can be blocked while floor > backing.
- **Protocol Pause:** `ProtocolConfig.config(keccak256("IS_PAUSED"))`. Alert-once on transition to true. Distinct from per-vault `isShutdown()` — pauses the underlying credit market.
- **Borrower Default Watch:** optional Envio-backed borrower default risk feed. The Envio indexer maintains `ThreeJaneBorrowerMarket` rows from MorphoCredit events, and the monitor computes the current delinquent/default status at runtime. Alerts are **MEDIUM only** and deduped per borrower/cycle/default milestone.
- **Proof of Solvency:** [Accountable](https://accountable.3jane.xyz/) collateral ratio (reserves / liabilities). Alerts **CRITICAL below 95%** and **HIGH below 99%**, plus freshness and availability alerts. See [Proof of Solvency](#proof-of-solvency) below.

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
| Borrower delinquent/default watch | New milestone: delinquent, ≤14d, ≤7d, ≤3d, ≤1d, default | MEDIUM |
| Accountable collateral ratio | < 95% for 2 consecutive runs (band transition) | CRITICAL |
| Accountable collateral ratio | < 99% (band transition) | HIGH |
| Accountable feed stale | Short cadence >2 periods; long cadence >1 period (alert-once) | MEDIUM |
| Accountable feed unavailable | One exhausted retrieval cycle (alert-once until recovery) | HIGH |
| Monitoring run failure | Uncaught exception in `main()` | LOW |

## Cache Freshness

TVL delta baselines expire after 3 hours and initialize from the next valid observation. Current-state and threshold
dedupe for junior buffer, USD3 OC, withdraw liquidity, vault shutdown, nominal-floor breach, and protocol pause is
re-armed after the same monitoring gap. PPS, insurance-fund, governance-value, and borrower-event history does not
expire.

## Borrower default watch

Set `ENVIO_GRAPHQL_URL` to the 3Jane Envio GraphQL endpoint to enable proactive borrower monitoring. Without this env var, the borrower default watch is skipped and all other 3Jane checks continue normally.

Borrowers move through repayment states based on the active repayment obligation:

- `Current`: no unpaid obligation, or the payment cycle is still open.
- `GracePeriod`: the cycle ended and `amountDue > 0`, but the borrower is still inside the grace window. This does not alert.
- `Delinquent`: the grace window has passed and `amountDue > 0`, but the default timestamp has not been reached yet. This is the proactive warning period, and the monitor alerts at `delinquent`, `14d`, `7d`, `3d`, and `1d` buckets.
- `Default`: the default timestamp has passed, or the protocol emitted `DefaultStarted`. The monitor sends a MEDIUM alert and includes how long the borrower has been defaulted.

By default, `defaultAt = cycleEnd + 7 days grace + 23 days delinquency`. These windows come from `gracePeriod` and `delinquencyPeriod` on the indexed borrower row.

The monitor expects Envio to expose a `ThreeJaneBorrowerMarket` entity with at least:

| Field | Purpose |
|-------|---------|
| `marketId` | MorphoCredit market id (`bytes32`) |
| `borrower` | Borrower address |
| `credit` | Latest indexed credit line |
| `amountDue` | Latest indexed repayment amount due |
| `cycleId` | Payment cycle id for the current obligation |
| `cycleEnd` | Indexed cycle end timestamp |
| `endingBalance` | Borrower balance at cycle close |
| `gracePeriod` | Grace period in seconds |
| `delinquencyPeriod` | Delinquency period in seconds |
| `defaultAt` | Event-derived default timestamp |
| `defaultStarted` | Whether `DefaultStarted` has been emitted for the borrower |
| `settled` | Whether the account was settled and should be skipped |
| `lastSeenBlock` | Ordering/pagination |

The indexer should populate/update that entity from `SetCreditLine`, `Borrow`, `Repay`, `PaymentCycleCreated`, `RepaymentObligationPosted`, `RepaymentTracked`, `DefaultStarted`, `DefaultCleared`, and `AccountSettled` events on `MorphoCredit`.

The current countdown and alert bucket are intentionally computed in this monitoring script, not in Envio, because they depend on wall-clock time. Grace and delinquency windows default to 7 days and 23 days respectively in the indexer, and can be overridden there with `THREE_JANE_GRACE_PERIOD_SECONDS` and `THREE_JANE_DELINQUENCY_PERIOD_SECONDS`.

## Proof of Solvency

[Accountable](https://docs.accountable.capital/accountable-documentation/proof-of-solvency) publishes a TEE-attested Proof of Solvency dashboard for 3Jane (feed id `100000026`). The human-readable UI is at `https://accountable.3jane.xyz/` (override with `THREE_JANE_ACCOUNTABLE_MESSAGE_URL`); the JSON report is at `https://accountable.3jane.xyz/dashboard` (override with `THREE_JANE_ACCOUNTABLE_URL`). No API key is required.

The client lives in [`utils/accountable.py`](../../utils/accountable.py) and is keyed by data feed id (DFID), so other Accountable feeds can be added without a rewrite. The request is URL/type-based and neither sends nor echoes the DFID, so feed identity is bound explicitly in config.

### Ratio is recomputed, not read

The API rounds `collateralization` to six decimals. Near the alert boundary that is a missed-critical-alert path: a true ratio of `0.9499996` would present as `0.95` and pass a `< 0.95` test. The monitor therefore computes the ratio from `total_reserves / total_supply` at full precision and uses the reported field only as a consistency cross-check (tolerance ≥1e-6, since the server's own rounding sets the floor).

`net` and `collateralization` are defined against *liabilities*, which equal `total_supply` only for a USD-pegged feed. The client asserts `total_supply.fx == 1` when the field is present. The live response currently omits it, so that path independently derives liabilities from `total_reserves - net` and requires them to match raw supply; a non-pegged feed still fails loudly instead of silently comparing against the wrong denominator.

### Freshness is per source, not global

A fresh aggregate timestamp does not prove every input is fresh, and this matters more than usual here: `reserves_split` is essentially all "Morpho Credit", of which the bulk is off-chain loan receivables priced by manually uploaded document reports. Those routinely run past their declared cadence.

The aggregate report and each required source use their declared cadence. Cadences of one hour or less get one missed-period allowance and become stale after two periods; longer cadences become stale as soon as the first expected update is late. The aggregate cadence comes from `reserves.interval`; source cadences come from each source's `frequency`. This means `15 MIN` becomes stale after 30 minutes, hourly after 2 hours, daily after 24 hours, and weekly after 7 days. A source whose `lastUpdated` is in the future is treated as unusable rather than clamped to "fresh", which would defeat the check. Unknown additional sources with an unrecognised cadence are skipped rather than flagged, so a schema addition on Accountable's side cannot spuriously page us.

The 3Jane dashboard UI declares `Slope - Forward Flows` as weekly, while older `/dashboard` JSON responses reported it as daily. The feed configuration therefore binds that source to `WEEKLY`; stale alerts display the effective cadence used by the monitor.

The four known 3Jane sources are required, and a missing or malformed freshness record for one of them makes the feed **stale**, not unavailable. Freshness can no longer be established, but the collateral ratio itself is unaffected — so the report is still returned and the sub-95% check still runs. An upstream source rename degrades the feed to a MEDIUM staleness alert; it cannot silently disable the CRITICAL solvency check.

### Ratio alerts

HIGH fires once when the ratio drops below 99%; CRITICAL fires once when it stays below 95% for **two consecutive, newer reports**. Each severity stays quiet until the ratio recovers above its threshold. Re-polling a frozen report cannot confirm CRITICAL, and an unavailable run resets partial confirmation. A single sub-95% reading is reported as HIGH so it stays visible without escalating on what is more likely a stale document-report refresh.

The 95%/99% bands are temporary test thresholds while Accountable's report excludes 3Jane idle funds. Recalibrate both thresholds when idle funds are included in the reported reserve totals.

### No emergency dispatch

Accountable alerts are sent with protocol key `3jane-accountable` and channel `3jane`. They reach the normal 3Jane Telegram channel at full severity, but the protocol key is deliberately **absent** from `utils.dispatch.DISPATCHABLE_PROTOCOLS`, so a CRITICAL here cannot trigger the emergency cap-zeroing webhook.

This is intentional for v1: the live collateral margin is only a few basis points, and the feed's noise profile needs a burn-in period before it should be allowed to drive automated action. Revisit once there is enough operating history — see issue #327.

## Alert dispatch

Alerts use the structured `send_alert` path. HIGH and CRITICAL alerts invoke the default emergency-dispatch hook after Telegram delivery, and `3jane` is enabled in `utils.dispatch.DISPATCHABLE_PROTOCOLS`.

The sender posts a signed `emergency_withdrawal` webhook using protocol key `3jane`. Dispatch requires `LIQUIDITY_WEBHOOK_SECRET`, is skipped in `LOG_LEVEL=DEBUG`, and has a 60-minute per-protocol cooldown. The receiving liquidity-monitoring deployment must independently map `3jane` to the vaults, collateral names, and markets whose caps should be zeroed.

Only HIGH and CRITICAL alerts dispatch. LOW and MEDIUM alerts—including insurance-fund outflows—remain Telegram/database alerts only.

## Governance

[Internal timelock monitoring](../timelock/README.md) covers CallScheduled events from the [3Jane 24-hour timelock](https://etherscan.io/address/0x1dccd4628d48a50c1a7adea3848bcc869f08f8c2) and [7-day upgrade timelock](https://etherscan.io/address/0x3d3c41419ab401cd25055e8f9421d7d96d887885) on Mainnet.

Those alerts carry a 3Jane `Protocol Context` section built by
[`utils/llm/threejane_context.py`](../../utils/llm/threejane_context.py):

- `bytes32` arguments are reversed to their `keccak256` pre-image, so a `setConfig` call names the parameter (`MAX_LTV`, `IS_PAUSED`, `DEBT_CAP`, …) and a `grantRole` names the role, each with what it controls. `ProtocolConfig` keys also carry the value stored on-chain right now.
- `RewardsDistributor` calls carry the distribution mode (`useMint`), whether the distributor holds `MINTER_ROLE` on JANE, JANE supply and whether transfers are globally enabled, `maxClaimable` / `totalClaimed` / outstanding, the current `merkleRoot` and epoch, the emissions stored for the three preceding epochs, and how the proposed allocation compares to the epoch before it.
- A key that caps a quantity is rendered next to that quantity — `USD3_SUPPLY_CAP` alongside USD3 `totalAssets`, batched into the same request — so a ceiling raise reads as slack or as unblocking deposits. Register more in `_USAGE_READS`, but only where both sides are denominated the same way.

Add a key or role to `_HASHED_LABELS` in that module when 3Jane introduces one; the hash is derived from the name, so the table cannot drift.

## Running

```bash
uv run 3jane/main.py
```

## Frequency

Runs hourly via the [monitoring runner](../automation/jobs.yaml).
