# Morpho Monitoring

## Governance

For roles on Morpho vaults, refer to the following [document](https://github.com/morpho-org/metamorpho/blob/main/README.md).

Morpho governance monitoring is defined in the [Python script](./governance.py) that is executed daily via the [monitoring runner](../automation/jobs.yaml) because minimum timelock is 3 days to get vaults whitelisted, and 1 day is mimimum value in the contract.

The script checks if there are any new values pending in the timelock for a given vault. It detects the following changes:

- Changing timelock value, minimal values is 1 day.
- Changing guardian address.
- Changing supply caps, only to higher value than the current one, for both supply and withdraw markets.
- Removing of a market from the vault.

### How to Add a New Vault

Add a `VaultConfig` to `VAULTS_V1_BY_CHAIN` in [config.py](./config.py). Every configured V1 vault receives both market and governance monitoring.

## Vaults & Markets

Morpho Vaults consist of multiple markets, each defining key parameters such as LTV, interest rate models, and oracle data.

Market monitoring is configured through [config.py](./config.py), while shared market and liquidity policy lives in [risk.py](./risk.py). The script fetches all markets for each vault and checks the following metrics:

- **Bad Debt Ratio:** If the bad debt ratio exceeds 0.5% of total borrowed assets, a Telegram message is sent.
- **Vault Risk Level:** If the computed risk level of a vault exceeds its maximum threshold, a Telegram message is sent.
- **Market Allocation Ratio:** If any market's allocation ratio exceeds its risk-adjusted threshold, a Telegram message is sent.

Additional insights on Morpho vault risks are available at [Llama Risk blog](https://www.llamarisk.com/research/morpho-vaults-risk-disclaimer).

### Risk Levels

The overall risk level of a Morpho Vault is determined by the risk levels of its markets. Markets and thresholds are defined in [risk.py](./risk.py); vault scores are defined in [config.py](./config.py). Both are mutable operating configuration and are intentionally not fixed by tests. Level 1 represents the safest configuration.

### Oracle validation

When adding or checking market rows in [risk.py](./risk.py), use [morpho-oracle-validation.md](./morpho-oracle-validation.md): resolve `uniqueKey` and `oracle.address` via the Morpho GraphQL API, validate feeds on-chain (typically `MorphoChainlinkOracleV2` getters and `description()`), then classify feeds (Chainlink, RedStone, Chronicle, API3, or unknown) using on-chain hints plus official listings — [Chainlink](https://docs.chain.link/data-feeds/price-feeds/addresses) / [data.chain.link](https://data.chain.link), [RedStone](https://docs.redstone.finance/), [Chronicle Oracles](https://chroniclelabs.org/dashboard/oracles), [Api3 Market](https://market.api3.org/) (see §4 in that doc).

### How to Add a New Vault

To monitor a new Morpho vault, add a `VaultConfig` to `VAULTS_V1_BY_CHAIN` or `VAULTS_V2_BY_CHAIN` in [config.py](./config.py). This is the single source used by market and governance monitoring.

**For YV Collateral Vaults:** Set `collateral_asset` on each V1 or V2 `VaultConfig` used by the Yearn strategy. Vaults with the same underlying asset are grouped automatically for combined liquidity monitoring.

### Bad Debt

Bad debt is fetched from the Morpho GraphQL API. Each market is checked for bad debt; if any market exhibits bad debt, a Telegram message is sent. The script runs hourly via the [monitoring runner](../../automation/jobs.yaml). The monitoring logic is implemented in [markets.py](./markets.py).

### Liquidity

The standard check alerts when immediately withdrawable vault liquidity falls below the shared threshold in [risk.py](./risk.py). YV-collateral strategy vaults use the market-aware coverage check below instead of the standard percentage threshold.

#### YV Collateral Vault Liquidity Monitoring

For vaults that are used as collateral in Yearn v3 strategies (YV collateral vaults), the system implements market-aware unwind liquidity monitoring. Instead of checking each vault individually, vaults with the same underlying asset are grouped together and their withdrawable liquidity is aggregated.

**Configuration:** YV collateral strategy vaults set `collateral_asset` in [config.py](./config.py). Keep both generations configured while liquidity migrates from V1 to V2. Watched direct YV-collateral markets are defined in `YV_COLLATERAL_MARKETS_BY_ASSET` in the same file.

**Thresholds:**

- **Regular vaults:** Defined by `LIQUIDITY_THRESHOLD` in [risk.py](./risk.py) and shared by V1 and V2.
- **YV collateral vaults:** Require enough combined withdrawable liquidity to cover collateral at risk in direct YV-collateral Morpho markets, plus a liquidation buffer (`YV_COLLATERAL_*` constants in [markets.py](./markets.py)). Price shock is selected from market LLTV: 2% for LLTV >= 86%, 15% for LLTV <= 77%, otherwise 10%.

**Logic:** For each asset group (e.g., all USDC vaults at one chain), the system:

1. Calculates combined total assets across all configured v1 and v2 strategy vaults with the same asset
2. Calculates combined immediately withdrawable liquidity across those vaults. Shared Morpho market liquidity is capped once per market so v1 and v2 vaults cannot double-count the same cash. For v2 this uses the API's `liquidityUsd` value (idle assets plus the selected liquidity adapter) and conservatively excludes force-deallocatable liquidity
3. Queries configured direct Yearn vault collateral markets such as `yvvbUSDC/vbUSDT` independently of vault allocations, so a v1 allocation removal cannot disable the check
4. Fetches Morpho collateral-at-risk data at the configured adverse price shock
5. Sums collateral at risk per underlying asset group
6. Sends alerts only if combined withdrawable liquidity is below total collateral at risk plus buffer

This approach alerts on liquidation coverage risk instead of low liquidity as a percentage of all assets, which avoids noise when a vault group is highly utilized but still has enough liquidity to unwind risky positions.

### Vault Risk Level

The total risk level of a vault is computed as the weighted sum of the risk levels of its individual market allocations:

```math
\text{Total Risk Level} = \sum_{i=1}^{n} (\text{Market Risk Level}_i \times \text{Allocation}_i)
```

Where:

- **Market Risk Level:** A value between 1 and 5, with 1 representing the lowest risk. This value acts as a multiplier (e.g., a market with risk level 1 contributes a multiplier of 1, level 2 contributes 2, etc.).
- **Allocation:** The percentage of the vault's assets allocated to that market.
- **Total Risk Level:** The sum of the weighted risks across all markets.

This computed risk level is compared against the mutable `MAX_RISK_THRESHOLDS` configuration in [risk.py](./risk.py).

The current maximum total-risk thresholds are:

- **Risk Level 1:** 1.15
- **Risk Level 2:** 2.30
- **Risk Level 3:** 3.45
- **Risk Level 4:** 4.60
- **Risk Level 5:** 5.00

For example, a Risk Level 2 vault alerts when its weighted total risk exceeds 2.30. These values are a snapshot for readability; [risk.py](./risk.py) remains the source of truth and may be updated as risk assessments change.

If a vault's total risk level exceeds its threshold, an alert is triggered via a Telegram message.

### Market Allocation Ratio

The system monitors each market's allocation within a vault to ensure it does not exceed its risk-adjusted threshold. Each market has a maximum allocation threshold based on its inherent risk tier and the vault's overall risk level.

The mutable base allocation limits and vault-level adjustment are defined by `ALLOCATION_TIERS` and `get_market_allocation_threshold` in [risk.py](./risk.py). Higher-risk vault configurations can accept more exposure to a given market tier.

The current base limits are:

- **Risk Level 1 market:** 101% configured, effectively allowing a full allocation
- **Risk Level 2 market:** 30%
- **Risk Level 3 market:** 10%
- **Risk Level 4 market:** 5%
- **Risk Level 5 or unknown market:** 1%

The vault risk level reduces the market tier used to select the limit by one step for each level above Risk Level 1, with a floor of Risk Level 1. For example:

- A Risk Level 1 vault may allocate up to 30% to a Risk Level 2 market.
- A Risk Level 2 vault may allocate fully to a Risk Level 2 market because its adjusted market tier is Risk Level 1.
- A Risk Level 2 vault may allocate up to 10% to a Risk Level 4 market.
- A Risk Level 3 vault may allocate up to 30% to a Risk Level 4 market.

Allocation ratios are capped at 100% by the monitor, so the configured 101% Risk Level 1 limit acts as a full-allocation allowance. As with the total-risk thresholds, [risk.py](./risk.py) is the source of truth for these mutable values.

The system monitors the allocation ratio for each market hourly:

```math
\text{Allocation\_ratio} = \frac{\text{Market Supply USD}}{\text{Total Vault Assets USD}}
```

If any market's allocation exceeds its adjusted threshold, an alert is triggered with a corresponding Telegram message. This mechanism ensures that vaults maintain proper diversification and are not overly concentrated in higher-risk markets.

## Vault V2 Monitoring

Morpho's [Vault V2](https://github.com/morpho-org/vault-v2) replaces the v1 single-vault timelock with a **per-function timelock** keyed by arbitrary calldata, plus a richer adapter system. Yearn-curated v2 vaults are monitored separately:

- [`governance_v2.py`](./governance_v2.py) — daily, pulls a per-vault governance **snapshot** from Morpho's GraphQL API (`vaultV2s.pendingConfigs` + `owner` / `curator` / `sentinels` / `allocators` / `adapters`) and diffs it against the persisted cache. Mirrors v1's pull-based approach (`pendingTimelock` / `pendingGuardian` / `pendingCap`) so RPC usage stays bounded. Alerts on: new pending timelocked operations, executed or revoked operations, owner / curator changes, sentinel / allocator / adapter set changes.
- [`markets_v2.py`](./markets_v2.py) — hourly, walks each v2 vault's adapters and applies the shared [risk.py](./risk.py) policy against underlying Morpho Blue markets when the vault uses `MorphoMarketV1AdapterV2`. It also checks the API's immediately withdrawable `liquidityUsd` against the shared 1% threshold. V2 vaults used by YV-collateral strategies skip the individual threshold because `markets.py` performs the more relevant combined collateral-at-risk coverage check. For `MorphoVaultV1Adapter`, the wrapped v1 vault keeps receiving its full v1 analysis via `markets.py`; we only flag a wrapped v1 vault absent from `VAULTS_V1_BY_CHAIN`.
- [`v2_decoders.py`](./v2_decoders.py) — selector→signature map and decoders for every v2 timelocked function (and the three `idData` tag prefixes used by `increaseAbsoluteCap`/`increaseRelativeCap`).

### Vault list

Monitored V1 and V2 vaults live in [`config.py`](./config.py) as typed `VaultConfig` rows. The V2 list is sourced from [Yearn's curator page on Morpho](https://app.morpho.org/curator/yearn?v2=true) and remains explicit so a third party using a similar name is not monitored automatically.

To add a new v2 vault, append a row to the chain's list and pick a risk tier (1–5).

### Cache

Both scripts share `MORPHO_FILENAME` (default `cache-id.txt`). New key types added by v2:

| Key suffix | Segment | Value | Meaning |
| --- | --- | --- | --- |
| `v2_pending` | `keccak(data)` | `validAt` ts, `-1`, or `0` | Pending timelock operation: pending / executed / revoked |
| `v2_pending_function` | `keccak(data)` | function name | Human-readable operation name used when a pending operation later executes or is revoked |
| `v2_pending_index` | `pending_keys` | comma-joined `keccak(data)` | Reverse index used to detect operations that disappeared from `pendingConfigs` |
| `v2_role` | `owner` / `curator` | lowercase address | Last-known instant-role address |
| `v2_set` | `sentinels` / `allocators` / `adapters` | comma-joined lowercase addresses | Last-known role set |

### Limitations

`MorphoMarketV1AdapterV2` has its own internal timelock system (`setSkimRecipient`, `burnShares`, `increaseTimelock`, etc.). The Morpho GraphQL API does **not** expose adapter-internal pending operations, and replaying `Submit` events on each adapter would reintroduce the RPC cost we're explicitly avoiding. Phase-2 candidate.

### How to run locally

```bash
# Hourly (allocation + risk)
python protocols/morpho/markets_v2.py
# Daily (timelocks + role changes)
python protocols/morpho/governance_v2.py
```

Set `MORPHO_FILENAME=/tmp/morpho-cache.txt` to use an isolated cache while testing.

## API Docs

Morpho GraphQL API wizard is available at [https://api.morpho.org/graphql](https://api.morpho.org/graphql). GraphQL schema is available in [morpho_ql_schema.txt](./morpho_ql_schema.txt) file. For fetching a market oracle and validating it against RPC (including Chainlink, RedStone, Chronicle, API3), see [morpho-oracle-validation.md](./morpho-oracle-validation.md).
