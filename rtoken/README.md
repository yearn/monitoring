# RToken

For more information on RToken, see the [Reserve Protocol risk overview](https://github.com/spalen0/risk-score/blob/master/protocol/reserve.md).

## [ETH+](https://app.reserve.org/ethereum/token/0xe72b141df173b999ae7c1adcbf60cc9833ce56a8/overview)

### Governance

[Tenderly alert](https://dashboard.tenderly.co/yearn/sam/alerts/rules/14fde22a-110d-43ff-be49-6f4b77ed3ecb) and [internal timelock monitoring](../timelock/README.md) when event `CallScheduled` is emitted from [Timelock contract](https://etherscan.io/address/0x5d8A7DC9405F08F14541BA918c1Bf7eb2dACE556#code).

### Data Monitoring

The script `rtoken/monitor_rtoken.py` runs hourly via the [monitoring runner](../automation/jobs.yaml) to monitor key health indicators of the RToken ETH+ system using on-chain data.

- **RToken Collateral Coverage**: Alerts if the `totalSupply` of the RToken drops below 104% of the required backing (`basketNeeded`), indicating potential undercollateralization. Threshold defined [in code](monitor_rtoken.py#L11).
- **StRSR Exchange Rate Stability**: Fetches the `exchangeRate` from the StRSR contract. On the first run, it caches this rate. On subsequent runs, it alerts if the current rate falls below the initial cached value, signalling potential depegging or risk. Caching logic [in code](monitor_rtoken.py#L148-L157).
- **RToken Redemption Available**: Alerts if the `redemptionAvailable` of the RToken is less than defined threshold ETH. Threshold defined [in code](monitor_rtoken.py#L47).

### Supply Monitoring

[Tenderly alert](https://dashboard.tenderly.co/yearn/sam/alerts/rules/2263c87a-7333-4452-957e-9695645d1040) when `totalSupply` of [`ETH+`](https://etherscan.io/address/0xE72B141DF173b999AE7c1aDcbF60Cc9833Ce56a8) has changed above defined threshold.

## [bsdETH](https://app.reserve.org/base/token/0xcb327b99ff831bf8223cced12b1338ff3aa322ff/overview) - DISABLED ⚠️

### Governance

[Tenderly alert](https://dashboard.tenderly.co/yearn/sam/alerts/rules/62804c0b-830c-433a-89fc-264bff3005e4) when event `CallScheduled` is emitted from [Timelock contract](https://basescan.org/address/0xe664d294824C2A8C952A10c4034e1105d2907F46#code).

### Data Monitoring

The script `rtoken/monitor_rtoken.py` runs hourly via the [monitoring runner](../automation/jobs.yaml) to monitor key health indicators of the RToken Based ETH system using on-chain data.

- **RToken Collateral Coverage**: Alerts if the `totalSupply` of the RToken drops below 103% of the required backing (`basketNeeded`), indicating potential undercollateralization. Threshold defined [in code](monitor_rtoken.py#L11).
- **StRSR Exchange Rate Stability**: Fetches the `exchangeRate` from the StRSR contract. On the first run, it caches this rate. On subsequent runs, it alerts if the current rate falls below the initial cached value, signalling potential depegging or risk. Caching logic [in code](monitor_rtoken.py#L148-L157).
- **RToken Redemption Available**: Alerts if the `redemptionAvailable` of the RToken is less than defined threshold ETH. Threshold defined [in code](monitor_rtoken.py#55).
