# Known bugs

- **Resolved — Infinifi L2 mismatch includes transfers awaiting settlement:** Moving
  liquidity out of the mainnet vault raises its mirrored L2 value before the assets
  reach L2 farms; the API mismatch check was removed because it could not reconcile
  transfers awaiting CCTP confirmations or keeper settlement.

- **Resolved — Infinifi L2 mismatch excludes maturity farms:** The `isL2LiquidityFarm`
  filter omitted Monad's August USDC maturity farm despite its inclusion in on-chain
  accounting; the API mismatch comparison has been removed.
- **Resolved — Infinifi vault alerts assume only assets updates change report values:**
  `portalAssetsReport` subtracts current local liquidity from farm shares; alert text
  now explains that token transfers can change the value without an assets update.

- **Resolved — oversized individual stale-operation alerts:** The ten-call cap did not
  bound decoded signature lengths, allowing Telegram to truncate details before caching;
  call details now fit the message budget with an explicit omitted-call count, and
  oversized entries are rejected before delivery.
