# Compound V3

## Utilization

Github actions run hourly and send telegram message if there is a market with utilization above `99%`. [Python script code](./main.py).

## Governance

[Internal timelock monitoring](../timelock/README.md) for queueing tx to [Timelock contract on Mainnet](https://etherscan.io/address/0x6d903f6003cca6255D85CcA4D3B5E5146dC33925#code).

This Timelock contract covers **Mainnet and all other chains**. Each protocol contract is controlled by the [Timelock contract](https://etherscan.io/address/0x6d903f6003cca6255D85CcA4D3B5E5146dC33925#code). For more info see the [governance docs](https://docs.compound.finance/governance/). Delay is [2 days](https://etherscan.io/address/0x6d903f6003cca6255D85CcA4D3B5E5146dC33925#readContract).

Additionally, Github actions bot runs every hour and fetches queued proposals using Compound API: [proposals.py](./proposals.py)

## On-Chain Collateral Risk Monitoring

The script [collateral.py](./collateral.py) reads collateral data directly from Compound V3 Comet contracts on-chain. It is run daily by Github actions.

### Monitored Markets

Markets are configured in [`MARKETS_BY_CHAIN`](./collateral.py) with their risk levels. Risk level defines the risk of Yearn strategy that is depositing into the market.

### What is Monitored

For each market, the script fetches on-chain:
- Number of collateral assets and their balances (`numAssets`, `getAssetInfo`, `totalsCollateral`)
- Prices via Compound's price feeds (`getPrice`)
- Total supply, borrow, and reserves (`totalSupply`, `totalBorrow`, `getReserves`)

It then checks:
- **Collateral Allocation Ratio:** If any asset's allocation exceeds its risk-adjusted threshold.
- **Total Risk Level:** If the weighted risk of all collateral exceeds the market's threshold.
- **Borrow/Supply Ratio:** Alerts if above 95%.
- **Bad Debt:** Alerts if `getReserves()` returns a negative value (protocol has more debt than assets).
- **Unknown Assets:** Flags collateral assets not yet in the risk tier mapping.

### Debt Supply Ratio

Alerts if the borrow/supply ratio exceeds 95% (`DEBT_SUPPLY_RATIO` in [collateral.py](./collateral.py)).

### Risk Levels

The overall risk level of a market is determined by the risk levels of its collateral assets. For details about each asset's risk tier, see [SUPPLY_ASSETS](/utils/assets.py). Markets are categorized by risk level, with Level 1 representing the safest configuration.

### How to Add a New Market

1. Add the market's Comet proxy address, name, and risk level to `MARKETS_BY_CHAIN` in [collateral.py](./collateral.py).
2. Ensure each collateral asset is in `SUPPLY_ASSETS` in [utils/assets.py](/utils/assets.py). Unknown assets default to risk tier 5.

### Market Risk Level

The total risk level of a market is the weighted sum of collateral asset risk tiers:

```math
\text{Total Risk Level} = \sum_{i=1}^{n} (\text{Asset Risk Tier}_i \times \text{Allocation}_i)
```

Where:

- **Asset Risk Tier:** A value between 1 and 5 (1 = lowest risk).
- **Allocation:** The asset's share of total collateral value.

This is compared against thresholds defined in [MAX_RISK_THRESHOLDS](/utils/assets.py):

- **Risk Level 1:** Maximum threshold of 1.10
- **Risk Level 2:** Maximum threshold of 2.20
- **Risk Level 3:** Maximum threshold of 3.30
- **Risk Level 4:** Maximum threshold of 4.40
- **Risk Level 5:** Maximum threshold of 5.00

### Collateral Allocation Ratio

Each collateral asset has a maximum allocation based on its risk tier and the market's risk level.

Base allocation limits by risk tier (defined in [ALLOCATION_TIERS](/utils/assets.py)):

- **Risk Tier 1:** 100%
- **Risk Tier 2:** 30%
- **Risk Tier 3:** 10%
- **Risk Tier 4:** 5%
- **Risk Tier 5:** 1%

For markets with higher risk levels, thresholds become more permissive via [get_market_allocation_threshold](/utils/assets.py).

Examples:

- A Risk-1 market accepts up to 30% in a Risk-2 asset.
- A Risk-2 market accepts up to 100% in a Risk-2 asset.
- A Risk-1 market accepts up to 10% in a Risk-3 asset.

### Bad Debt

Bad debt is detected on-chain via `getReserves()`. When reserves go negative, the protocol has more outstanding borrows than available assets — this triggers an alert with the USD value of the shortfall.
