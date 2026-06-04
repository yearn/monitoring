# Fluid

## Governance

[Fluid Liquidity](https://etherscan.io/address/0x52aa899454998be5b000ad077a46bbe360f4e497#code) contract is upgradable proxy on Mainnet. The admin is set to the [InstaTimelock](https://etherscan.io/address/0x52aa899454998be5b000ad077a46bbe360f4e497#readContract#F1), address: 0x2386DC45AdDed673317eF068992F19421B481F4c with min delay set to [1 day](https://etherscan.io/address/0x2386DC45AdDed673317eF068992F19421B481F4c#readContract#F5). [Internal timelock monitoring](../timelock/README.md) is set to notify when the function `queueTransaction()` is called.

The script [proposals.py](/fluid/proposals.py) to check for new governance proposals using Fluid API is [run hourly via the monitoring runner](../automation/jobs.yaml). If the proposal is queued, it sends a telegram message. It uses cache to avoid sending duplicate messages. For more info see the [Fluid Governance](https://fluid.io/gov) page.
