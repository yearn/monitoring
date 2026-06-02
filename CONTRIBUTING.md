# Contributing

## Getting Started

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env  # then edit with your API keys
```

## Development Commands

```bash
uv run ruff format .          # format code
uv run ruff check --fix .     # lint + autofix
uv run pytest tests/          # run tests
```

## Project Structure

Each protocol lives in its own directory with a `main.py` entry point:

```
protocol-name/
  main.py          # entry point
  abi/             # contract ABIs (if needed)
  README.md        # protocol-specific docs
```

Shared utilities live in `utils/`:

| Module | Purpose |
|---|---|
| `utils/logging.py` | Structured logging via `get_logger(name)` |
| `utils/telegram.py` | Telegram alert delivery |
| `utils/cache.py` | File-based key:value persistence |
| `utils/web3_wrapper.py` | Web3 connection management (`ChainManager`) |
| `utils/config.py` | Environment config (`Config`) |
| `utils/formatting.py` | Number formatting helpers (`format_usd`, `format_token_amount`) |
| `utils/http.py` | HTTP request helper (`fetch_json`) |
| `utils/chains.py` | Chain enum and explorer URLs |
| `utils/abi.py` | ABI loader |
| `utils/gauntlet.py` | Gauntlet risk parameter helpers |
| `utils/runner.py` | `run_with_alert` — script entrypoint wrapper with crash-alert telemetry |

## Code Style

### General

- **Line length**: 120 characters (configured in `pyproject.toml`)
- **Python version**: 3.10+
- **Formatter/linter**: ruff (run both format and check before committing)
- **Naming**: `snake_case` for functions/variables, `UPPER_CASE` for constants, `PascalCase` for classes

### Logging

Use structured logging everywhere — never use `print()`.

```python
from utils.logging import get_logger

logger = get_logger("protocol_name")

# Use %s formatting (not f-strings) for lazy evaluation
logger.info("Processing %s assets on %s", len(assets), chain.name)
logger.error("Failed to fetch data: %s", error)
```

### PROTOCOL Constant

Each monitor defines a lowercase `PROTOCOL` constant used for telegram credential lookup and cache keys:

```python
PROTOCOL = "aave"  # lowercase — telegram.py calls .upper() internally
logger = get_logger(PROTOCOL)
```

### Type Hints

Add type annotations to all function signatures:

```python
def process_assets(chain: Chain, threshold: float = 0.99) -> None:
    ...

def get_price(token_address: str) -> float | None:
    ...
```

### Telegram Alerts

Use `send_telegram_message` from `utils/telegram.py`. It looks up `TELEGRAM_BOT_TOKEN_{PROTOCOL.upper()}` and `TELEGRAM_CHAT_ID_{PROTOCOL.upper()}` from environment variables:

```python
from utils.telegram import send_telegram_message

send_telegram_message("Alert text here", PROTOCOL)
```

When interpolating uncrafted input (exception messages, API responses, on-chain strings that may contain `_*[\``), pass `plain_text=True` so Telegram doesn't try to parse Markdown on it:

```python
except requests.RequestException as e:
    send_telegram_message(f"Fetch failed: {e}", PROTOCOL, disable_notification=True, plain_text=True)
```

### Script Entrypoint

Wrap each script's entrypoint with `run_with_alert` from `utils/runner.py`. On any unhandled exception it sends a plain-text Telegram crash alert (with the script name and a link to the failing run, when available) and returns normally — so a CI shell loop running multiple scripts continues to the next one instead of aborting the whole run.

```python
if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
```

For multi-protocol scripts with no single `PROTOCOL` constant (e.g. `timelock_alerts.py`, `safe/main.py`), pass a sensible default channel as a string: `"yearn"` for general ops, `"pegs"` for peg monitors.

### Web3 / RPC Calls

Use `ChainManager` for connections and batch requests whenever possible:

```python
from utils.chains import Chain
from utils.web3_wrapper import ChainManager

client = ChainManager.get_client(Chain.MAINNET)

with client.batch_requests() as batch:
    batch.add(contract.functions.totalSupply())
    batch.add(contract.functions.balanceOf(address))
    responses = client.execute_batch(batch)
```

### Caching

Use `utils/cache.py` for persisting state between runs (e.g. last processed timestamp or proposal ID):

```python
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file

value = get_last_value_for_key_from_file(cache_filename, "MY_KEY")
write_last_value_to_file(cache_filename, "MY_KEY", new_value)
```

## Adding a New Protocol

1. Create `protocol-name/main.py` following the pattern above, with a `main()` function and an `if __name__ == "__main__":` block that wraps it via `run_with_alert(main, PROTOCOL)` (see [Script Entrypoint](#script-entrypoint))
2. Add a `protocol-name/README.md` describing what it monitors
3. Add `"protocol-name"` to the `packages` list in `pyproject.toml` under `[tool.setuptools]`
4. Add the corresponding `TELEGRAM_BOT_TOKEN_*` and `TELEGRAM_CHAT_ID_*` entries to `.env.example`
5. Add the protocol to the CI workflow if it should run on a schedule

## Tests

Tests live in `tests/`. Run them with:

```bash
uv run pytest tests/
```

When writing tests, patch `utils.telegram.logger` (not `builtins.print`) for assertion on log output.
