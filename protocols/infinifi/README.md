# Infinifi Protocol Monitoring

This folder contains monitoring scripts for the Infinifi protocol.

## Structure

- `main.py`: Monitors protocol reserves, backing, and liquid USDC reserves.
  Run this script hourly using github actions.

[Risk Score Report](https://github.com/yearn/risk-score/blob/master/reports/report/infinifi.md)

## Alerts

- **Liquid Reserves**: A Telegram alert is triggered if liquid reserves drop below $8M.
- **Reserve Ratio Breach**: Alert if liquid ratio falls below protocol `reserveRatio` target.
- **Illiquid Ratio Breach**: Alert if illiquid ratio rises above protocol `illiquidTargetRatio`.
- **Backing Per iUSD**: Alert if `totalTVL / iUSD supply` drops below `0.999`.
- **Redemption Pressure**: Alert if `pending redemptions / liquid reserves` exceeds `80%`.
- **Farm Allocation Shift**: Alert if any farm allocation ratio (`farm assets / total TVL`) changes by more than `FARM_RATIO_CHANGE_ALERT_THRESHOLD` versus cached ratio. Farms below 1% of total TVL are excluded.
- **Farm Activation**: Alert if a farm previously at `0` cached ratio moves above `FARM_RATIO_ACTIVATION_ALERT_THRESHOLD` of total TVL.
- **Junior TVL Below Risky Exposure**: Alert if junior TVL (locked iUSD) covers less than 50% of risky farm TVL. Risky farms are all farms NOT in the `SAFE_FARM_IDENTIFIERS` whitelist.

## Large Mint Monitoring (No Event Scanning)

`main.py` includes large iUSD mint monitoring and intentionally does **not** scan events.

It compares cached `totalSupply` deltas and alerts when the increase is above:

- `IUSD_LARGE_MINT_THRESHOLD_PERCENT` (default: `0.05`, i.e. `5%` of previous `totalSupply`)

### Emergency dispatch

HIGH and CRITICAL alerts automatically trigger a `repository_dispatch` to
[liquidity-monitoring](https://github.com/tapired/liquidity-monitoring) to
zero Morpho market caps for siUSD collateral:

- **CRITICAL** — caps are zeroed and reallocation runs immediately
- **HIGH** — a PR is opened with zeroed caps for team review; after merging, trigger reallocation manually

Dispatch is rate-limited to once per 60 minutes per protocol. See
`utils/dispatch.py` and `liquidity-monitoring/hooks.md` for details.

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

**Deployer Address**:

- `0xdecaDAc8778D088A30eE811b8Cc4eE72cED9Bf22`

## Resources

- [Docs](https://docs.infinifi.xyz/)
- iUSD Token: 0x48f9e38f3070AD8945DFEae3FA70987722E3D89c (Ethereum)
- [Protocol Analytics](https://stats.infinifi.xyz/)
