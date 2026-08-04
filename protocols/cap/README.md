# CAP

For more info about CAP protocol check [the docs](https://docs.cap.app/).

## Governance

[cUSD](https://etherscan.io/address/0xcCcc62962d17b8914c62D74FfB843d73B2a3cccC#code) is an upgradeable proxy on Mainnet. Its roles are stored in `AccessStorageLocation` at `0xb413d65cb88f23816c329284a0d3eb15a99df7963ab7402ade4c5da22bff6b00`, which points to the [AccessControl](https://etherscan.io/address/0x7731129a10d51e18cde607c5c115f26503d2c683#code) proxy. The sole default admin is the [Timelock contract](https://etherscan.io/address/0xD8236031d8279d82E615aF2BFab5FC0127A329ab#readContract), which has a minimum [24-hour delay](https://etherscan.io/address/0xD8236031d8279d82E615aF2BFab5FC0127A329ab#readContract#F5).

[Internal timelock monitoring](../timelock/README.md) alerts on transactions queued to the [Mainnet Timelock](https://etherscan.io/address/0xD8236031d8279d82E615aF2BFab5FC0127A329ab#code).

## stcUSD Monitoring

The hourly [status.py](./status.py) monitor checks the [stcUSD contract](https://etherscan.io/address/0x88887bE419578051FF9F4eb6C858A951921D8888):

1. The contract's cUSD balance must cover `totalAssets() + lockedProfit()`. A deficit sends one critical alert; recovery re-arms the monitor.
2. `convertToAssets(1e18)` must not decrease between runs. Any decrease sends a critical alert.

stcUSD has no withdrawal pause, cooldown, queue, allowlist, or withdrawal-cap override. Its 86,400-second `lockDuration` only vests newly received profit. The duration is set during the one-time initializer and has no setter. Changing it requires a UUPS upgrade, whose sole live upgrade-role holder is the monitored CAP TimelockController.

## Data Monitoring

The script [liquidity.py](./liquidity.py) is run daily via the [monitoring runner](../automation/jobs.yaml).

It monitors withdrawable liquidity for the CAP protocol's cUSD contract [`0xcCcc62962d17b8914c62D74FfB843d73B2a3cccC`](https://etherscan.io/address/0xcCcc62962d17b8914c62D74FfB843d73B2a3cccC#code):

1. **Fetches all assets** from the cUSD contract
2. **For each asset**, calculates total withdrawable liquidity:
   - Withdrawable amount from the fractional reserve vault (via `maxWithdraw` for the cUSD contract)
   - Direct token balance held by the cUSD contract
3. **Sums normalized values** across all assets
4. **Sends Telegram alert** if total withdrawable liquidity falls below [defined threshold](./liquidity.py#L8) telegram alert is sent
5. **RedStone Price Feed for cUSD_FUNDAMENTAL** if the value falls below 99980000, telegram alert is sent. [Tenderly alert](https://dashboard.tenderly.co/yearn/sam/alerts/rules/316f440e-457b-4cfa-a69e-f7f54230bf44)

## Large Mint Monitoring (No Event Scanning)

Large mint monitoring is integrated into [liquidity.py](./liquidity.py).

It intentionally does **not** scan events. Instead, it compares cached `totalSupply` values and alerts when the increase is above:

- `CUSD_LARGE_MINT_THRESHOLD_PERCENT` (default: `0.05`, i.e. `5%` of previous `totalSupply`)

The daily supply baseline expires after 36 hours and initializes from the next valid observation, preventing multiple
missed daily runs from being treated as one large mint interval.
