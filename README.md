# Monitoring Tools

[![CI](https://github.com/yearn/monitoring/actions/workflows/ci.yml/badge.svg)](https://github.com/yearn/monitoring/actions/workflows/ci.yml) [![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/yearn/monitoring)

Monitoring scripts for DeFi protocols to track key metrics and send alerts. Join the Telegram group to receive alerts: `@yearn_curation_alerts`

## Supported Protocols

- [Aave V3](./protocols/aave/README.md)
- [APYUSD](./protocols/apyusd/README.md)
- [Bad Debt](./protocols/bad-debt/)
- [Cap](./protocols/cap/README.md)
- [Compound V3](./protocols/compound/README.md)
- [Ethena](./protocols/ethena/README.md)
- [Euler](./protocols/euler/README.md) — _monitoring disabled_
- [Fluid](./protocols/fluid/README.md)
- [Infinifi](./protocols/infinifi/README.md)
- [Lido](./protocols/lido/README.md)
- [LRTs](./protocols/lrt-pegs/README.md)
- [Maple](./protocols/maple/README.md)
- [Maker DAO](./protocols/maker/README.md)
- [Morpho](./protocols/morpho/README.md)
- [Pendle](./protocols/pendle/README.md)
- [RTokens - ETH+](./protocols/rtoken/README.md)
- [Spark](./protocols/spark/README.md)
- [Strata](./protocols/strata/README.md)
- [Stargate](./protocols/stargate/README.md) — _monitoring disabled_
- [USDAI](./protocols/usdai/README.md)
- [USTB - Superstate](./protocols/ustb/README.md)
- [Yearn](./protocols/yearn/README.md)

## Cross-Protocol Monitoring

- [Timelock Alerts](./protocols/timelock/README.md) — monitors OpenZeppelin `TimelockController` contracts for `CallScheduled` events across multiple protocols and sends Telegram alerts to protocol-specific channels.
- [Safe Multisigs](./protocols/safe/main.py) — monitors Safe multisig wallets for queued transactions across multiple protocols.

## Telegram Alerts

- Invite SAM alerter bot to Telegram group using handle: `@sam_alerter_bot`

## Installation

1. **Clone the repository**

```bash
git clone https://github.com/yearn/monitoring-scripts-py.git
cd monitoring-scripts-py
```

2. **Set up virtual environment**

```bash
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. **Install dependencies**

```bash
uv pip install -e ".[dev]"
```

> Note: This project uses [uv](https://github.com/astral-sh/uv) for faster dependency installation. If you don't have uv installed, you can install it with `pip install uv` or follow the [installation instructions](https://github.com/astral-sh/uv#installation).

4. **Environment setup**

Copy and edit example environment file:

```bash
cp .env.example .env
```

## Usage

Run a specific script for a protocol. Example:

```bash
uv run protocols/aave/main.py
```

## Deployment

In production the scripts run on a schedule via supercronic on a VPS, defined in [`automation/jobs.yaml`](./automation/jobs.yaml). See [`deploy/`](./deploy/) — [`install.sh`](./deploy/install.sh) provisions a host and [`runbook.md`](./deploy/runbook.md) covers operations.

The optional read-only alerts API exposes persisted alert history from SQLite. See [`deploy/alerts-api.md`](./deploy/alerts-api.md) for endpoint examples and pagination.

## Code Style

Format and lint code with ruff:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run pytest tests/
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full style guidelines, project conventions, and instructions on adding new protocols.

## Details

For more details about this repository, check out AI generated docs using DeepWiki at [https://deepwiki.com/yearn/monitoring](https://deepwiki.com/yearn/monitoring).
