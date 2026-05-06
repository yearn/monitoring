# Depeg Monitoring

Centralized depeg monitoring for LRTs and stablecoins. Runs two types of checks:

1. **Fundamental oracle check** — reads Redstone on-chain push oracles. Each asset uses its own threshold (see table below); breaching it triggers a CRITICAL alert.
2. **DefiLlama market price check** — fetches USD prices, computes a ratio vs the underlying reference (ETH or USD), then normalizes that ratio against a per-asset `fair_value` so accruing LRTs are checked against their accrued rate rather than a flat 1:1 peg. Deviation below the per-asset threshold triggers a CRITICAL alert.

When DefiLlama returns no price for a configured asset, a MEDIUM alert fires so coverage gaps are visible rather than silently skipped.

LRT alerts route to the `lrt` Telegram channel. Stablecoin alerts route to `stables`. The workflow exports `TELEGRAM_BOT_TOKEN_LRT`, `TELEGRAM_BOT_TOKEN_STABLES`, `TELEGRAM_CHAT_ID_LRT`, and `TELEGRAM_CHAT_ID_STABLES`; bot tokens fall back to `TELEGRAM_BOT_TOKEN_DEFAULT`, but chat IDs have no fallback and must be set.

## Fundamental Oracles

| Asset | Oracle address | Threshold | Tenderly alert |
|-------|----------------|-----------|----------------|
| LBTC  | `0xb415eAA355D8440ac7eCB602D3fb67ccC1f0bc81` | 0.998 | `eca272ef-979a-47b3-a7f0-2e67172889bb` (value change between blocks) |
| cUSD  | `0x9a5a3c3ed0361505cc1d4e824b3854de5724434a` | 0.9998 | `316f440e-457b-4cfa-a69e-f7f54230bf44` (`latestAnswer` < 0.9998) |

Both oracles implement AggregatorV3 (`latestRoundData()`, 8 decimals).

## DefiLlama-Monitored Assets

These assets do not have on-chain fundamental push oracles on Ethereum mainnet. Redstone provides off-chain fundamental feeds (pull model) for weETH, ezETH, rsETH, and pufETH, but they require calldata injection at transaction time and cannot be read directly on-chain.

Per-asset `fair_value` is a conservative floor under the current Redstone fundamental. The check compares `market_ratio / fair_value` against `threshold` — so a 2% deviation alert on weETH fires when the market ratio drops below `1.07 × 0.98 ≈ 1.0486` ETH, not below `0.98` ETH flat.

### LRTs (vs ETH)

| Token  | Address | fair_value | threshold |
|--------|---------|------------|-----------|
| weETH  | `0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee` | 1.07 | 0.98 |
| ezETH  | `0xbf5495Efe5DB9ce00f80364C8B423567e58d2110` | 1.06 | 0.98 |
| rsETH  | `0xA1290d69c65A6Fe4DF752f95823Fae25cB99e5A7` | 1.05 | 0.98 |
| pufETH | `0xD9A442856C234a39a81a089C06451EBAa4306a72` | 1.05 | 0.98 |
| osETH  | `0xf1C9acDc66974dFB6dEcB12aA385b9cD01190E38` | 1.00 | 0.98 |
| rswETH | `0xFAe103DC9cf190eD75350761e95403b7b8aFa6c0` | 1.00 | 0.98 |
| mETH   | `0xd5F7838F5C461fefF7FE49ea5ebaF7728bB0ADfa` | 1.00 | 0.98 |

### Stablecoins (vs USD)

| Token  | Address | fair_value | threshold | Notes |
|--------|---------|------------|-----------|-------|
| FDUSD  | `0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409` | 1.00 | 0.98 | |
| deUSD  | `0x15700B564Ca08D9439C58cA5053166E8317aa138` | 1.00 | 0.98 | |
| USD0   | `0x73A15FeD60Bf67631dC6cd7Bc5B6e8da8190aCF5` | 1.00 | 0.98 | |
| USD0++ | `0x35D8949372D46B7a3D5A56006AE77B215fc69bC0` | 1.00 | 0.90 | 4-year bond; legitimately trades at a discount. |
| USDe   | `0x4c9EDD5852cd905f086C759E8383e09bff1E68B3` | 1.00 | 0.98 | |

## Tenderly Alert Coverage

| Asset | Tenderly Alert | Status |
|-------|---------------|--------|
| LBTC  | `eca272ef-...` (value change between blocks) | Covered |
| cUSD  | `316f440e-...` (latestAnswer < 0.9998) | Covered |
| weETH | — | **Needs Tenderly alert** if on-chain push oracle deployed |
| ezETH | — | **Needs Tenderly alert** if on-chain push oracle deployed |
| rsETH | — | **Needs Tenderly alert** if on-chain push oracle deployed |
| pufETH | — | **Needs Tenderly alert** if on-chain push oracle deployed |
| Others | N/A (DefiLlama only) | Monitored by this script |

## Creating Tenderly Alerts for New Oracles

If Redstone deploys on-chain push oracles for weETH, ezETH, rsETH, or pufETH on Ethereum mainnet, create Tenderly alerts with:
- **Network**: Ethereum Mainnet
- **Contract address**: The oracle contract
- **Alert type**: Transaction — function call `latestAnswer()` returns value below threshold
- **Threshold**: 99800000 (0.998 with 8 decimals) for LRTs, 99980000 (0.9998) for stables
- **Delivery**: Telegram channel for the respective protocol
