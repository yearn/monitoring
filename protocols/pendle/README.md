# Pendle

## Governance

Pendle's important contracts are owned by a 2/4 Safe Multisig on each network. These multisigs are responsible for adding new facets to the Pendle router and configuring the yield factories. While the already deployed PT, YT, and markets are not owned by these multisigs, the SY tokens are, and these tokens are upgradable with a pause functionality that the multisigs can trigger.

Additionally, other contracts like vePENDLE, PENDLE, RewardDistributor, and Voting are also owned by these multisigs, allowing them to change parameters and trigger emergency functions within these contracts.

    Mainnet Safe Multisig: 0x8119EC16F0573B7dAc7C0CB94EB504FB32456ee1
    Arbitrum Safe Multisig: 0x7877AdFaDEd756f3248a0EBfe8Ac2E2eF87b75Ac

The owner of SY contracts was changed to [governance proxy contract](https://etherscan.io/address/0x2aD631F72fB16d91c4953A7f4260A97C2fE2f31e) with an additional guardian role that can only pause SY contracts. The governance proxy contract owner is multisig defined above.
