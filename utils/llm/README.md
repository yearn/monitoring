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
2. When the call's `target` + `chain_id` are known, resolve the signature from the target's **verified ABI** first (`get_function_signature_by_selector`, EIP-1967/getter proxy-aware) — more reliable than 4byte, which can't disambiguate selector collisions
3. Otherwise look up the selector in `utils/calldata/known_selectors.py` (local table)
4. If still unresolved, query the [Sourcify 4byte API](https://api.4byte.sourcify.dev)
5. Parse the function signature to extract parameter types
6. Decode parameters using `eth_abi.decode()`

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

**State overrides** (`state_objects`) reduce false reverts. When a call forwards ETH (`value > 0`), the executor (timelock/Safe) often doesn't hold that balance, so a faithful sim would revert with "insufficient funds" — a false negative. `_merge_balance_override` grants the sender exactly the forwarded `value`. Callers can pass additional overrides (e.g. a role/owner storage slot) to unblock access-gated setters; caller-supplied values win on conflict.

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

1. Reads the **current implementation** from the EIP-1967 storage slot (`0x360894a...`) of the proxy, falling back to the legacy zeppelinos slot (`0x7050c9e...`) for pre-EIP-1967 proxies like USDC's `FiatTokenProxy`.
2. Builds an Etherscan diff URL: `etherscan.io/contractdiffchecker?a1=old&a2=new`.
3. Fetches the verified source of **both** implementations and runs a structural diff (`utils/impl_diff.py`):
   - **Functions added / removed / changed visibility or modifiers**, identified by name + arg types so overloads are distinct.
   - **Storage layout safety check** — slot-by-slot comparison. Safe iff the new layout begins with the old layout in the same order (append-only) OR an OZ trailing `uintN[K] __gap` array is consumed: any new vars inserted before the gap must be matched by an equal reduction in the gap's size. Gap underflow, no-shrink, and gap removal without consumption are flagged.
   - **EIP-7201 namespaced storage** is detected (`_getXxxStorage() returns (XxxStorage storage $)`) and the positional layout check is skipped — namespaced storage lives at a constant slot, not slot 0+.
   - Immutable/`constant` state vars are excluded from the layout check (they don't occupy a storage slot).
   - State vars without an explicit visibility modifier (default-internal) ARE included — function locals are excluded by brace-depth tracking rather than by requiring a visibility keyword.

The structural diff is injected into the prompt under `--- Implementation Diff ---`. Best-effort: if either impl is unverified or extraction fails, the diff section is silently omitted but the rest of the upgrade context still renders.

### 5b. Deterministic Safety Checks (`utils/llm/ai_explainer.py`, `utils/source_context.py`)

`_collect_safety_checks()` runs seatbelt-style checks grounded in Etherscan data we already fetch, and surfaces them as hard facts under `--- Safety Checks ---`:

- **Unverified target** — a governance tx whose target has no published source is a red flag (`get_verification_status` returns a tri-state; the note is emitted only on an explicit `False`, never on a fetch error, so it doesn't cry wolf).
- **ETH to a non-payable function** — forwarding `value > 0` to a `nonpayable` function reverts and can strand funds (`get_function_state_mutability`, with EIP-1967 proxy follow; overloaded functions resolve to `payable` if any overload accepts value).

The system prompt instructs the LLM to treat each item as verified and reflect it in the verdict (e.g. an unverified target is at least MEDIUM).

### 6. LLM Prompt & Completion (`utils/llm/ai_explainer.py`)

The prompt is split into a **system** prompt (static instructions) and a **user** prompt (per-tx context). `complete(prompt, system_prompt=...)` passes the system block via the provider's native system role, which improves instruction-following and lets the Anthropic provider mark it `cache_control: ephemeral` — repeated alerts within the cache window pay for the (large) instruction prompt only once. The static block (`SYSTEM_INSTRUCTIONS`) enforces brevity:

- Starts with a verb, no "This transaction…" preamble
- Trailing risk tag in caps (LOW / MEDIUM / HIGH / CRITICAL)
- Refuses to assume parameter units from function name alone
- Trusts source-context natspec over prior assumptions
- Quotes concrete before→after deltas when state reads are available
- Flags any divergence between a proposal's **stated intent** and the decoded actions

When a `description` is passed to the explainer, it renders under `--- Stated Intent (proposal description) ---` and the LLM compares stated intent against the calldata, flagging undisclosed role/ownership/upgrade or fund-movement changes.

Example assembled prompt:

```
System (sent via native system role, cached):
        You are a DeFi risk analyst writing alerts for a monitoring team...
        (brevity rules, unit-interpretation rules)

Protocol: AAVE
Contract: Aave Governance V3
Target: 0x...

--- Execution Context ---             (optional, e.g. for DELEGATECALL via MultiSendCallOnly)
Outer call is DELEGATECALL from the Safe (0x...) into ...

--- Stated Intent (proposal description) ---  (optional, when a description is supplied)
Upgrade the pool implementation to add an emergency pause.

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

--- Safety Checks ---                 (optional, deterministic seatbelt-style checks)
- 0xNewImpl is UNVERIFIED on Etherscan — source is not published; the call cannot be inspected.

--- Simulation Results ---
Simulation: SUCCESS
Gas used: 50,000
...
```

The full prompt is logged at INFO level for debugging.

### 7. Optional Refine Pass

When `refine=True` is passed to `explain_transaction` / `explain_batch_transaction`, a second LLM call critiques the draft against a checklist (verb-leading TLDR, supported units, risk-magnitude consistency) and revises only if it finds concrete issues. Hard rules forbid introducing new unit assumptions, removing hedges, escalating LOW out of caution, or style-only churn. Falls back to the draft on `PASS`, on any `LLMError`, or on an empty revision.

Cost: ~2× LLM calls per alert when enabled. Default is **off**.

### 8. Dual-Output: Structured or Text

`_generate_draft()` produces the `Explanation` dataclass (`summary` → Telegram, `detail` → paste service) via one of two paths:

**Structured output (preferred).** When the provider advertises `supports_structured_output`, the draft is requested as JSON matching `EXPLANATION_SCHEMA`:

```json
{ "summary": "Upgrades AAVE pool impl 0xOld → 0xNew. Verify audited.",
  "detail": "Calls upgradeTo(address) on the AAVE pool proxy...",
  "risk_tag": "MEDIUM" }
```

`risk_tag` is `enum`-constrained to `LOW/MEDIUM/HIGH/CRITICAL`, so the Telegram tag is always valid — no regex extraction. OpenAI-compatible providers use `response_format: json_schema`; the Anthropic provider uses a forced tool call. `_explanation_from_json` maps the object to `Explanation`, appending `risk_tag` to the summary if the model didn't inline it.

**Text fallback.** If structured output is disabled, fails, or returns an empty summary, the draft falls back to a plain `complete()` call returning:

```
TLDR: Upgrades AAVE pool impl 0xOld → 0xNew. Verify audited. MEDIUM.

DETAIL:
Calls upgradeTo(address) on the AAVE pool proxy...
```

`_parse_explanation()` splits this with tolerant regex (handles `### DETAIL`, `**TLDR:**`, etc.); if the format isn't followed, the whole response becomes the summary (backward compatible).

Structured output is controlled by `LLM_STRUCTURED_OUTPUT` (per-provider default: on for `anthropic`/`codex`/`openai`/`venice`; off for `groq`/custom, since JSON-schema support varies by backend). The refine pass (step 7) always uses the text path.

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
| `LLM_PROVIDER` | `venice` | Provider name: `venice`, `groq`, `openai`, `anthropic`, `codex`, or custom |
| `LLM_API_KEY` | *(required except codex)* | API key for the LLM provider. For `codex`, omitted means reuse existing Codex auth |
| `LLM_MODEL` | `deepseek-v4-flash` | Model identifier |
| `LLM_BASE_URL` | *(per provider)* | API base URL (not needed for anthropic) |
| `LLM_STRUCTURED_OUTPUT` | *(per provider)* | `true`/`false` to force JSON-schema output. Default: on for anthropic/codex/openai/venice, off for groq/custom |
| `LLM_CODEX_MODEL_PROVIDER` | *(unset)* | Optional Codex SDK model-provider override |
| `LLM_CODEX_CWD` | *(current process cwd)* | Optional Codex runtime working directory |
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
| Codex | *(native SDK)* | `gpt-5.2-codex` | `openai-codex` |
| Custom | Set `LLM_BASE_URL` | Set `LLM_MODEL` | `openai` |

The `openai`, `anthropic`, and `openai-codex` packages are optional dependencies. Install with:

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
├── codex_provider.py        # OpenAI Codex Python SDK provider
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

## Eval Harness

`tests/eval/` guards the prompt/pipeline against regressions with real mainnet fixtures (`fixtures.py`) and tolerant assertions — a risk tag within an acceptable band plus required/forbidden substrings, since LLM output isn't deterministic.

It makes live LLM + Etherscan + RPC calls (costs money), so it's **excluded from the default test suite**. Run it after prompt or pipeline changes:

```bash
python -m tests.eval.run_eval                          # standalone report, non-zero exit on failure
RUN_LLM_EVAL=1 python -m pytest tests/eval -v          # same cases as parametrized tests
```

Add a fixture whenever a prompt change fixes a specific failure mode (e.g. the intent-mismatch case asserts a misleading "no changes" description can't downgrade a real parameter change below MEDIUM).
