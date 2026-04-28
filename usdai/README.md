# USDai Monitoring

USDai monitors on Arbitrum focus on backing safety and loan activity.

## Contracts (Arbitrum One)

- USDai Token (proxy): `0x0A1a1A107E45b7Ced86833863f482BC5f4ed82EF`
- PYUSD Token: `0x46850aD61C2B7d64d08c9C754F45254596696984`
- sUSDai: `0x0B2b2B2076d95dda7817e785989fE353fe955ef9`
- Loan Router: `0x0C2ED170F2bB1DF1a44292Ad621B577b3C9597D1`

## Backing Invariant

The invariant monitored in `usdai/main.py` is:

`usdai.totalSupply() + usdai.bridgedSupply() <= PYUSD.balanceOf(USDai)`

All values are normalized to 1e18 units before comparison.

### Why `bridgedSupply` matters

`bridgedSupply` represents USDai minted for cross-chain/bridge accounting. So the required PYUSD backing is not only local `totalSupply()`, but `totalSupply() + bridgedSupply()`.

### Alert Condition

We alert only when:

`(totalSupply + bridgedSupply - pyusdBalance) >= USDAI_INVARIANT_BREACH_THRESHOLD_RAW`

Default threshold is `100e18` (100 USDai).

## Loan Monitoring

We also track sUSDai loan principal from Loan Router and alert on meaningful total principal changes.

A legacy loan principal is intentionally included as a fixed adjustment for continuity.

## Large Mint Monitoring (No Event Scanning)

`usdai/large_mints.py` intentionally does **not** scan events.

It runs cached `totalSupply` delta checks and alerts when the increase is above:

- `USDAI_LARGE_MINT_THRESHOLD_PERCENT` (default: `0.05`, i.e. `5%` of previous `totalSupply`)

The GitHub workflow `.github/workflows/hourly.yml` runs this monitor hourly.

## Large Transfer Monitoring (Incremental Event Scanning)

`usdai/large_transfers.py` scans only new `Transfer` logs from cached block state.

It uses chunked `eth_getLogs` calls:

- `USDAI_LARGE_TRANSFER_CHUNK_BLOCKS` (default: `2000`)
- `USDAI_LARGE_TRANSFER_FIRST_RUN_LOOKBACK_BLOCKS` (default: `2000`)

And alerts when transfer amount is above:

- `USDAI_LARGE_TRANSFER_THRESHOLD` (default: `100000` USDai)

This avoids full-history rescans on every run while still catching large movements.

## Price Monitoring Scope

`usdai/main.py` does not monitor PYUSD/USD price.

Price/peg monitoring should be handled by the shared stable monitor:

- `stables/main.py`

## Usage

Run USDai invariant + loan monitor:

```bash
uv run usdai/main.py
```

Run large mint monitor:

```bash
uv run usdai/large_mints.py
```

Run large transfer monitor:

```bash
uv run usdai/large_transfers.py
```
