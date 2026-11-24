# 3Jane USD3/sUSD3 Monitoring

## What it monitors

3Jane is a credit-based money market on Ethereum (modified Morpho Blue fork) with unsecured lending. USD3 is the senior tranche ERC-4626 vault backed by USDC deposits. sUSD3 is the junior (first-loss) tranche created by staking USD3.

- **PPS (Price Per Share):** `convertToAssets(1e6)` on USD3 and sUSD3 vs cached prior run. Alerts on any decrease — indicates loan markdowns or defaults (critical since loans are unsecured). Emergency dispatch needs a drawdown of at least **0.01%** (`PPS_DISPATCH_MIN_DECREASE_BPS`) from the peak PPS since the last dispatch-eligible alert, so float noise alerts without moving funds while a loss spread over several small drops still dispatches.
- **TVL (Total Value Locked):** `totalAssets()` on both vaults vs cached prior run. Alerts when absolute change is **≥15%**. USD3 TVL is in USDC; sUSD3 TVL is in USD3 and is labelled as such.
- **USD3 Protection (OC + Junior Buffer):** A single consistent protection check derived from sUSD3 backing and deployed credit. OC is `Deployed / (Deployed - sUSD3)` and the equivalent junior buffer is `sUSD3 / Deployed`. Alerts below **111% OC / 9.91% buffer** as HIGH and below **106% OC / 5.66% buffer** as CRITICAL. Both values appear in one alert, eliminating contradictory thresholds for the same first-loss protection. Deduped: while breached, re-alerts only when OC drops at least **0.5 percentage points** (`USD3_OC_REALERT_STEP`) below the last alerted value, or on a crossing below 106% (CRITICAL escalation is never suppressed; after a CRITICAL alert it re-arms only once OC recovers to ≥106.5%, so OC flapping around 106% cannot re-fire CRITICAL every run; that recovery does not lift the baseline, so a rebound followed by a small dip that stays above 106% does not send an extra HIGH); recovery above 111% re-arms. This excludes indirect enhancement from underlying credit-line assets and warehouse equity slices.
- **Insurance Fund:** Tracks the fund's raw waUSDC share balance and alerts when an outflow is worth **≥$50k USDC**. Caching shares instead of asset value prevents waUSDC yield from masking withdrawals.
- **Withdraw Liquidity:** `availableWithdrawLimit()` on the USD3 vault. Alerts when it falls below **$4M** — low withdraw liquidity means senior-tranche withdrawals may queue or stall. Deduped: while breached, re-alerts only when the limit drops at least **$1M** (`WITHDRAW_LIMIT_REALERT_STEP`) below the last alerted value, so a fully utilized market draining dust to $0 alerts once. Falling below **$10k** (`WITHDRAW_LIMIT_DRAINED_THRESHOLD`, effectively drained) always alerts, so a limit last alerted under $1M cannot drain to $0 silently; that re-arms once the limit recovers above $1.01M, without lifting the baseline, so only another drain below $10k re-alerts before the limit clears $4M. Recovery above $4M re-arms.
- **Vault Shutdown:** `isShutdown()` on both vaults, read at the same pinned block as the other onchain values, in its own batch so a failing shutdown read cannot skip the core checks. Alert-once when either vault enters emergency shutdown.
- **Debt Cap:** `ProtocolConfig.getDebtCap()` vs cached prior. The governed value is shown in waUSDC shares alongside its USDC equivalent at the current waUSDC conversion rate. Alerts on any change — signals governance scaling the protocol up or down.
- **Nominal sUSD3 Backing Floor:** `ProtocolConfig.config(keccak256("SUSD3_NOMINAL_BACKING_FLOOR"))` vs cached prior. Alerts on any change (governance lever). Separate alert-once when the floor exceeds sUSD3's USD3 holdings valued in USDC — sUSD3 redemptions can be blocked while floor > backing.
- **Protocol Pause:** `ProtocolConfig.config(keccak256("IS_PAUSED"))`. Alert-once on transition to true. Distinct from per-vault `isShutdown()` — pauses the underlying credit market.
- **Borrower Default Watch:** optional Envio-backed borrower default risk feed. The Envio indexer maintains `ThreeJaneBorrowerMarket` rows from MorphoCredit events, and the monitor computes the current delinquent/default status at runtime. Alerts are **MEDIUM only** and deduped per borrower/cycle/default milestone.

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
| USD3 PPS decrease | Any decrease vs cached prior (dispatch at ≥0.01% drawdown from peak) | CRITICAL |
| sUSD3 PPS decrease | Any decrease vs cached prior (dispatch at ≥0.01% drawdown from peak) | HIGH |
| TVL change | ≥15% absolute change vs prior run | LOW |
| USD3 protection low | OC < 111% (equivalent junior buffer < 9.91%) | HIGH |
| USD3 protection critical | OC < 106% (equivalent junior buffer < 5.66%) | CRITICAL |
| Insurance fund outflow | ≥$50k USDC since prior run | MEDIUM |
| Withdraw liquidity low | `availableWithdrawLimit()` < $4M | MEDIUM |
| Vault shutdown | `isShutdown()` transitions to true (alert-once) | CRITICAL |
| Debt cap change | Any change to `getDebtCap()` | LOW |
| Nominal backing floor change | Any change to `SUSD3_NOMINAL_BACKING_FLOOR` | MEDIUM |
| Nominal floor breach | Floor > sUSD3 backing valued in USDC (alert-once) | MEDIUM |
| Protocol paused | `IS_PAUSED` transitions to true (alert-once) | CRITICAL |
| Borrower delinquent/default watch | New milestone: delinquent, ≤14d, ≤7d, ≤3d, ≤1d, default | MEDIUM |
| DefiLlama Verified source issue | Record marked out of date, stale verification or source data, or required source missing | MEDIUM |
| DefiLlama Verified unavailable | 2 consecutive runs without a usable JSON report | LOW |
| DefiLlama Verified outage continues | 6 consecutive runs without a usable JSON report | MEDIUM |
| Monitoring run failure | Uncaught exception in `main()` | LOW |

## Cache Freshness

TVL delta baselines expire after 3 hours and initialize from the next valid observation. Current-state and threshold
dedupe for USD3 protection, withdraw liquidity, vault shutdown, nominal-floor breach, and protocol pause is
re-armed after the same monitoring gap. PPS, insurance-fund, governance-value, and borrower-event history does not
expire.

## Borrower default watch

Set `ENVIO_GRAPHQL_URL` to the 3Jane Envio GraphQL endpoint to enable proactive borrower monitoring. Without this env var, the borrower default watch is skipped and all other 3Jane checks continue normally.

Borrowers move through repayment states based on the active repayment obligation:

- `Current`: no unpaid obligation, or the payment cycle is still open.
- `GracePeriod`: the cycle ended and `amountDue > 0`, but the borrower is still inside the grace window. This does not alert.
- `Delinquent`: the grace window has passed and `amountDue > 0`, but the default timestamp has not been reached yet. This is the proactive warning period, and the monitor alerts at `delinquent`, `14d`, `7d`, `3d`, and `1d` buckets.
- `Default`: the default timestamp has passed, or the protocol emitted `DefaultStarted`. The monitor sends a MEDIUM alert and includes how long the borrower has been defaulted.

`defaultAt = cycleEnd + GRACE_PERIOD + DELINQUENCY_PERIOD`. The monitor reads both periods directly from `ProtocolConfig` at the same Ethereum block as the core vault snapshot on every run, matching `MorphoCredit.getRepaymentStatus()`. Envio supplies borrower obligations and cycle timestamps, but its stored timing fields are not used for status calculations.

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
| `defaultStarted` | Whether `DefaultStarted` has been emitted for the borrower |
| `settled` | Whether the account was settled and should be skipped |
| `lastSeenBlock` | Ordering/pagination |

The indexer should populate/update that entity from `SetCreditLine`, `Borrow`, `Repay`, `PaymentCycleCreated`, `RepaymentObligationPosted`, `RepaymentTracked`, `DefaultStarted`, `DefaultCleared`, and `AccountSettled` events on `MorphoCredit`.

The current countdown and alert bucket are intentionally computed in this monitoring script, not in Envio, because they depend on wall-clock time and governance-controlled timing. This also prevents an indexer environment value from drifting away from the live contract configuration.

## DefiLlama Verified

The monitor reads [DefiLlama's public JSON route](https://defillama.com/api/public/verified/3jane) for the
[3Jane verification record](https://defillama.com/verified/3jane). It checks the protocol slug and
recomputes total principal from the originator rows, with one cent of tolerance for floating-point
rounding. The fixture in [tests/fixtures](../../tests/fixtures/defillama_verified_3jane.json)
is a captured API response with explanatory prose omitted.

This is a partial-scope disclosure. It contains offchain outstanding principal excluding interest,
eligible collateral for warehouse loans, and an Erebor Bank cash balance. The API exposes no
complete reserves and liabilities, borrower addresses or individual loan IDs, or cryptographic
attestation. The 99% and 95% Accountable coverage bands cannot be applied to these figures, and
MorphoCredit total borrow and Envio borrower rows cannot be reconciled to this disclosure.

The monitor uses the following age limits. Cadences are operational estimates based on the
source methodology and observed timestamps. Document ages count UTC calendar days.
DefiLlama publishes only the latest record, so the monitor keeps the last 12 distinct
`reportDate` values per originator in monitor state (`3JANE_DEFILLAMA_VERIFIED_REPORT_DATES`,
a JSON object keyed by originator) and logs each new date with the gap since the previous one.
Use that history to replace these estimates with the observed cadence.

| Source | Cadence + grace | Maximum age |
|--------|-----------------|-------------|
| Verification run (`timestampOnRun`) and Erebor Bank balance | Hourly + 2 hours | 3 hours |
| LendSwift, GigFi, and Slope documents | Weekly + 3 days | 10 days |
| Settle documents | Monthly (up to 31 days) + 7 days | 38 days |

A missing required source, stale timestamp, or provider `state.upToDate: false` sends a MEDIUM
alert when the set of issues changes. A usable report clears the availability failure streak
even if its data is stale. Redirects, non-JSON responses, HTTP failures, invalid JSON or
schema, and unexpected errors while fetching or evaluating the report count as unavailable. Each
run counts once, and an availability alert is marked sent only after Telegram accepts it, so a
failed send retries on the next failed run. A failed source-issue alert retries on the next run
and does not count as a feed outage. Availability alerts stay at LOW and MEDIUM because they
reflect feed health. The feed check runs first, in its own failure
boundary, so it still runs when RPC access fails, and its failures cannot skip the onchain and
Envio checks. Document dates may be up to one calendar day ahead of the UTC verification run,
since documents dated in a time zone east of UTC are not malformed.

The Verified client sends the desktop Firefox user agent with `Accept: application/json`.
Direct Python requests with these headers returned valid JSON. Plain Python requests and curl
received a Cloudflare challenge. The official SDK and published Free and Pro API specs omit
this website route.

To check access from the monitoring VPS without running alert checks, use Python:

```bash
python3 - <<'PROBE'
from urllib.request import Request, urlopen

request = Request(
    "https://defillama.com/api/public/verified/3jane",
    headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:160.0) Gecko/20100101 Firefox/160.0",
    },
)
with urlopen(request, timeout=15) as response:
    print(response.status, response.headers.get("Content-Type"))
PROBE
```

Expect HTTP 200 and an `application/json` content type. If the VPS still receives a challenge,
set `THREE_JANE_DEFILLAMA_VERIFIED_URL` when DefiLlama provides an alternate trusted HTTPS
endpoint serving the same JSON.

## Alert dispatch

Alerts use the structured `send_alert` path. HIGH and CRITICAL alerts invoke the default emergency-dispatch hook before the Telegram send, so a slow or failed Telegram delivery cannot delay or block it, and `3jane` is enabled in `utils.dispatch.DISPATCHABLE_PROTOCOLS`.

The sender posts a signed `emergency_withdrawal` webhook using protocol key `3jane`. Dispatch requires `LIQUIDITY_WEBHOOK_SECRET`, is skipped in `LOG_LEVEL=DEBUG`, and has a 60-minute per-protocol cooldown. The receiving liquidity-monitoring deployment must independently map `3jane` to the vaults, collateral names, and markets whose caps should be zeroed.

Only HIGH and CRITICAL alerts dispatch, and PPS alerts below the 0.01% drawdown gate are sent with dispatch disabled. LOW and MEDIUM alerts—including insurance-fund outflows—remain Telegram/database alerts only.

## Governance

[Internal timelock monitoring](../timelock/README.md) covers CallScheduled events from the [3Jane 24-hour timelock](https://etherscan.io/address/0x1dccd4628d48a50c1a7adea3848bcc869f08f8c2) and [7-day upgrade timelock](https://etherscan.io/address/0x3d3c41419ab401cd25055e8f9421d7d96d887885) on Mainnet.

Those alerts carry a 3Jane `Protocol Context` section built by
[`utils/llm/threejane_context.py`](../../utils/llm/threejane_context.py) and
[`utils/llm/threejane_account_context.py`](../../utils/llm/threejane_account_context.py):

- `bytes32` arguments are reversed to their `keccak256` pre-image, so a `setConfig` call names the parameter (`MAX_LTV`, `IS_PAUSED`, `DEBT_CAP`, …) and a `grantRole` names the role, each with what it controls. `ProtocolConfig` keys also carry the value stored on-chain right now.
- `RewardsDistributor` calls carry the distribution mode (`useMint`), whether the distributor holds `MINTER_ROLE` on JANE, JANE supply and whether transfers are globally enabled, `maxClaimable` / `totalClaimed` / outstanding, the current `merkleRoot` and epoch, the emissions stored for the three preceding epochs, and how the proposed allocation compares to the epoch before it.
- A key that caps a quantity is rendered next to that quantity — `USD3_SUPPLY_CAP` alongside USD3 `totalAssets`, batched into the same request — so a ceiling raise reads as slack or as unblocking deposits. Cap, proposed cap and `totalAssets` render in USDC with the headroom against each, plus how `USD3.availableDepositLimit` enforces the cap and that exempt accounts skip it. Register more in `_USAGE_READS`, but only where both sides are denominated the same way.
- `LCCVault.bounceCommitment` (per-facility vaults behind a beacon proxy) carries the commitment in the vault's funding asset, the user's active commitment and margin, whether the bounce is full or partial, the margin returned, the share of the vault's active commitment it withdraws, and whether it would revert against current state, including the vault-phase reverts during a pending auction or an unsettled capital call.
- `USD3.setSupplyCapExempt` calls are grouped into one section per alert: counts by direction and account type, everything the exemption bypasses (cap headroom, first-deposit minimum, the waUSDC-paused block) and what still applies, and USD3 supply against the cap — each once — then a line per account with its type (EOA, EOA with an EIP-7702 delegation, or contract), the flag change, `ringFenceConduit`, its USD3 balance and its last change from USD3's `SupplyCapExemptUpdated` log. A re-grant of a revoked exemption is named as one. The context also counts the accounts already exempt and how many are ring-fence conduits. Only a revocation that leaves `ringFenceConduit` set is flagged as out of step. The context also states the exemption's documented purpose (protocol-controlled receivers such as LCC vaults), so a grant to a wallet reads as an unlimited per-wallet right past the cap.

Add a key or role to `_HASHED_LABELS` in that module when 3Jane introduces one; the hash is derived from the name, so the table cannot drift.

## Running

```bash
uv run protocols/3jane/main.py
```

## Frequency

Runs hourly via the [monitoring runner](../automation/jobs.yaml).
