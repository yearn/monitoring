# Infinifi Protocol Monitoring

This folder contains monitoring scripts for the Infinifi protocol.

## Structure

- `main.py`: Monitors protocol reserves, backing, and liquid USDC reserves.
  Run this script hourly using github actions.
- `escrow_valuation.py`: Compares each RWAEscrowRouter's reported `totalAssets` with the value of what it holds. Runs hourly.
- `l2_vaults.py`: Watches the value each L2 vault (OutlandVault) reports for its chain. Runs hourly.

[Risk Score Report](https://github.com/yearn/risk-score/blob/master/reports/report/infinifi.md)

## Alerts

- **Liquid Reserves**: A Telegram alert is triggered if liquid reserves drop below $8M.
- **Reserve Ratio Breach**: Alert if liquid ratio falls below protocol `reserveRatio` target.
- **Illiquid Ratio Breach**: Alert if illiquid ratio rises above protocol `illiquidTargetRatio`.
- **Backing Per iUSD**: Alert if API `totalTVL / iUSD supply` drops below `0.999`. The supply is pinned to and reported with an Ethereum block. The API does not expose a block identifier to this monitor, so this cross-source comparison cannot be atomic.
- **Redemption Pressure**: Alert if `pending redemptions / liquid reserves` exceeds `80%`.
- **Farm Allocation Shift**: Alert if any farm allocation ratio (`farm assets / total TVL`) changes by more than `FARM_RATIO_CHANGE_ALERT_THRESHOLD` versus cached ratio. Farms below 1% of total TVL are excluded.
- **Farm Activation**: Alert if a farm previously at `0` cached ratio moves above `FARM_RATIO_ACTIVATION_ALERT_THRESHOLD` of total TVL.
- **Junior TVL Below Risky Exposure**: Alert if junior TVL (locked iUSD) covers less than 50% of risky farm TVL. Risky farms are all farms NOT in the `SAFE_FARM_IDENTIFIERS` whitelist.

## Escrow Valuation Gap

`escrow_valuation.py` checks every farm in the FarmRegistry whose escrow is an `RWAEscrowRouter`. A router's `totalAssets` is a stored figure: the rate manager raises it by a governance-set annual rate on each harvest, and nothing ties it to the tokens the router holds. The script values the holdings independently:

- **Stablecoins** (the escrow's asset, USDC, DAI, USDT) at par.
- **Midas mTokens** at Midas's NAV feed. The router's whitelisted Midas vaults give each mToken and its data feed. The script reads the feed under the vault's adjusted ("PriceLowered", −7%) aggregator, not the adjusted price the redemption vaults pay.
- **Pending Midas requests** opened by the router: redemptions at NAV, deposits at their USD amount. They are found in the vault's logs whose second indexed topic is the router.

Every on-chain read and log query uses the same block, including infiniFi's total assets, so a redemption settling during the run cannot create a false gap. Discovery includes historical whitelist targets: disabling calls to a vault or token does not remove its remaining holdings or pending claims from the valuation.

Alerts:

- **Valuation gap**: reported `totalAssets` exceeds the holdings' value by more than `INFINIFI_ESCROW_GAP_THRESHOLD` (default `0.03`, 3% of the reported value). The alert is MEDIUM, and becomes HIGH when the gap is also more than `INFINIFI_ESCROW_GAP_TVL_HIGH_THRESHOLD` (default `0.05`) of infiniFi's total assets (`Accounting.totalAssetsValue()`). HIGH triggers the emergency dispatch below. An escalation from MEDIUM to HIGH alerts again.
- **Unpriced holdings** (MEDIUM): the router holds a token the script cannot price. The gap check is skipped until every holding is priced.

Plain `RWAEscrow`s send funds to an off-chain receiver, so there is nothing on-chain to compare them with; they are skipped. Background: on 29/09/2026 the mGLOBAL router's position was converted to mGLO at Midas's lowered redemption price, leaving the router about $1.67M above Midas NAV.

## L2 Vault Reports

Each `OutlandVault` on mainnet mirrors infiniFi's deposits on one L2 (`PortalHub.getVaultChainIds()` / `getVault(chainId)`). Its value, `portalAssetsReport().totalAssetsValue`, changes only when an assets-update message executes, which mints or burns the vault's farm shares and books the change as profit or loss on mainnet. Messages normally arrive over the bridge, but governance can inject any message with `Connector.govReceive`. On 23/09/2026 an injected all-zero update wrote the old Base vault down to 0.

`l2_vaults.py` sends MEDIUM alerts when:

- **Value drop**: a vault's value falls by more than `INFINIFI_L2_VAULT_DROP_THRESHOLD` (default `0.5`) since the last stored reading, including a fall to 0, and the fall is at least `INFINIFI_L2_VAULT_MIN_VALUE` (default `10000`) USD.
- **Stale report**: a vault holding at least the minimum value has had no assets update for `INFINIFI_L2_VAULT_STALE_HOURS` (default `48`). Base normally reports every 6–22h.

- **Mismatch with the L2**: the value mainnet has booked differs from what infiniFi's API shows on that L2 (the `isL2LiquidityFarm` farms in `/api/protocol/data`, the same response `main.py` uses for allocations) by more than `INFINIFI_L2_VAULT_MISMATCH_THRESHOLD` (default `0.25`) and the minimum value. This catches a wrong or injected report directly. If the API is down, only the on-chain checks run.

The drop baseline never expires, so a write-down during a monitoring gap is still compared against the last value seen; only alert dedupe uses the 3h fresh window. The baseline is keyed by vault address. A planned migration that points the hub at a new vault starts a fresh baseline instead of alerting on the old one.

## Large Mint Monitoring (No Event Scanning)

`main.py` includes large iUSD mint monitoring and intentionally does **not** scan events.

It compares cached `totalSupply` deltas and alerts when the increase is above:

- `IUSD_LARGE_MINT_THRESHOLD_PERCENT` (default: `0.05`, i.e. `5%` of previous `totalSupply`)

## Cache Freshness

Hourly delta baselines expire after 3 hours and initialize from the next valid observation. Breach-dedupe state and
liquid-reserve crossing detection are re-armed after the same monitoring gap. PPS-style loss baselines are not affected.

### Emergency dispatch

HIGH and CRITICAL alerts automatically trigger a signed webhook to
[liquidity-monitoring](https://github.com/tapired/liquidity-monitoring) to
zero Morpho market caps for siUSD collateral:

- **CRITICAL** — caps are zeroed and reallocation runs immediately
- **HIGH** — a PR is opened with zeroed caps for team review; after merging, trigger reallocation manually

Dispatch is rate-limited to once per 60 minutes per protocol. The dispatcher
sends the exact JSON body to `http://127.0.0.1:8080/webhook/emergency` with
`X-Hub-Signature-256: sha256=<hmac>` using `LIQUIDITY_WEBHOOK_SECRET`. See
`utils/dispatch.py` for details.

### Alerts disabled ⚠️

- **Reserve Ratio Breach**: Alert if liquid ratio falls below protocol `reserveRatio` target.
- **Illiquid Ratio Breach**: Alert if illiquid ratio rises above protocol `illiquidTargetRatio`.

## Governance Monitoring

Governance monitoring will be monitored via Tenderly alerts on the following addresses:

**Team Multisig**:

- `0x80608f852D152024c0a2087b16939235fEc2400c`

**Timelock Contracts** — monitored by [internal timelock monitoring](../timelock/README.md) for CallScheduled events:

- `TIMELOCK_SHORT`: [`0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32`](https://etherscan.io/address/0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32)
- `TIMELOCK_LONG`: [`0x3D18480CC32B6AB3B833dCabD80E76CfD41c48a9`](https://etherscan.io/address/0x3D18480CC32B6AB3B833dCabD80E76CfD41c48a9)

For RWA escrow governance calls, the linked AI report resolves the owning farm, accounting asset, normalized `totalAssets`, custody (the off-chain receiver, or a router's `externalCall` whitelist) and keeper into a deterministic `Protocol Context` section. `setRate` is rendered as annual percentages, `governanceUpdateTotalAssets` as the profit or loss it books, and `setWhitelist` with the router's balance of any whitelisted token. Outland `govReceive` / `processMessage` messages are decoded against the source chain's vault report.

**Deployer Address**:

- `0xdecaDAc8778D088A30eE811b8Cc4eE72cED9Bf22`

## Resources

- [Docs](https://docs.infinifi.xyz/)
- iUSD Token: 0x48f9e38f3070AD8945DFEae3FA70987722E3D89c (Ethereum)
- [Protocol Analytics](https://stats.infinifi.xyz/)
