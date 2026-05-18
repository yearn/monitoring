# USTB (Superstate) Monitoring

Monitors the [Superstate Short Duration US Government Securities Fund (USTB)](https://superstate.co/ustb) on Ethereum Mainnet.

[Risk Score Report](https://curation.yearn.fi/report/superstate-ustb/)

## Structure

- `main.py`: NAV/Share, redemption capacity, supply changes, and oracle staleness checks. Run hourly via GitHub Actions.
- Stablecoin price check is in [`stables/main.py`](../stables/main.py).

## Contract Addresses

| Contract               | Address                                                                                                               |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------- |
| USTB Token (Proxy)     | [`0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e`](https://etherscan.io/address/0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e) |
| Continuous Price Oracle| [`0xe4fa682f94610ccd170680cc3b045d77d9e528a8`](https://etherscan.io/address/0xe4fa682f94610ccd170680cc3b045d77d9e528a8) |
| Chainlink Oracle       | [`0x289B5036cd942e619E1Ee48670F98d214E745AAC`](https://etherscan.io/address/0x289B5036cd942e619E1Ee48670F98d214E745AAC) |
| RedemptionIdle (Proxy) | [`0x4c21b7577c8fe8b0b0669165ee7c8f67fa1454cf`](https://etherscan.io/address/0x4c21b7577c8fe8b0b0669165ee7c8f67fa1454cf) |

## Alerts

### NAV/Share (Continuous Oracle + Chainlink)

- **NAV decrease**: CRITICAL alert if the latest oracle checkpoint NAV is lower than the previous one — indicates fund losses.
- **Oracle divergence**: HIGH alert if the Continuous Price Oracle and Chainlink feed differ by more than **0.5%**.

### Supply Changes

- **Large supply change**: HIGH alert if USTB total supply changes by more than **+/-10%** compared to the previous hourly run.

### Oracle Staleness

- **Stale oracle**: HIGH alert if the latest checkpoint `effectiveAt` timestamp is more than **4 days** (345,600s) old. The SuperstateOracle reverts at 5 days (432,000s), so this provides a 1-day buffer.

### Stablecoin Price (via stables)

- **USTB price drop**: CRITICAL alert if DeFiLlama price falls below **$10.50** (monitored in `stables/main.py`).

## Resources

- [Superstate Docs](https://docs.superstate.com/)
- [Smart Contracts](https://docs.superstate.com/welcome-to-superstate/smart-contracts)
- [Chainlink USTB NAV Feed](https://data.chain.link/feeds/ethereum/mainnet/ustb-nav)
