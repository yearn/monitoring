# Fetch and validate a Morpho market oracle

Use this when adding or checking a row in `morpho/markets.py`: resolve the **oracle address** and **feed stack** from the API, then confirm on-chain.

## 1. What you need

- **Market `uniqueKey`**: 66-char hex `0x…` from the Morpho app URL path, e.g.  
  `https://app.morpho.org/ethereum/market/0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd/weth-usdc` →  
  `uniqueKey = 0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd`
- **Chain `chainId`** for the GraphQL query (must match the network in the URL):

| Network   | `chainId` |
|-----------|-----------|
| Ethereum  | `1`       |
| Base      | `8453`    |
| Polygon   | `137`     |
| Arbitrum  | `42161`   |
| Katana    | `747474`  |

- **RPC URL** for that chain (public node is fine for read-only calls).
- `cast` from [Foundry](https://book.getfoundry.sh/) (or any Web3 client that can call view functions).

## 2. Fetch market + oracle from Morpho GraphQL

Endpoint: `POST https://api.morpho.org/graphql` with `Content-Type: application/json`.

Minimal query (inline `uniqueKey` and `chainId`):

```json
{
  "query": "{ marketByUniqueKey(uniqueKey: \"0xYOUR_KEY\", chainId: 1) { loanAsset { symbol address } collateralAsset { symbol address } lltv oracle { address } } }"
}
```

Check:

- **`collateralAsset` / `loanAsset`**: match the pair you expect.
- **`lltv`**: `int(lltv) / 1e18` for human-readable LLTV (e.g. `860000000000000000` → 86%).
- **`oracle.address`**: address to read on-chain in the next step.

If the API returns `null`, the `uniqueKey` or `chainId` is wrong.

## 3. On-chain: `MorphoChainlinkOracleV2` (most common)

Most Morpho oracles on this repo expose the same layout. Read each address; `0x0000000000000000000000000000000000000000` means “unused”.

```bash
RPC=https://ethereum.publicnode.com   # or your chain’s RPC
O=0x0F948CBa8231Db7898ef36A4212581Ad7b1B4580   # oracle.address from API

for sig in BASE_VAULT QUOTE_VAULT BASE_FEED_1 BASE_FEED_2 QUOTE_FEED_1 QUOTE_FEED_2; do
  raw=$(cast call "$O" "${sig}()(address)" --rpc-url "$RPC" | tail -1)
  addr="0x${raw: -40}"
  echo "$sig: $addr"
done
```

For every **non-zero** feed or vault address, call `description()(string)` when the contract supports it (Chainlink-style aggregators / proxies):

```bash
cast call "$FEED" "description()(string)" --rpc-url "$RPC"
```

If you see a **proxy** (e.g. `aggregator()(address)`), follow to the aggregator and still call `description()` on the feed contract you get from the oracle first.

**How to interpret:**

- **Collateral leg** usually uses `BASE_FEED_1` / `BASE_FEED_2` (and sometimes `BASE_VAULT` for ERC4626 rate).
- **Loan asset** often uses `QUOTE_FEED_1` / `QUOTE_FEED_2` (and `QUOTE_VAULT`).
- **All quote feeds zero** often means the loan asset is treated as **$1** (e.g. USDC) inside the oracle; say so in the comment.
- **Vaults non-zero**: document the vault rate (ERC4626 / Maple / etc.) and any external price feeds.

## 4. Identify feed provider (Chainlink, RedStone, Chronicle, API3, or unknown)

Morpho can wire **Chainlink**, **RedStone**, **Chronicle**, **API3** (dAPI / beacon), or custom contracts. Treat classification as **best-effort** until you cross-check an official list or explorer.

### 4.1 Fast on-chain signals

For each non-zero feed address `FEED`:

1. **`description()(string)`** (if it does not revert):

   - String contains **`RedStone`** / **`Redstone`** / **`redstone`** → label **RedStone** (RedStone often embeds the name in the feed contract).
   - String contains **`Chronicle`** → strong hint for **Chronicle**; still match the address on [Chronicle’s oracle dashboard](https://chroniclelabs.org/dashboard/oracles) when possible.
   - Descriptions like **`ETH / USD`**, **`BTC / USD`** without a vendor prefix are *often* Chainlink-style proxies, but **not proof**—many stacks reuse similar wording.

2. **Chainlink-style proxy** — try:

   ```bash
   cast call "$FEED" "aggregator()(address)" --rpc-url "$RPC"
   ```

   If this returns a **non-zero** address, the contract is very likely a **Chainlink EACAggregatorProxy** (or clone): follow-up with `description()` on the **proxy** you already have, and optionally inspect the **aggregator** on a block explorer. This pattern is typical of [Chainlink data feeds](https://docs.chain.link/data-feeds/using-data-feeds).

3. **`latestRoundData(uint80)`** — `AggregatorV3Interface` is implemented by Chainlink aggregators and many proxies. If `decimals()` and `latestRoundData()` work on the address you call from Morpho, it is consistent with **Chainlink-style** pricing, but **RedStone** and other feeds can also expose compatible interfaces—so do not rely on this alone.

4. **API3 (dAPI proxy)** — many API3 consumer contracts implement **`IProxy`**: try `api3ServerV1()(address)` and `dapiName()(bytes32)` (exact ABI can vary by deployment). If a non-zero `api3ServerV1` appears, cross-check the feed on [Api3 Market](https://market.api3.org/) by chain and asset.

5. If `description()` reverts and `aggregator()` reverts, open the contract on a **block explorer** (verified source, contract name, `implementation`) and classify from code or **unknown** until you confirm.

### 4.2 Cross-check against official listings (recommended)

**Chainlink**

- **Docs (authoritative address tables):** [Price Feed Contract Addresses](https://docs.chain.link/data-feeds/price-feeds/addresses) — pick the **network** (e.g. Ethereum Mainnet), then use the page search (**Ctrl/Cmd+F**) for the feed **address** or pair name (e.g. `ETH / USD`). If the **proxy address** matches a row, you can document **Chainlink** with confidence.
- **Feed explorer (human-friendly):** [data.chain.link](https://data.chain.link) — browse by chain/asset (e.g. [ETH/USD on Ethereum mainnet](https://data.chain.link/feeds/ethereum/mainnet/eth-usd)) and compare the **contract address** to your `FEED`.
- **ENS (optional):** Some feeds are documented under `*.data.eth` names; see [Using ENS with Data Feeds](https://docs.chain.link/data-feeds/ens).

There is **no stable public JSON API** documented here for “download all Chainlink feed addresses” in this repo; use the docs page or data.chain.link and match **by address**.

**RedStone**

- **Documentation:** [RedStone price feeds / integration docs](https://docs.redstone.finance/) (navigation varies; search for your asset and chain).
- **App:** [RedStone app — feeds](https://app.redstone.finance/) — find the feed for your chain and compare **contract address** to `FEED`.
- On-chain, RedStone feeds often self-identify in **`description()`** (see §4.1).

**Chronicle**

- **Oracle dashboard:** [Chronicle — Oracles](https://chroniclelabs.org/dashboard/oracles) — switch Mainnets/Testnets, use filters/search, and compare **contract address** to your `FEED`.
- **Protocol:** [Chronicle Protocol](https://chroniclelabs.org/) (docs, proof of asset, etc.).

**API3**

- **Market / feed catalog:** [Api3 Market](https://market.api3.org/) — browse by network and data feed; **match the on-chain proxy or oracle address** to the deployment listed for your chain.
- **Docs:** [api3.org](https://api3.org/) (dAPIs, Airnode, integration guides).

### 4.3 What to write in `markets.py`

- If **Chainlink** is confirmed (address on [docs](https://docs.chain.link/data-feeds/price-feeds/addresses) or [data.chain.link](https://data.chain.link) for that network): e.g. `Chainlink ETH/USD`, `Chainlink USDC/USD`.
- If **RedStone** is confirmed (docs/app + `description()`): e.g. `RedStone Price Feed for …`.
- If **Chronicle** is confirmed (address on [Chronicle oracles dashboard](https://chroniclelabs.org/dashboard/oracles) or `description()`): e.g. `Chronicle …/USD`.
- If **API3** is confirmed ([Api3 Market](https://market.api3.org/) + `api3ServerV1` / docs): e.g. `API3 …/USD` or name the dAPI.
- If you only have `description()` and proxy shape but no registry match: e.g. `Chainlink-compatible ETH/USD (verify on docs.chain.link)` or **unknown** until verified.

## 5. When the oracle is not `MorphoChainlinkOracleV2`

If `cast` reverts on `BASE_FEED_1()(address)"`, the contract may be a different Morpho oracle type (custom adapter, Pendle, MetaOracle, etc.):

1. Open the oracle on the chain explorer (e.g. [Etherscan](https://etherscan.io/)).
2. Read **Contract** → **Read Contract** / **Code** / verified ABI.
3. Look for price sources: `feed`, `aggregator`, `oracle`, `vault`, `primaryOracle`, `fallbackOracle`, etc.
4. Document in the same style as neighboring rows in `markets.py`.

## 6. Validate against `markets.py`

- **LLTV** in the comment matches `int(lltv)/1e18` from the API.
- **Asset symbols** match the API (collateral vs loan order in comments: `COLLATERAL/LOAN`).
- **Oracle description** matches what you read: feed names from `description()`, vaults named explicitly, and note when a stable feed is missing but implied.

## 7. Quick copy-paste (Python + GraphQL)

```python
import json, urllib.request

def market_by_unique_key(unique_key: str, chain_id: int) -> dict:
    body = {
        "query": (
            '{ marketByUniqueKey(uniqueKey: "%s", chainId: %d) '
            "{ loanAsset { symbol address } collateralAsset { symbol address } lltv oracle { address } } }"
        )
        % (unique_key, chain_id)
    }
    req = urllib.request.Request(
        "https://api.morpho.org/graphql",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]["marketByUniqueKey"]
```

## References

- Morpho app (markets): `https://app.morpho.org`
- GraphQL: `https://api.morpho.org/graphql`
- Local schema snapshot: `morpho/morpho_ql_schema.txt` (oracle union types include `MorphoChainlinkOracleData` / `MorphoChainlinkOracleV2Data`).
- Chainlink: [Price Feed Contract Addresses](https://docs.chain.link/data-feeds/price-feeds/addresses), [data.chain.link](https://data.chain.link), [Using Data Feeds](https://docs.chain.link/data-feeds/using-data-feeds).
- RedStone: [docs.redstone.finance](https://docs.redstone.finance/), [app.redstone.finance](https://app.redstone.finance/).
- Chronicle: [Chronicle Oracles dashboard](https://chroniclelabs.org/dashboard/oracles), [Chronicle Protocol](https://chroniclelabs.org/).
- API3: [Api3 Market](https://market.api3.org/), [api3.org](https://api3.org/).
