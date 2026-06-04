# Yearn Monitoring

This folder contains monitoring scripts for Yearn vault activity, Safe multisig queues, and timelock operations.

## Large Flows

The script `yearn/alert_large_flows.py` checks recent deposit and withdrawal events and sends a Telegram alert when a single flow exceeds a USD threshold. It runs hourly via the [monitoring runner](../automation/jobs.yaml).

### Data Sources

- **Events**: Envio indexer GraphQL API (configurable via `ENVIO_GRAPHQL_URL`).
- **Pricing**: CoinGecko token prices for non-stables (uses `COINGECKO_API_KEY` if provided).
- **Fallback**: On-chain `totalSupply()` via ERC20 ABI when pricing fails.

### Alerts

An alert is emitted when a single deposit or withdrawal for a tracked vault is greater than the configured USD threshold (default: `5,000,000`). For stables, USD value is assumed to be the raw amount. For non-stables, if pricing fails, an alert triggers when the flow is >= 10% of the vault totalSupply. Alerts are sent in chronological order by block number and include vault and tx links.

### Caching

The script stores the last alerted transaction hash in `cache-id.txt` (key: `YEARN_LARGE_FLOW_LAST_TX`) to avoid duplicate alerts between hourly runs.

### Usage

```bash
uv run yearn/alert_large_flows.py
```

Optional flags:

- `--threshold-usd` (default: `5000000`)
- `--limit` (default: `100`)
- `--since-seconds` (default: `7200`)
- `--chain-ids` (default: `1`)
- `--no-cache` (disable caching)

=======

## Endorsed Vault Check

The script `yearn/check_endorsed.py` verifies that all Yearn v3 vaults listed in the yDaemon API are actually endorsed on-chain in the registry contract. It runs weekly via the [monitoring runner](../automation/jobs.yaml).

### How It Works

For each supported chain (Mainnet, Polygon, Base, Arbitrum, Katana):

1. Fetches all v3 vault addresses from the [yDaemon API](https://ydaemon.yearn.fi).
2. Calls `isEndorsed(address)` on the registry contract (`0xd40ecF29e001c76Dcc4cC0D9cd50520CE845B038`).
3. Collects any vault that is listed in yDaemon but **not** endorsed on-chain.

### Alerts

If any unendorsed vaults are found, a Telegram alert is sent to the Yearn group listing each address grouped by chain. If the message exceeds the Telegram character limit, a short summary with a link to the GitHub Actions logs is sent instead.

### Usage

```bash
uv run yearn/check_endorsed.py
```

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

1. Fetches vault data from the [yDaemon API](https://ydaemon.yearn.fi), including all known strategies
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

1. Fetches all v3 vaults and their strategies from the [yDaemon API](https://ydaemon.yearn.fi)
2. Batch-queries the CommonReportTrigger contract for:
   - `vaultReportTrigger(vault, strategy)` - For all vault/strategy pairs
   - `strategyReportTrigger(strategy)` - For standalone strategies
   - `strategyTendTrigger(strategy)` - For tend operations
3. Stores trigger states with timestamps in a JSON cache file
4. Compares current state with cached state to track how long triggers have been true
5. Alerts when any trigger has been stuck for >24 hours (configurable threshold)

### Data Sources

- **On-chain RPC calls**: Queries CommonReportTrigger contract functions directly
- **yDaemon API**: Fetches vault and strategy addresses to monitor
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

