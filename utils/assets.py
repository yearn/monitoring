"""
Shared asset risk tiers and allocation thresholds used across protocol monitors.

Each asset is assigned a risk tier (1-5) based on oracle quality, liquidity,
and overall risk profile. These tiers drive per-asset allocation limits and
total weighted risk scoring for vaults/markets.
"""

# Asset risk tiers: [symbol, tier]
# Tier 1 = lowest risk (blue chips, deep liquidity, Chainlink oracles)
# Tier 5 = highest risk (thin liquidity, exotic oracles, niche assets)
SUPPLY_ASSETS: list[list] = [
    # Risk Tier 1
    ["USDC", 1],
    ["USDT", 1],
    ["DAI", 1],
    ["USDS", 1],
    ["sUSDS", 1],
    ["WETH", 1],
    ["WBTC", 1],
    ["mETH", 1],  # Pyth oracle mETH/ETH and chainlink ETH/USD
    ["cbETH", 1],  # Rate cbETH/WETH and chainlink WETH/USD
    ["rETH", 1],  # Rate rETH/WETH and chainlink WETH/USD
    ["wstETH", 1],  # Rate wstETH/WETH and chainlink WETH/USD
    ["cbBTC", 1],  # Chainlink oracle cbBTC/USD
    [
        "weETH",
        1,
    ],  # Rate weeth/WETH and chainlink WETH/USD https://www.llamarisk.com/research/risk-collateral-risk-assessment-wrapped-etherfi-eth-weeth
    # Risk Tier 2
    [
        "ezETH",
        2,  # Risk 1 is fine for ezETH/WETH market
    ],  # Rate ezETH/WETH and chainlink WETH/USD https://www.llamarisk.com/research/risk-collateral-risk-assessment-renzo-restaked-eth-ezeth
    [
        "ETHx",
        2,
    ],  # Rate ETHx/WETH and chainlink WETH/USD https://www.llamarisk.com/research/risk-addendum-to-collateral-risk-assessment-stader-ethx
    ["USDe", 2],  # Chainlink USDe/USD
    ["sUSDe", 2],  # Rate sUSDe/USDe and chainlink USDe/USD
    ["PT-USDe-27MAR2025", 2],  # Pendle PT to USDe and chainlink USDe/USD
    ["PT-sUSDE-27MAR2025", 2],  # Pendle PT to USDe and chainlink USDe/USD
    ["PT-sUSDE-29MAY2025", 2],  # Pendle PT to USDe and chainlink USDe/USD
    ["PYUSD", 2],  # Pyth oracle PYUSD/USD and high liquidity (10M)
    ["tBTC", 2],  # Uses chainlink oracle tBTC/USD
    ["sFRAX", 2],
    ["rsETH", 2],  # Rate rsETH/WETH and chainlink WETH/USD
    ["LBTC", 2],  # Uses chainlink oracle LBTC/BTC and BTC/USD
    ["wOETH", 2],  # Origin ETH
    ["pufETH", 2],  # pufETH
    ["WPOL", 2],
    ["MaticX", 2],  # MaticX
    ["stMATIC", 2],  # stMATIC
    # Risk Tier 3
    ["USD0", 3],  # pyth oracle USD0/USD and medium liquidity (6M)
    ["osETH", 3],  # StakeWise osETH
    ["tETH", 3],  # Treehouse tETH
    ["SKY", 3],
    ["UNI", 3],
    ["LINK", 3],
    ["syrupUSDC", 3],
    ["COMP", 3],
    ["XAUt", 3],  # Tether Gold
    ["rswETH", 3],  # Swell rswETH
    # Risk Tier 4
    [
        "wUSDM",
        4,
    ],  # Rate wusdm/WETH and fixed rate USDM/USD https://www.llamarisk.com/research/archive-llamarisk-asset-risk-assessment-mountain-protocol-usdm - https://mountainprotocol.com/usdm
    ["eBTC", 4],  # Rate eBTC/BTC and chainlink BTC/USD
    [
        "SolvBTC",
        4,
    ],  # Uses chainlink oracle solvBTC/BTC and BTC/USD https://solv.finance/
    [
        "USDtb",
        4,
    ],  # onboarded by aave: https://app.aave.com/governance/v3/proposal/?proposalId=305
    # Risk Tier 5
    [
        "USD0++",
        5,
    ],  # pyth oracle USD0++/USD but low liquidity to get usd0, below 1M without slippage and euler market size is 4M
    ["FDUSD", 5],  # pyth oracle FDUSD/USD and low liquidity (100k)
    ["mTBILL", 5],  # Euler Vault uses fixed rate oracle. https://midas.app/
    ["wM", 5],  # Euler Vault uses fixed rate oracle. https://www.m0.org/
    ["mBASIS", 5],
    ["deUSD", 5],
    ["sdeUSD", 5],
]

# Convert SUPPLY_ASSETS list to dictionary for easier lookup
SUPPLY_ASSETS_DICT: dict[str, int] = {asset: risk_tier for asset, risk_tier in SUPPLY_ASSETS}

# Max allocation per risk tier (ratio of total collateral)
ALLOCATION_TIERS: dict[int, float] = {
    1: 1.01,  # Risk tier 1 max allocation
    2: 0.30,  # Risk tier 2 max allocation
    3: 0.10,  # Risk tier 3 max allocation
    4: 0.05,  # Risk tier 4 max allocation
    5: 0.01,  # Unknown market max allocation
}

# Max weighted risk score by vault risk level
MAX_RISK_THRESHOLDS: dict[int, float] = {
    1: 1.10,  # Risk tier 1 max total risk
    2: 2.20,  # Risk tier 2 max total risk
    3: 3.30,  # Risk tier 3 max total risk
    4: 4.40,  # Risk tier 4 max total risk
    5: 5.00,  # Risk tier 5 max total risk
}


def get_market_allocation_threshold(market_risk_level: int, vault_risk_level: int) -> float:
    """Get allocation threshold based on market and vault risk levels.

    For higher vault risk levels, thresholds shift up (become more permissive).
    For example, if vault risk level is 2, then market risk level 2 assets
    get the tier-1 threshold (1.01) instead of tier-2 (0.30).
    """
    adjusted_risk = max(1, market_risk_level - (vault_risk_level - 1))
    return ALLOCATION_TIERS[adjusted_risk]
