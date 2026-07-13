# Spark

## Utilization

`main.py` checks utilization rates of active, non-deprecated [SparkLend](https://etherscan.io/address/0xC13e21B648A5Ee794902342038FF3aDAB66BE987) markets on mainnet (Aave v3 fork, same logic as `protocols/aave/main.py`): WETH, wstETH, WBTC, USDT, weETH, cbBTC, USDS, PYUSD. Deprecated markets (DAI, sDAI, USDC, sUSDS, LBTC) are excluded. Sends a Telegram alert when a market's utilization is above 99%.

Run: `python protocols/spark/main.py`

## Governance

### Proposals Script

`proposals.py` monitors new governance proposals from the [Spark governance portal](https://app.spark.fi/spk/governance), which is backed by the Snapshot space [`sparkfi.eth`](https://snapshot.box/#/s:sparkfi.eth) (queried via `https://hub.snapshot.org/graphql`). Sends Telegram alerts for new proposals whose voting is still open and caches the created timestamp of the last processed proposal to avoid duplicate messages.

Run: `python protocols/spark/proposals.py`

### Tenderly Alerts

Spark is a Sky star (subDAO): approved proposals are implemented through the Sky governance cycle. An executive spell is scheduled by calling `plot()` on [DSPause](https://etherscan.io/address/0xbE286431454714F511008713973d3B053A2d38f3) and, after the 16-hour delay, cast through the [PauseProxy](https://etherscan.io/address/0xBE8E3e3618f7474F8cB1d074A26afFef007E98FB), which forwards Spark-related payloads to the [Spark SubDAO Proxy](https://etherscan.io/address/0x3300f198988e4C9C63F75dF86De36421f06af8c4) for execution.

Tenderly alert on `plot()` called in DSPause covers scheduling of all spells, including Spark ones (this is the same contract already covered by the Maker DSPause alert, see `protocols/maker/README.md`). Note that the PauseProxy (`0xBE8E3e...`) itself has no `plot()` function — only `exec()`.

To get the proposal data from a received alert:

1. see the tx from the alert on Tenderly
2. find the spell address in the trace
3. match it with the proposal on the [Spark governance portal](https://app.spark.fi/spk/governance) or [Sky voting portal](https://vote.sky.money/executive)
