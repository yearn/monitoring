# Stablecoin Monitoring

## Market price ([main.py](./main.py), every 10 minutes)

Fetches DeFiLlama prices for every token in `MONITORED_TOKENS` with one request and sends a CRITICAL depeg alert to the owning protocol's channel when a price falls below its threshold. Current tokens: USDe, sUSDe (Ethena), iUSD (infiniFi), syrupUSDC, syrupUSDT, syrupUSDG (Maple) and USTB (Superstate).

Emergency dispatch needs confirmation. The first below-threshold sample still alerts, marked "Unconfirmed", but is sent with dispatch disabled. Dispatch happens once a later check reads a second, independent sample below threshold (`DISPATCH_CONFIRMATION_RUNS = 2`). A sample only counts if DeFiLlama's per-coin timestamp is newer than the last counted one and at least 5 minutes have passed since it (`BREACH_MIN_SPACING_SECONDS`), so the same datapoint served twice, or a manual re-run, cannot confirm itself. A missing DeFiLlama price neither confirms nor clears a breach. A previous breach older than 25 minutes (`BREACH_CONFIRMATION_WINDOW_SECONDS`) no longer counts, so a breach after a long gap is treated as a new first sample. A price back above threshold resets the count.

## On-chain oracles ([oracles.py](./oracles.py), hourly)

Checks the Chainlink oracles that lending markets liquidate on: staleness, round sanity and monotonicity, deviation from peg, and divergence between the oracle and the DeFiLlama market price. Rate oracles are checked for monotonicity and large deltas.
