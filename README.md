# Monitoring Tools

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/yearn/monitoring-scripts-py) [![Hourly Monitoring Scripts](https://github.com/yearn/monitoring-scripts-py/actions/workflows/hourly.yml/badge.svg)](https://github.com/yearn/monitoring-scripts-py/actions/workflows/hourly.yml) [![Daily Monitoring Scripts](https://github.com/yearn/monitoring-scripts-py/actions/workflows/daily.yml/badge.svg)](https://github.com/yearn/monitoring-scripts-py/actions/workflows/daily.yml) [![Safe Multisigs](https://github.com/yearn/monitoring-scripts-py/actions/workflows/multisig-checker.yml/badge.svg)](https://github.com/yearn/monitoring-scripts-py/actions/workflows/multisig-checker.yml)

Monitoring scripts for DeFi protocols to track key metrics and send alerts. Join the Telegram group to receive alerts: `@yearn_curation_alerts`

## Supported Protocols

- [Aave V3](./aave/README.md)
- [Bad Debt](./bad-debt/)
- [Cap](./cap/README.md)
- [Compound V3](./compound/README.md)
- [Ethena](./ethena/README.md)
- [Euler](./euler/README.md) — _monitoring disabled_
- [Fluid](./fluid/README.md)
- [Infinifi](./infinifi/README.md)
- [Lido](./lido/README.md)
- [LRTs](./lrt-pegs/README.md)
- [Maple](./maple/README.md)
- [Maker DAO](./maker/README.md)
- [Moonwell](./moonwell/README.md)
- [Morpho](./morpho/README.md)
- [Pendle](./pendle/README.md)
- [Resolv](./resolv/README.md) — _monitoring disabled_
- [RTokens - ETH+](./rtoken/README.md)
- [Silo](./silo/README.md) — _monitoring disabled_
- [Spark](./spark/README.md)
- [Strata](./strata/README.md)
- [Stargate](./stargate/README.md) — _monitoring disabled_
- [USD0 - Usual Money](./usd0/README.md)
- [USDAI](./usdai/README.md)
- [USTB - Superstate](./ustb/README.md)
- [Yearn](./yearn/README.md)

## Cross-Protocol Monitoring

- [Timelock Alerts](./timelock/README.md) — monitors OpenZeppelin `TimelockController` contracts for `CallScheduled` events across multiple protocols and sends Telegram alerts to protocol-specific channels.
- [Safe Multisigs](./safe/main.py) — monitors Safe multisig wallets for queued transactions across multiple protocols.

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
uv run aave/main.py
```

## Code Style

Format and lint code with ruff:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run pytest tests/
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full style guidelines, project conventions, and instructions on adding new protocols.

## Details

For more details about this repository, check out AI generated docs using DeepWiki at [https://deepwiki.com/yearn/monitoring-scripts-py](https://deepwiki.com/yearn/monitoring-scripts-py).
