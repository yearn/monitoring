# Morpho Monitoring

## Governance

For roles on Morpho vaults, refer to the following [document](https://github.com/morpho-org/metamorpho/blob/main/README.md).

Morpho governance monitoring is defined in the [Python script](./governance.py) that is executed daily via [GitHub Actions](../.github/workflows/daily.yml) because minimum timelock is 3 days to get vaults whitelisted, and 1 day is mimimum value in the contract.

The script checks if there are any new values pending in the timelock for a given vault. It detects the following changes:

- Changing timelock value, minimal values is 1 day.
- Changing guardian address.
- Changing supply caps, only to higher value than the current one, for both supply and withdraw markets.
- Removing of a market from the vault.

### How to Add a New Vault

Add the vault address to either the `MAINNET_VAULTS` or `BASE_VAULTS` variable in [governance.py#L21](./governance.py#L21) to monitor governance changes.

## Vaults & Markets

Morpho Vaults consist of multiple markets, each defining key parameters such as LTV, interest rate models, and oracle data.

Market monitoring is configured via the vault definitions in [markets.py#L13](./markets.py#L13). The script fetches all markets for each vault and checks the following metrics:

- **Bad Debt Ratio:** If the bad debt ratio exceeds 0.5% of total borrowed assets, a Telegram message is sent.
- **Utilization Ratio:** If the utilization ratio exceeds 95%, a Telegram message is sent.
- **Vault Risk Level:** If the computed risk level of a vault exceeds its maximum threshold, a Telegram message is sent.
- **Market Allocation Ratio:** If any market's allocation ratio exceeds its risk-adjusted threshold, a Telegram message is sent.

Additional insights on Morpho vault risks are available at [Llama Risk blog](https://www.llamarisk.com/research/morpho-vaults-risk-disclaimer).

### Risk Levels

The overall risk level of a Morpho Vault is determined by the risk levels of its markets. For more details, refer to the comments in [markets.py#L36](./markets.py#L36). Markets and vaults are categorized by their risk level and blockchain, with Level 1 representing the safest configuration.

### Oracle validation

When adding or checking market rows in [markets.py](./markets.py), use [morpho-oracle-validation.md](./morpho-oracle-validation.md): resolve `uniqueKey` and `oracle.address` via the Morpho GraphQL API, validate feeds on-chain (typically `MorphoChainlinkOracleV2` getters and `description()`), then classify feeds (Chainlink, RedStone, Chronicle, API3, or unknown) using on-chain hints plus official listings — [Chainlink](https://docs.chain.link/data-feeds/price-feeds/addresses) / [data.chain.link](https://data.chain.link), [RedStone](https://docs.redstone.finance/), [Chronicle Oracles](https://chroniclelabs.org/dashboard/oracles), [Api3 Market](https://market.api3.org/) (see §4 in that doc).

### How to Add a New Vault

To monitor a new Morpho vault, add its address to the `VAULTS_BY_CHAIN` variable in [markets.py#L13](./markets.py#L13). This ensures that both the vault's overall metrics and its individual markets are monitored.

**For YV Collateral Vaults:** If Morpho vault is using Yearn V3 Vault (YV collateral vault) as collateral, additional configuration is needed. Add all Morpho Vaults that are used as strategies in Yearn V3 Vault to `VAULTS_WITH_YV_COLLATERAL_BY_ASSET` mapping, organized by chain and underlying asset address. This enables combined liquidity monitoring for all vaults with the same asset.

### Bad Debt

Bad debt is fetched from the Morpho GraphQL API. Each market is checked for bad debt; if any market exhibits bad debt, a Telegram message is sent. The script runs hourly via [GitHub Actions](../.github/workflows/hourly.yml). The monitoring logic is implemented in [markets.py#L166](./markets.py#L166).

### Utilization & Liquidity

The utilization ratio for each market is calculated as the ratio of borrowed assets to total collateral assets. If this ratio exceeds 95%, a Telegram message is sent. The script runs hourly via [GitHub Actions](../.github/workflows/hourly.yml), and the monitoring logic is defined in [markets.py#L263](./markets.py#L263). Note that liquidity is the inverse of utilization—high utilization implies low liquidity (e.g., 95% utilization corresponds to 5% liquidity).

#### YV Collateral Vault Liquidity Monitoring

For vaults that are used as collateral in Yearn v3 strategies (YV collateral vaults), the system implements combined liquidity monitoring. Instead of checking each vault individually, vaults with the same underlying asset are grouped together and their liquidity is aggregated.

**Configuration:** YV collateral vaults are defined in the `VAULTS_WITH_YV_COLLATERAL_BY_ASSET` mapping in [markets.py](./markets.py), organized by chain and asset address.

**Thresholds:**

- **Regular vaults:** Defined in [markets.py#24](./markets.py#24) variable.
- **YV collateral vaults:** Defined in [markets.py#25](./markets.py#25) variable, more conservative due to their use as collateral and more liquidity is needed for liquidating YV tokens as collateral.

**Logic:** For each asset group (e.g., all USDC vaults at one chain), the system:

1. Calculates combined total assets across all vaults with the same asset
2. Calculates combined available liquidity across all vaults with the same asset
3. Checks if combined liquidity ratio falls below the 15% threshold
4. Sends alerts if the combined liquidity is insufficient

This approach provides a more accurate assessment of liquidity risk for YV collateral tokens where the available liquidity is spread across multiple vault strategies.

### Vault Risk Level

The total risk level of a vault is computed as the weighted sum of the risk levels of its individual market allocations:

```math
\text{Total Risk Level} = \sum_{i=1}^{n} (\text{Market Risk Level}_i \times \text{Allocation}_i)
```

Where:

- **Market Risk Level:** A value between 1 and 5, with 1 representing the lowest risk. This value acts as a multiplier (e.g., a market with risk level 1 contributes a multiplier of 1, level 2 contributes 2, etc.).
- **Allocation:** The percentage of the vault's assets allocated to that market.
- **Total Risk Level:** The sum of the weighted risks across all markets.

This computed risk level is compared against predefined maximum thresholds defined in [markets.py#L134](./markets.py#L134):

- **Risk Level 1:** Maximum threshold of 1.10
- **Risk Level 2:** Maximum threshold of 2.20
- **Risk Level 3:** Maximum threshold of 3.30
- **Risk Level 4:** Maximum threshold of 4.40
- **Risk Level 5:** Maximum threshold of 5.00

If a vault's total risk level exceeds its threshold, an alert is triggered via a Telegram message.

### Market Allocation Ratio

The system monitors each market's allocation within a vault to ensure it does not exceed its risk-adjusted threshold. Each market has a maximum allocation threshold based on its inherent risk tier and the vault's overall risk level.

The base allocation limits by risk tier (as defined in [markets.py#L125](./markets.py#L125)) are:

- **Risk Level 1:** 100%
- **Risk Level 2:** 30%
- **Risk Level 3:** 10%
- **Risk Level 4:** 5%
- **Risk Level 5:** 1%

These limits apply to vaults with a risk level of 1. For vaults with higher risk levels, the thresholds become more permissive. The adjustment is calculated in the [get_market_allocation_threshold](./markets.py#L143) function.

Examples:

- A Risk-1 vault accepts up to 30% of its total assets in a Risk-2 market.
- A Risk-2 vault accepts up to 80% of its total assets in a Risk-2 market.
- A Risk-3 vault accepts up to 100% of its total assets in a Risk-2 market.
- A Risk-2 vault accepts up to 10% of its total assets in a Risk-4 market.
- A Risk-3 vault accepts up to 30% of its total assets in a Risk-4 market.

The system monitors the allocation ratio for each market hourly:

```math
\text{Allocation\_ratio} = \frac{\text{Market Supply USD}}{\text{Total Vault Assets USD}}
```

If any market's allocation exceeds its adjusted threshold, an alert is triggered with a corresponding Telegram message. This mechanism ensures that vaults maintain proper diversification and are not overly concentrated in higher-risk markets.

## Vault V2 Monitoring

Morpho's [Vault V2](https://github.com/morpho-org/vault-v2) replaces the v1 single-vault timelock with a **per-function timelock** keyed by arbitrary calldata, plus a richer adapter system. Yearn-curated v2 vaults are monitored separately:

- [`governance_v2.py`](./governance_v2.py) — daily, pulls a per-vault governance **snapshot** from Morpho's GraphQL API (`vaultV2s.pendingConfigs` + `owner` / `curator` / `sentinels` / `allocators` / `adapters`) and diffs it against the persisted cache. Mirrors v1's pull-based approach (`pendingTimelock` / `pendingGuardian` / `pendingCap`) so RPC usage stays bounded. Alerts on: new pending timelocked operations, executed or revoked operations, owner / curator changes, sentinel / allocator / adapter set changes.
- [`markets_v2.py`](./markets_v2.py) — hourly, walks each v2 vault's adapters and runs the existing v1 risk-tier scoring against the underlying Morpho Blue markets when the vault uses `MorphoMarketV1AdapterV2`. For `MorphoVaultV1Adapter` (today's common case) the wrapped v1 vault keeps receiving its full v1 analysis via `markets.py`; we only flag the case where v2 introduces a new wrapped v1 vault that operators should add to `VAULTS_BY_CHAIN`.
- [`v2_decoders.py`](./v2_decoders.py) — selector→signature map and decoders for every v2 timelocked function (and the three `idData` tag prefixes used by `increaseAbsoluteCap`/`increaseRelativeCap`).

### Vault list

Monitored v2 vaults live in [`VAULTS_V2_BY_CHAIN`](./markets_v2.py) — same shape as the v1 [`VAULTS_BY_CHAIN`](./markets.py#L29), one `[name, address, risk_level]` row per vault. The initial list is sourced from [Yearn's curator page on Morpho](https://app.morpho.org/curator/yearn?v2=true) (filtered via GraphQL by Yearn's curator addresses) and is kept manually so a third-party squatting on the name doesn't get monitored as a Yearn vault.

To add a new v2 vault, append a row to the chain's list and pick a risk tier (1–5).

### Cache

Both scripts share `MORPHO_FILENAME` (default `cache-id.txt`). New key types added by v2:

| Key suffix | Segment | Value | Meaning |
| --- | --- | --- | --- |
| `v2_pending` | `keccak(data)` | `validAt` ts, `-1`, or `0` | Pending timelock operation: pending / executed / revoked |
| `v2_pending_index` | `pending_keys` | comma-joined `keccak(data)` | Reverse index used to detect operations that disappeared from `pendingConfigs` |
| `v2_role` | `owner` / `curator` | lowercase address | Last-known instant-role address |
| `v2_set` | `sentinels` / `allocators` / `adapters` | comma-joined lowercase addresses | Last-known role set |

### Limitations

`MorphoMarketV1AdapterV2` has its own internal timelock system (`setSkimRecipient`, `burnShares`, `increaseTimelock`, etc.). The Morpho GraphQL API does **not** expose adapter-internal pending operations, and replaying `Submit` events on each adapter would reintroduce the RPC cost we're explicitly avoiding. Phase-2 candidate.

### How to run locally

```bash
# Hourly (allocation + risk)
python morpho/markets_v2.py
# Daily (timelocks + role changes)
python morpho/governance_v2.py
```

Set `MORPHO_FILENAME=/tmp/morpho-cache.txt` to use an isolated cache while testing.

## API Docs

Morpho GraphQL API wizard is available at [https://api.morpho.org/graphql](https://api.morpho.org/graphql). GraphQL schema is available in [morpho_ql_schema.txt](./morpho_ql_schema.txt) file. For fetching a market oracle and validating it against RPC (including Chainlink, RedStone, Chronicle, API3), see [morpho-oracle-validation.md](./morpho-oracle-validation.md).
