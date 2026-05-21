# AI Transaction Explainer

Generates human-readable explanations for queued governance transactions (timelocks and Safe multisigs) by combining calldata decoding, on-chain state reads, verified-source natspec, Tenderly simulation, and LLM inference.

## Architecture

```
                       ┌─────────────────────┐
                       │  Governance Alert    │
                       │  (timelock / safe)   │
                       └──────────┬──────────┘
                                  │ calldata (hex)
                                  ▼
                       ┌─────────────────────┐
                       │  Calldata Decoder    │
                       │  (4byte + eth_abi)   │
                       └──────────┬──────────┘
                                  │ DecodedCall(s)
            ┌────────────┬────────┼────────┬────────────┐
            │            │        │        │            │
            ▼            ▼        ▼        ▼            ▼
   ┌────────────┐ ┌────────────┐ ┌────┐ ┌─────────┐ ┌──────────────┐
   │  Etherscan │ │  On-chain  │ │Prox│ │Tenderly │ │  LLM Provider │
   │   source   │ │ state read │ │detec│ │   sim   │ │  (factory)    │
   │  + natspec │ │ (getters)  │ │tion │ │         │ │               │
   └─────┬──────┘ └──────┬─────┘ └─┬──┘ └────┬────┘ └──────┬────────┘
         │               │         │        │              │
         └───────────────┴─────────┴────────┴──────────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │  _build_prompt()    │
                       │  → LLM.complete()   │
                       │  → (optional)        │
                       │     refine pass     │
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │  Telegram Alert      │
                       │  🤖 AI Summary: ... │
                       └─────────────────────┘
```

## Pipeline Steps

### 1. Calldata Decoding (`utils/calldata/decoder.py`)

Converts raw hex calldata into a structured `DecodedCall`:

1. Extract the 4-byte function selector (first 4 bytes after `0x`)
2. Look up the selector in `utils/calldata/known_selectors.py` (local table)
3. If not found, query the [Sourcify 4byte API](https://api.4byte.sourcify.dev)
4. Parse the function signature to extract parameter types
5. Decode parameters using `eth_abi.decode()`

Result: `DecodedCall(function_name="upgradeTo", signature="upgradeTo(address)", params=[("address", "0x...")])`

### 2. Verified Source Context (`utils/source_context.py`)

For each `(target, function)` pair, fetches the verified Solidity source via the Etherscan v2 multichain API and extracts:

- The function's preceding natspec block + signature line
- Declarations + natspec for state variables the function writes

If the function isn't found in the target's source (e.g., the target is an `ERC1967Proxy` / `TransparentUpgradeableProxy`), follows the EIP-1967 implementation slot and retries against the impl source. Caches per `(chain_id, address)` for the workflow run.

Requires `ETHERSCAN_TOKEN`. Failures degrade gracefully — no `--- Contract Source Context ---` section is added.

### 3. On-chain Before-State (`utils/on_chain_state.py`)

For setter-style calls, reads the *current* on-chain value of state variables the function will write so the LLM can quote concrete before→after deltas instead of guessing scale.

Handles:

- **Simple public state vars** (uint*/int*/address/bool/bytes*/string) via the auto-generated no-arg getter.
- **Single-key mappings** where setter args include the mapping key type (e.g., `mapping(address => uint256) public coverageCap` paired with `setCoverageCap(address, uint256)`).
- **Diamond-storage / non-public-var setters** via a speculative getter-guess from the setter signature (last arg = value type, leading args = key types). If wrong, the eth_call reverts and is skipped gracefully.

Follows EIP-1967 proxies to locate the function source, but issues `eth_call`s against the original storage-holder address.

### 4. Tenderly Simulation (`utils/tenderly/simulation.py`)

Simulates the transaction against current on-chain state to get:

- **Success/failure** status and gas used
- **Token transfers** (ERC-20 balance changes)
- **State changes** (storage slot diffs)
- **Emitted events** (decoded log entries)

Requires `TENDERLY_API_KEY`. Simulation failure is non-blocking — the pipeline continues with the decoded calldata only.

Callers can pass `skip_simulation=True` to bypass Tenderly entirely. Used for Safe transactions with `operation=DELEGATECALL` (typically multiSend batches), where our plain-CALL simulator can't model the real execution and would produce a spurious "revert" verdict.

### 5. Proxy Upgrade Detection & Implementation Diff (`utils/proxy.py`, `utils/impl_diff.py`)

`detect_proxy_upgrade(data_hex, target)` recognizes three patterns and returns a `ProxyUpgrade(proxy_address, new_implementation)` dataclass:

| Selector | Function | Proxy address source |
|---|---|---|
| `0x3659cfe6` | `upgradeTo(address)` | tx target |
| `0x4f1ef286` | `upgradeToAndCall(address,bytes)` | tx target |
| `0x9623609d` | `upgradeAndCall(address,address,bytes)` (OZ ProxyAdmin) | first calldata arg |

For the ProxyAdmin pattern, the tx target is the ProxyAdmin and the actual proxy is inside the calldata — the Telegram alert surfaces both. Detection short-circuits on the selector check *before* calldata decoding, so non-upgrade calls don't trigger the Sourcify 4byte lookup.

When an upgrade is detected the pipeline:

1. Reads the **current implementation** from the EIP-1967 storage slot (`0x360894a...`) of the proxy.
2. Builds an Etherscan diff URL: `etherscan.io/contractdiffchecker?a1=old&a2=new`.
3. Fetches the verified source of **both** implementations and runs a structural diff (`utils/impl_diff.py`):
   - **Functions added / removed / changed visibility or modifiers**, identified by name + arg types so overloads are distinct.
   - **Storage layout safety check** — slot-by-slot comparison. Safe iff the new layout begins with the old layout in the same order (append-only) OR an OZ trailing `uintN[K] __gap` array is consumed: any new vars inserted before the gap must be matched by an equal reduction in the gap's size. Gap underflow, no-shrink, and gap removal without consumption are flagged.
   - **EIP-7201 namespaced storage** is detected (`_getXxxStorage() returns (XxxStorage storage $)`) and the positional layout check is skipped — namespaced storage lives at a constant slot, not slot 0+.
   - Immutable/`constant` state vars are excluded from the layout check (they don't occupy a storage slot).
   - State vars without an explicit visibility modifier (default-internal) ARE included — function locals are excluded by brace-depth tracking rather than by requiring a visibility keyword.

The structural diff is injected into the prompt under `--- Implementation Diff ---`. Best-effort: if either impl is unverified or extraction fails, the diff section is silently omitted but the rest of the upgrade context still renders.

### 6. LLM Prompt & Completion (`utils/llm/ai_explainer.py`)

The prompt is built from all available context. The system prompt enforces brevity:

- Starts with a verb, no "This transaction…" preamble
- Trailing risk tag in caps (LOW / MEDIUM / HIGH / CRITICAL)
- Refuses to assume parameter units from function name alone
- Trusts source-context natspec over prior assumptions
- Quotes concrete before→after deltas when state reads are available

Example assembled prompt:

```
System: You are a DeFi risk analyst writing alerts for a monitoring team...
        (brevity rules, unit-interpretation rules)

Protocol: AAVE
Contract: Aave Governance V3
Target: 0x...

--- Execution Context ---             (optional, e.g. for DELEGATECALL via MultiSendCallOnly)
Outer call is DELEGATECALL from the Safe (0x...) into ...

--- Decoded Calldata ---
Call 1: upgradeTo(address)
  address: 0xNewImpl

--- Shared Across Batch ---           (optional, for batch txs with uniform args)
  arg[0] (address) is identical across all 4 calls: '0x...'

--- Contract Source Context ---       (Etherscan natspec)
Contract: PoolAddressesProvider
/// @notice Updates the impl of pool...
function upgradeTo(address newImpl) external onlyAdmin { ... }

--- Current State (before this call) ---
On 0x...:
  poolImpl = 0xOldImpl  // current value, type: address

--- Proxy Upgrade ---
This is a PROXY UPGRADE on 0xProxy.
Current implementation: 0xOldImpl
New implementation: 0xNewImpl
Diff: https://etherscan.io/contractdiffchecker?a1=...&a2=...

Old: 0xOldImpl (PoolImplV1)
New: 0xNewImpl (PoolImplV2)

Functions added (2):
  + emergencyPause() external onlyOwner
  + setOracle(address) external onlyOwner

Storage layout safe (append-only). New state vars at end:
  + address public oracle

--- Simulation Results ---
Simulation: SUCCESS
Gas used: 50,000
...
```

The full prompt is logged at INFO level for debugging.

### 7. Optional Refine Pass

When `refine=True` is passed to `explain_transaction` / `explain_batch_transaction`, a second LLM call critiques the draft against a checklist (verb-leading TLDR, supported units, risk-magnitude consistency) and revises only if it finds concrete issues. Hard rules forbid introducing new unit assumptions, removing hedges, escalating LOW out of caution, or style-only churn. Falls back to the draft on `PASS`, on any `LLMError`, or on an empty revision.

Cost: ~2× LLM calls per alert when enabled. Default is **off**.

### 8. Dual-Output Parsing

The LLM is asked to return two sections:

```
TLDR: Upgrades AAVE pool impl 0xOld → 0xNew. Verify audited. MEDIUM.

DETAIL:
Calls upgradeTo(address) on the AAVE pool proxy...
Current implementation: 0xOld...
New implementation: 0xNew...
```

`_parse_explanation()` splits this into an `Explanation` dataclass:
- `summary` (from TLDR) — short, goes to Telegram
- `detail` (from DETAIL) — thorough analysis, uploaded to a paste service and linked from the Telegram message

If the LLM doesn't follow the format, the full response is used as the summary (backward compatible).

### 9. Output Formatting

`format_explanation_line()` uses only the summary for the Telegram message:

```
🤖 *AI Summary:*
Upgrades AAVE pool impl 0xOld → 0xNew. Verify audited. MEDIUM.
[Full details](https://dpaste.org/abc123)
```

The "Full details" link points to a dpaste.org upload with the detailed analysis.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `venice` | Provider name: `venice`, `groq`, `openai`, `anthropic`, or custom |
| `LLM_API_KEY` | *(required)* | API key for the LLM provider |
| `LLM_MODEL` | `deepseek-v4-flash` | Model identifier |
| `LLM_BASE_URL` | *(per provider)* | API base URL (not needed for anthropic) |
| `ETHERSCAN_TOKEN` | *(optional)* | Etherscan v2 multichain API key for source context |
| `TENDERLY_API_KEY` | *(optional)* | Tenderly API key for simulation |
| `TENDERLY_ACCOUNT` | `yearn` | Tenderly account slug |
| `TENDERLY_PROJECT` | `sam` | Tenderly project slug |

### Supported Providers

| Provider | Base URL | Default Model | Package |
|---|---|---|---|
| Venice.ai | `https://api.venice.ai/api/v1` | `deepseek-v4-flash` | `openai` |
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-safeguard-20b` | `openai` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | `openai` |
| Anthropic | *(native API)* | `claude-haiku-4-5-20251001` | `anthropic` |
| Custom | Set `LLM_BASE_URL` | Set `LLM_MODEL` | `openai` |

The `openai` and `anthropic` packages are optional dependencies. Install with:

```bash
uv pip install 'monitoring-scripts-py[ai]'
```

## Module Structure

```
utils/llm/
├── __init__.py              # Exports: LLMProvider, get_llm_provider
├── ai_explainer.py          # Orchestrator: decode → fetch context → prompt → explain
├── anthropic_provider.py    # Anthropic (Claude) native API provider
├── base.py                  # Abstract LLMProvider base class + LLMError
├── factory.py               # Provider factory with env-based config + singleton
├── openai_compat.py         # OpenAI-compatible provider (Venice, OpenAI, etc.)
└── README.md                # This file

utils/source_context.py      # Etherscan v2 source fetch + natspec extractor + proxy follow
utils/on_chain_state.py      # Before-state reader (auto-generated getters, mappings, diamond storage)
utils/proxy.py               # EIP-1967 impl slot read + proxy-upgrade detection (3 selectors)
utils/impl_diff.py           # Structural old-vs-new impl diff (functions, storage layout, gap-aware)
utils/tenderly/simulation.py # Tenderly Simulation API client
utils/calldata/              # Selector resolver + ABI decoder
safe/multisend.py            # Safe MultiSendCallOnly inner-call extractor + DELEGATECALL context note
```

## Integration Points

- **Timelock alerts** (`timelock/timelock_alerts.py`): Calls `explain_transaction()` or `explain_batch_transaction()` for each scheduled operation.
- **Safe alerts** (`safe/main.py`): Routes through `_explain_safe_tx()`, which detects `operation=DELEGATECALL` multisend batches and dispatches to `explain_batch_transaction()` with `skip_simulation=True` and a DELEGATECALL context note. Plain CALL Safe txs use `explain_transaction()` as before.
- Both call sites use `format_explanation_line()` to append the AI summary to Telegram messages.
- Both call sites can opt into the refine pass per-protocol by passing `refine=True` to the explainer.
