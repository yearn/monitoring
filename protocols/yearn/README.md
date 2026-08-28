# Yearn Monitoring

This folder contains monitoring scripts for Yearn vault activity, Safe multisig queues, and timelock operations.

## Lender-Borrower Risk

The script `yearn/lender_borrower.py` monitors the active Katana Morpho `vbWBTC/yvUSDC` lender-borrower strategy. The strategy deposits vbWBTC as Morpho collateral, borrows vbUSDC, and lends the borrowed vbUSDC into the Yearn vbUSDC vault.

### Checks

1. **Liquidation risk**: reproduces the strategy warning LTV from Morpho's LLTV and `warningLTVMultiplier()`, then alerts when `getCurrentLTV()` exceeds it. The displayed vbWBTC and vbUSDC prices come from the strategy's Morpho and USD oracles. The borrow-token USD feed must have updated within 26 hours. Runs every 30 minutes.
2. **Net spread**: derives Morpho's instantaneous borrow APR from the adaptive IRM's window-average rate and subtracts it from the lender vault APR returned by Yearn's APR oracle. A medium alert fires after at least three samples when the rolling 24-hour average is below `-1%`. A zero lender APR is treated as unavailable data, alerts, and is not stored as a rate sample. Runs every six hours.
3. **Debt coverage**: compares `balanceOfLentAssets() + balanceOfBorrowToken()` with `balanceOfDebt()`. A medium alert fires when the deficit is both at least 10 basis points of debt and worth at least $100. Runs every six hours with the net-spread check.

All breach, unavailable-data, and monitor-error alerts use `MEDIUM` severity and route to the internal curation Telegram channel, falling back to the Yearn channel when curation is not configured. MEDIUM sends Telegram without invoking the HIGH/CRITICAL emergency-dispatch hook. Persistent breaches and errors are deduplicated and reminded once per 24 hours. The monitor is read-only and does not initiate deleveraging.

### Usage

```bash
uv run protocols/yearn/lender_borrower.py --checks=ltv --dry-run
uv run protocols/yearn/lender_borrower.py --checks=rates-and-coverage --dry-run
```

Omit `--dry-run` to persist rate samples and send configured alerts.

## Large Flows

The script `yearn/alert_large_flows.py` checks recent deposit and withdrawal events and sends a Telegram alert when a single flow exceeds a USD threshold. It runs hourly via the [monitoring runner](../automation/jobs.yaml).

### Data Sources

- **Events**: Envio indexer GraphQL API (configurable via `ENVIO_GRAPHQL_URL`).
- **Pricing**: DeFiLlama token prices for non-stables.
- **Fallback**: On-chain `totalSupply()` via ERC20 ABI when pricing fails.

### Alerts

An alert is emitted when a single deposit or withdrawal for a tracked vault is greater than the configured USD threshold (default: `500,000`). Katana withdrawals use a lower fixed threshold of `50,000`. For stables, USD value is assumed to be the raw amount. For non-stables, if pricing fails, an alert triggers when the flow is >= 10% of the vault totalSupply. Alerts are sent in chronological order by block number and include vault and tx links.

### Caching

The script stores the last alerted transaction hash in `cache-id.txt` (key: `YEARN_LARGE_FLOW_LAST_TX`) to avoid duplicate alerts between hourly runs.

### Usage

```bash
uv run yearn/alert_large_flows.py
```

Optional flags:

- `--threshold-usd` (default: `500000`)
- `--limit` (default: `100`)
- `--since-seconds` (default: `7200`)
- `--chain-ids` (default: all vault chain IDs — `1,8453,42161,747474`)
- `--no-cache` (disable caching)

## Small Parent Vault Flows

The script `yearn/alert_small_parent_flows.py` alerts on every positive deposit or withdrawal strictly below 10,000 normalized underlying-token units for an active Yearn v3 parent vault. The comparison is in token units, not USD: for example, both 9,999 USDC and 9,999 WETH qualify.

### Data Sources

- **Parent vault discovery**: Kong GraphQL, filtered to Yearn v3 `vaultType: 1` vaults and excluding retired or hidden entries.
- **Flow events**: Envio `Deposit` and `Withdraw` entities. Alerts include the ERC-4626 owner and sender, the transaction initiator, and the asset receiver for withdrawals.
- **Token decimals**: the parent vault's underlying asset metadata from Kong.

Deposits and withdrawals are processed with independent per-chain `(blockNumber, logIndex)` cursors stored in the monitoring database. A cursor advances only after an event is successfully evaluated and, when applicable, delivered to Telegram. A new deployment starts each stream with a two-hour lookback.

### Usage

```bash
uv run protocols/yearn/alert_small_parent_flows.py
```

Optional flags:

- `--threshold-units` (default: `10000`)
- `--lookback-seconds` (default: `7200`, used only before a chain cursor exists)
- `--page-size` (default: `1000`)
- `--chain-ids` (default: `1,10,8453,42161,137,747474`)

=======

## Shadow Debt Check

The script `yearn/check_shadow_debt.py` detects "shadow debt" issues in Yearn v3 vaults - when strategies have allocated debt but are NOT in the vault's default queue. This causes APR oracle calculations to be incomplete.

### The Problem

The `AprOracle.getWeightedAverageApr()` function only loops through strategies in the default queue:

```solidity
address[] memory strategies = IVault(_vault).get_default_queue();
```

If a vault has active strategies with debt that are NOT in this queue, the weighted average APR calculation will:
- **Miss these strategies** completely
- Report an **incomplete APR** (likely understated)
- Cause vault depositors to see **inaccurate APR**

### How It Works

For each vault on each supported chain (Mainnet, Polygon, Base, Arbitrum, Katana):

1. Fetches vault data from the [Kong API](https://kong.yearn.fi/api/gql), including all known strategies
2. Queries the vault's default queue via `get_default_queue()`
3. Batch-queries `strategies(address)` for each known strategy to get debt allocation
4. Identifies strategies with `current_debt > 0` that are **not** in the default queue
5. Alerts if any "shadow debt" is detected

### Alerts

A Telegram alert is sent when shadow debt is detected, including:
- Vault address and symbol
- Number of strategies with shadow debt
- Amount of shadow debt per strategy
- Percentage of total vault debt that is "in shadow"
- Links to vault and strategy addresses on block explorers

Example alert format:
```
🌑 Shadow Debt Alert
Found 1 vault(s) with shadow debt affecting 2 strateg(ies)

Mainnet
  • 0xbe53a109... (USDC): 2 strateg(ies) with 1.5M debt (15% of total)
    - 0x1234abcd...: 1.0M
    - 0x5678efgh...: 500K

⚠️ Impact: APR oracle calculations will be incomplete for these vaults
```

### Configuration

The script has a minimum debt threshold (default: 1 token) to avoid alerting on dust amounts. This threshold is automatically scaled based on each vault's decimal precision (e.g., 1 USDC for 6-decimal vaults, 1 WETH for 18-decimal vaults). This can be adjusted via the `--min-debt-threshold` flag.

=======

## Stuck TKS Trigger Check

The script `yearn/check_stuck_triggers.py` monitors the CommonReportTrigger contract to detect when strategy or vault triggers have been stuck in the "true" state for over 24 hours, which indicates potential keeper service issues or health check failures.

### The Problem

The CommonReportTrigger contract (`0xf8dF17a35c88AbB25e83C92f9D293B4368b9D52D`) determines when strategies and vaults should execute report or tend operations. If a trigger returns `true` but the operation isn't executed for an extended period (>24 hours), it indicates:

- **Keeper service not executing** - The automated keeper may be down or misconfigured
- **Health check failures** - The strategy's health check may be preventing execution
- **Gas prices too high** - Network fees may be making execution unprofitable
- **Configuration issues** - Strategy or vault settings may be preventing execution

### How It Works

For each supported chain (Mainnet, Polygon, Base, Arbitrum, Katana):

1. Fetches all v3 vaults and default-queue strategies from the [Kong API](https://kong.yearn.fi/api/gql)
2. Batch-queries the CommonReportTrigger contract for:
   - `vaultReportTrigger(vault, strategy)` - For all vault/strategy pairs
   - `strategyReportTrigger(strategy)` - For standalone strategies
   - `strategyTendTrigger(strategy)` - For tend operations
3. Stores trigger states with timestamps in a JSON cache file
4. Compares current state with cached state to track how long triggers have been true
5. Alerts when any trigger has been stuck for >24 hours (configurable threshold)

### Data Sources

- **On-chain RPC calls**: Queries CommonReportTrigger contract functions directly
- **Kong API**: Fetches vault and strategy addresses to monitor
- **Cache file**: JSON file (`tks-trigger-cache.json`) tracks trigger states over time

### Alerts

A Telegram alert is sent when stuck triggers are detected, including:
- Chain and number of stuck triggers
- Trigger type (vault report, strategy report, or strategy tend)
- How long the trigger has been stuck
- Vault and strategy addresses with block explorer links
- Possible causes for investigation

Example alert format:
```
⚠️ TKS Trigger Alert
Found 2 trigger(s) stuck for >24 hours

Mainnet (2 triggers)
  • Vault Report: stuck for 25.3 hours
    Vault: 0xbe53a109... (link)
    Strategy: 0x1234abcd... (link)
  • Strategy Report: stuck for 26.1 hours
    Strategy: 0x5678efgh... (link)

🔍 Possible causes:
  • Keeper service not executing
  • Health check failures
  • Gas prices too high
  • Strategy configuration issues
```

### Cache Management

The script maintains a JSON cache file that tracks:
- Whether each trigger is currently true/false
- When the trigger first became true (`first_seen`)
- When the trigger was last checked (`last_checked`)
- The reason returned by the trigger (if available)

Triggers are removed from the cache once they return to `false`, ensuring only active issues are tracked.

### Usage

```bash
uv run yearn/check_shadow_debt.py
```

Optional flags:

- `--chains` (default: `MAINNET,POLYGON,BASE,ARBITRUM,KATANA`) - Comma-separated chain names
- `--min-debt-threshold` (default: `1`) - Minimum debt in tokens to alert on (scaled per vault by decimals)

=======

### Usage

```bash
uv run yearn/check_stuck_triggers.py
```

Optional flags:

- `--threshold-hours` (default: `24.0`) - Minimum hours a trigger must be stuck to alert
- `--chains` (default: `MAINNET,POLYGON,BASE,ARBITRUM,KATANA`) - Comma-separated chain names
- `--cache-file` (default: `tks-trigger-cache.json`) - Path to cache file
- `--include-strategies` - Comma-separated list of standalone strategy addresses to monitor

### Examples

Check all chains with default 24-hour threshold:
```bash
uv run yearn/check_stuck_triggers.py
```

Check only Mainnet with 12-hour threshold:
```bash
uv run yearn/check_stuck_triggers.py --chains MAINNET --threshold-hours 12
```

Monitor specific standalone strategies:
```bash
uv run yearn/check_stuck_triggers.py --include-strategies 0x1234...,0x5678...
```

=======

## Safe Multisig Monitoring

Yearn Safe multisigs are monitored via the shared [Safe monitoring script](../safe/main.py). The script polls the [Safe Transaction Service](https://docs.safe.global/core-api/transaction-service-reference) for queued transactions and sends Telegram alerts when unexpected pending txs appear. It runs every 10 minutes via the [monitoring runner](../automation/jobs.yaml).

Yearn multisig config lives in [`safe/addresses.py`](../safe/addresses.py) under `YEARN_MULTISIGS`. The same workflow also monitors non-Yearn protocol multisigs (LIDO, AAVE, etc.) configured in `ALL_SAFE_ADDRESSES`.

### How It Works

For each configured Safe on each chain:

1. Fetches unexecuted multisig transactions from the Safe Transaction Service API.
2. Filters out already-alerted nonces (cached in `nonces.txt`) and dead slots where `nonce < safe.currentNonce`.
3. For Yearn safes, skips txs proposed by known bot EOAs (see Expected Proposers below).
4. Sends a Telegram alert with Safe URL, target contract, nonce, and an optional AI calldata explanation.

=======

## Timelock Monitoring

Yearn TimelockController contracts are monitored across 6 chains via the shared [timelock monitoring script](../timelock/README.md). Alerts are routed to the `YEARN` Telegram channel.

### Monitored Addresses

All chains use the same contract address: `0x88ba032be87d5ef1fbe87336b7090767f367bf73`

| Chain | Explorer |
|-------|----------|
| Mainnet | [etherscan.io](https://etherscan.io/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73) |
| Base | [basescan.org](https://basescan.org/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73) |
| Arbitrum | [arbiscan.io](https://arbiscan.io/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73) |
| Polygon | [polygonscan.com](https://polygonscan.com/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73) |
| Katana | [katanascan.com](https://katanascan.com/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73) |
| Optimism | [optimistic.etherscan.io](https://optimistic.etherscan.io/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73) |

=======

## Indexer Freshness

The script `yearn/check_indexer_freshness.py` watches the [Envio indexer](https://github.com/chain-events/yearn-indexing-test) that feeds the large-flows, timelock and 3jane borrower monitors. It runs hourly, first in the [hourly profile](../../automation/jobs.yaml).

An indexer stall is invisible to the monitors that depend on it: GraphQL keeps answering, it just stops returning new rows, so an outage looks exactly like a quiet hour. This check makes the silence loud.

### How It Works

1. Queries `chain_metadata` at `ENVIO_GRAPHQL_URL` for each chain's `latest_processed_block`.
2. Fetches that block's timestamp via `ChainManager` and compares it to wall-clock time.
3. Alerts when a chain's newest indexed block is older than `--max-lag-minutes` (default `60`), or when an expected chain reports no sync state at all.

Step 2 is what makes the check trustworthy. Envio parks `chain_metadata.block_height` at the last processed block once a chain looks caught up, so a stalled indexer keeps reporting itself as zero blocks behind — the same trap called out in the indexer's own [monitoring dashboard](https://envio-monitoring.yearn.dev/).

Step 3 covers the inverse trap: an empty result set is not good news. If a chain drops out of the indexer's config, or comes back from a restart with no processed block, it simply stops appearing in `chain_metadata` — and a check that only looks at what it was given would report every remaining chain fresh while that chain's monitors sit blind. `EXPECTED_CHAINS` is therefore the authority on what must be present, and anything absent from it alerts.

`EXPECTED_CHAINS` lists the chains whose indexed events feed monitors here (Mainnet, Optimism, Polygon, Base, Arbitrum, Katana). It is deliberately spelled out rather than derived from the `Chain` enum, so adding an enum member for an unrelated protocol doesn't start alerting that the indexer is missing a chain it was never asked to index — **add a chain here when its events start feeding a monitor.** The indexer also covers Gnosis and Berachain, which nothing here reads from; those are logged and skipped. A chain whose RPC is unreachable is skipped too rather than alerted on: a broken provider is not a stale indexer.

### Alerts

This monitor only ever reports Envio indexer problems, so all of its alerts go to the Envio chat (`TELEGRAM_CHAT_ID_ENVIO`) labelled `[yearn]`, alongside the other indexer failures (large flows, timelock). Every other yearn monitor's operational error still goes to the errors channel. If `TELEGRAM_CHAT_ID_ENVIO` is unset these fall back to the errors channel, and from there to the protocol's own chat.

- **Stale or missing chains** — one message listing every lagging chain with its lag and last indexed block, plus every expected chain the indexer reported no sync state for.
- **Indexer unavailable** — the GraphQL endpoint is unset, unreachable, returned errors, or reported no chains. Sent on every run for as long as it lasts.
- **Recovered** — sent once when a previously alerting chain catches up.

A re-sync can run for days, so each chain alerts on the way into trouble and then at most once per `--alert-cooldown-hours` (default `6`) instead of every hourly run. The cooldown is tracked per chain, so one lagging chain never suppresses another's first alert. The last-alert timestamp is cached under `YEARN_INDEXER_STALE_ALERT_<chain_id>`.

### Usage

```bash
uv run protocols/yearn/check_indexer_freshness.py
```

Optional flags (each also settable via env):

- `--max-lag-minutes` (default `60`, env `INDEXER_MAX_LAG_MINUTES`)
- `--alert-cooldown-hours` (default `6`, env `INDEXER_ALERT_COOLDOWN_HOURS`)
