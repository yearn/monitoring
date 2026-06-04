
# Lido

## Governance

Tenderly alerts will send telegram message when there are transactions made by:

- [Lido DAO voting](https://dashboard.tenderly.co/yearn/sam/alerts/rules/8e577a18-92b2-4cab-86b8-53c7c3025a00) which start the voting process. The alert is triggered on event emitted `StartVote` to cover both `newVote()` functions for starting the voting process. Voted transactions(script) can be forwarded to [Lido DAO Aragon Agent](https://etherscan.io/address/0x3e40D73EB977Dc6a537aF587D48316feE66E9C8c) for execution. Aragon Agent contract can update the following proxies:
  - [Locator proxy](https://etherscan.io/address/0xC1d0b3DE6792Bf6b4b37EccdcC24e45978Cfd2Eb#readContract#F1) - is the universal address book for the Lido protocol. All addresses are embedded into the implementation's bytecode as immutables for gas efficiency, allowing one to update them along with a proxy implementation. [Docs](https://docs.lido.fi/contracts/lido-locator)
  - [Staking router proxy](https://etherscan.io/address/0xFdDf38947aFB03C621C71b06C9C70bce73f12999#readContract#F1) - is a top-level controller contract for staking modules. Used to maintain a registry of staking modules, allocating stake to modules, and distribute protocol fees. [Docs](https://docs.lido.fi/contracts/staking-router)
  - [Withdrawal queue ERC721 proxy](https://etherscan.io/address/0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1#readContract#F1) - A FIFO queue for stETH withdrawal requests and an unstETH NFT implementation representing the position in the queue. [Docs](https://docs.lido.fi/contracts/withdrawal-queue-erc721/).

Monitor Safe multisig for [Emergency Brakes functions](https://docs.lido.fi/multisigs/emergency-brakes/):

- Emergency Brakes: Ethereum used to disable deposits & withdrawals for wstETH bridging to other chains (Arbitrum, Optimism, Base, Scroll, Mantle, ZKSync, Binance Smart Chain, Mode) in case of an emergency on Ethereum mainnet or the counterpart chain, and can pause Easy Track pipeline.
- GateSeal Committee - can trigger GateSeal to pause WithdrawalQueueERC721. Expires on 1 April 2025.

[Internal timelock monitoring](../timelock/README.md) for StartVote events on the [Lido Timelock](https://etherscan.io/address/0x2e59a20f205bb85a89c53f1936454680651e618e) on Mainnet.

Not monitoring [Easy track voting](https://docs.lido.fi/guides/easy-track-guide/) because it cannot trigger crucial changes. Responsible only for:

- Node Operators increasing staking limits
- Funds being allocated to LEGO program
- Funds being allocated to reward programs

## Exchange rates

Sending telegram message if the exchange rate on DEX is above [defined threshold](https://github.com/tapired/monitoring-scripts-py/blob/main/lido/steth/main.py#L7)`2%` from validator rate.

- stETH: [Curve pool](https://curve.fi/#/ethereum/pools/steth/deposit) - [0xDC24316b9AE028F1497c275EB9192a3Ea0f67022](https://etherscan.io/address/0xDC24316b9AE028F1497c275EB9192a3Ea0f67022)

[Python script code](https://github.com/tapired/monitoring-scripts-py/tree/main/lido)
