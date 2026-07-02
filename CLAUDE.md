# CLAUDE.md - Monitoring Scripts Python Codebase

## Commands

### Installation
- Create virtual environment: `uv venv`
- Activate virtual environment: `source .venv/bin/activate` (On Windows: `.venv\Scripts\activate`)
- Install dependencies:
  - Regular installation: `uv pip install .`
  - Development installation: `uv pip install -e ".[dev]"`
- Environment setup: `cp .env.example .env` (then edit with your API keys)

### Usage
- Run script: `python protocols/<protocol>/main.py`
  - Example: `python protocols/aave/main.py`

### Development
- Format code: `uv run ruff format .`
- Lint code: `uv run ruff check .`
- Fix fixable lint issues: `uv run ruff check --fix .`
- Type checking: `uv run mypy .`
- use .env file to load API keys and other environment variables
- Always pull latest main branch

## Code Style Guidelines
- **Imports**: Use ruff for organizing imports (stdlib, third-party, local)
- **Formatting**: Use ruff format with line length 120
- **Typing**: Use type hints for all functions (parameters and return values)
- **Docstrings**: Google style docstrings for all functions and classes
- **Naming**:
  - Snake case for functions/variables (`process_assets`)
  - UPPER_CASE for constants (`THRESHOLD_UR`)
  - PascalCase for classes (`ChainManager`)
- **Error handling**: Use specific exceptions with meaningful error messages
- **Web3**: Use ChainManager for connections, batch requests whenever possible
- **Configuration**: Move hardcoded values to configuration files or env variables
- **Logging**: Use structured logging instead of print statements
- **Testing**: Write unit tests for utility functions and integration tests for protocols

## Monitoring Metadata

- `monitoring.yaml` at the repo root is the source of truth for the protocol cards on https://curation.yearn.fi/monitoring/.
- When adding, removing, or changing a monitor, update `monitoring.yaml` and the protocol `README.md`.
- The alerts API exposes this metadata at `GET /v1/monitoring` (see `api/server.py`).
- `tests/test_monitoring_config.py` validates that every enabled protocol task in `automation/jobs.yaml` has a `monitoring.yaml` entry and that every task referenced there is scheduled.

## Best Practices
- Create custom exception types for domain-specific errors
- Extract repeated patterns into utility functions. Try to keep utility functions small and focused on a single task and store in utils folder.
- Use dataclasses for structured data
- Add proper type annotations to improve IDE support and catch errors
- Keep files small and focused on a single task. If a file is too large, split it into smaller files.
- Ask questions if you are not sure about the code. Ask for clarification if needed before writing the code.
- always print full addresses with links instead of truncated ones.
- Every script's `if __name__ == "__main__":` block must wrap its entrypoint with `run_with_alert` from `utils/runner.py` so an unhandled crash sends a Telegram alert and lets the CI loop continue:

  ```python
  if __name__ == "__main__":
      from utils.runner import run_with_alert

      run_with_alert(main, PROTOCOL)
  ```

  For multi-protocol scripts with no single `PROTOCOL` constant, pass a sensible default channel (e.g. `"yearn"` for ops, `"pegs"` for peg monitors).
