"""
Morpho markets monitoring script.

This module checks Morpho markets for:
1. Bad debt
2. High allocation levels
3. Low liquidity
"""

from typing import Any, Dict, List

import requests

from utils.chains import Chain
from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_telegram_message

# Configuration constants
API_URL = "https://api.morpho.org/graphql"
MORPHO_URL = "https://app.morpho.org"
PROTOCOL = "morpho"
logger = get_logger(PROTOCOL)
BAD_DEBT_RATIO = 0.005  # 0.5% of total borrowed tvl
LIQUIDITY_THRESHOLD = 0.01  # 1% of total assets
LIQUIDITY_THRESHOLD_YV_COLLATERAL = 0.06  # 6% of total assets

# Map vaults by chain
VAULTS_BY_CHAIN = {
    Chain.MAINNET: [
        # name, address, risk level
        ["Steakhouse USDC", "0xBEEF01735c132Ada46AA9aA4c54623cAA92A64CB", 1],
        ["Steakhouse USDT", "0xbEef047a543E45807105E51A8BBEFCc5950fcfBa", 1],
        ["Gauntlet WETH Prime", "0x2371e134e3455e0593363cBF89d3b6cf53740618", 1],
        ["Gauntlet USDC Prime", "0xdd0f28e19C1780eb6396170735D45153D261490d", 1],
        ["Gauntlet USDT Prime", "0x8CB3649114051cA5119141a34C200D65dc0Faa73", 1],
        ["Gauntlet DAI Core", "0x500331c9fF24D9d11aee6B07734Aa72343EA74a5", 2],
        ["Yearn OG WETH", "0xE89371eAaAC6D46d4C3ED23453241987916224FC", 2],
        ["Yearn OG USDC", "0xF9bdDd4A9b3A45f980e11fDDE96e16364dDBEc49", 3],
        # these vaults are not used by yVaults
        # ["Gauntlet WBTC Core", "0x443df5eEE3196e9b2Dd77CaBd3eA76C3dee8f9b2", 3],
        # ["Gauntlet WETH Core", "0x4881Ef0BF6d2365D3dd6499ccd7532bcdBCE0658", 3],
        # ["Gauntlet USDC Core", "0x8eB67A509616cd6A7c1B3c8C21D48FF57df3d458", 4],
        # ["MEV Capital USDC", "0xd63070114470f685b75B74D60EEc7c1113d33a3D", 4],
        # Vault Bridge for Katana Chain
        ["Vault Bridge USDC", "0xBEefb9f61CC44895d8AEc381373555a64191A9c4", 1],
        ["Vault Bridge USDT", "0xc54b4E08C1Dcc199fdd35c6b5Ab589ffD3428a8d", 1],
        ["Vault Bridge WETH", "0x31A5684983EeE865d943A696AAC155363bA024f9", 1],
        ["Vault Bridge WBTC", "0x812B2C6Ab3f4471c0E43D4BB61098a9211017427", 2],
        ["Sentora PYUSD", "0x19b3cD7032B8C062E8d44EaCad661a0970DD8c55", 2],
        ["Sentora RLUSD", "0x71cb2F8038B2C5D65ddc740B2F3268890CD2A89C", 2],
    ],
    Chain.BASE: [
        ["GauntletUSDC Prime", "0xeE8F4eC5672F09119b96Ab6fB59C27E1b7e44b61", 1],
        ["Gauntlet USDT Prime", "0x585867B44914942578149766B305496374E33682", 1],
        ["SteakhousePrime USDC", "0xBEEFE94c8aD530842bfE7d8B397938fFc1cb83b2", 1],
        ["Moonwell Flagship USDC", "0xc1256Ae5FF1cf2719D4937adb3bbCCab2E00A2Ca", 1],
        ["Moonwell Flagship ETH", "0xa0E430870c4604CcfC7B38Ca7845B1FF653D0ff1", 2],
        ["Moonwell Flagship EURC", "0xf24608E0CCb972b0b0f4A6446a0BBf58c701a026", 2],
        ["Gauntlet WETH Core", "0x6b13c060F13Af1fdB319F52315BbbF3fb1D88844", 2],
        ["Seamless/Gauntlet WETH", "0x27D8c7273fd3fcC6956a0B370cE5Fd4A7fc65c18", 2],
        ["Seamless/Gauntlet USDC", "0x616a4E1db48e22028f6bbf20444Cd3b8e3273738", 2],
        ["Seamless/Gauntlet cbBTC", "0x5a47C803488FE2BB0A0EAaf346b420e4dF22F3C7", 2],
        ["Yearn OG WETH", "0x1D795E29044A62Da42D927c4b179269139A28A6B", 2],
        # ["Yearn OG USDC", "0xef417a2512C5a41f69AE4e021648b69a7CdE5D03", 2],
        [
            "Moonwell Frontier cbBTC",
            "0x543257eF2161176D7C8cD90BA65C2d4CaEF5a796",
            3,
        ],
    ],
    Chain.KATANA: [
        ["Yearn OG WETH", "0xFaDe0C546f44e33C134c4036207B314AC643dc2E", 1],
        ["Yearn OG USDC", "0xCE2b8e464Fc7b5E58710C24b7e5EBFB6027f29D7", 1],
        ["Yearn OG USDT", "0x8ED68f91AfbE5871dCE31ae007a936ebE8511d47", 1],
        ["Yearn OG WBTC", "0xe107cCdeb8e20E499545C813f98Cc90619b29859", 1],
        ["Gauntlet USDC", "0xE4248e2105508FcBad3fe95691551d1AF14015f7", 2],
        ["SteakhouseHigh Yield USDC", "0x1445A01a57D7B7663CfD7B4EE0a8Ec03B379aabD", 3],
        ["Gauntlet USDT", "0x1ecDC3F2B5E90bfB55fF45a7476FF98A8957388E", 1],
        ["SteakhousePrime USDC", "0x61D4F9D3797BA4dA152238c53a6f93Fb665C3c1d", 1],
        ["Gauntlet WETH", "0xC5e7AB07030305fc925175b25B93b285d40dCdFf", 1],
        ["Gauntlet WBTC", "0xf243523996ADbb273F0B237B53f30017C4364bBC", 1],
        # ["SteakhousePrime AUSD", "0x82c4C641CCc38719ae1f0FBd16A64808d838fDfD", 1],
        # ["Gauntlet AUSD", "0x9540441C503D763094921dbE4f13268E6d1d3B56", 1],
    ],
}

# Morpho Vaults that are used by Yearn Strategies which are used as YV collateral in Morpho Markets
# Organized by asset address for easier grouping and management
VAULTS_WITH_YV_COLLATERAL_BY_ASSET = {
    # NOTE: Mainnet is disabled because there is no borrowing demand for yvUSDC as collateral
    # Chain.MAINNET: {
    #     # USDC vaults
    #     "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": [
    #         ["OEV USDC", "0x68Aea7b82Df6CcdF76235D46445Ed83f85F845A3"],
    #         ["SteakhousePrime USDC", "0xBEEF01735c132Ada46AA9aA4c54623cAA92A64CB"],
    #         ["Gauntlet USDC Prime", "0xdd0f28e19C1780eb6396170735D45153D261490d"],
    #     ],
    #     # WETH vaults
    #     "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": [
    #         ["Gauntlet WETH Prime", "0x2371e134e3455e0593363cBF89d3b6cf53740618"],
    #     ],
    # },
    Chain.KATANA: {
        # USDC vaults - using addresses from VAULTS_BY_CHAIN as source of truth
        "0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36": [  # USDC asset address on Katana
            ["Yearn OG USDC", "0xCE2b8e464Fc7b5E58710C24b7e5EBFB6027f29D7"],
            ["SteakhousePrime USDC", "0x61D4F9D3797BA4dA152238c53a6f93Fb665C3c1d"],
            ["SteakhouseHigh Yield USDC", "0x1445A01a57D7B7663CfD7B4EE0a8Ec03B379aabD"],
            ["Gauntlet USDC", "0xE4248e2105508FcBad3fe95691551d1AF14015f7"],
        ],
        # USDT vaults - using addresses from VAULTS_BY_CHAIN as source of truth
        "0x2DCa96907fde857dd3D816880A0df407eeB2D2F2": [  # USDT asset address on Katana
            [
                "Yearn OG USDT",
                "0x8ED68f91AfbE5871dCE31ae007a936ebE8511d47",
            ],  # Corrected from VAULTS_BY_CHAIN
            ["Gauntlet USDT", "0x1ecDC3F2B5E90bfB55fF45a7476FF98A8957388E"],
        ],
        # WETH vaults - using addresses from VAULTS_BY_CHAIN as source of truth
        "0xEE7D8BCFb72bC1880D0Cf19822eB0A2e6577aB62": [  # WETH asset address on Katana
            [
                "Gauntlet WETH",
                "0xC5e7AB07030305fc925175b25B93b285d40dCdFf",
            ],  # Corrected from VAULTS_BY_CHAIN
            ["Yearn OG WETH", "0xFaDe0C546f44e33C134c4036207B314AC643dc2E"],
        ],
        # WBTC vaults - using addresses from VAULTS_BY_CHAIN as source of truth
        "0x0913DA6Da4b42f538B445599b46Bb4622342Cf52": [  # WBTC asset address on Katana
            ["Yearn OG WBTC", "0xe107cCdeb8e20E499545C813f98Cc90619b29859"],
            ["Gauntlet WBTC", "0xf243523996ADbb273F0B237B53f30017C4364bBC"],
        ],
        # NOTE: skip AUSD vaults because it is used in low amounts as collateral
        # AUSD vaults - using addresses from VAULTS_BY_CHAIN as source of truth
        # "0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a": [  # AUSD asset address on Katana
        #     ["SteakhousePrime AUSD", "0x82c4C641CCc38719ae1f0FBd16A64808d838fDfD"],
        #     ["Gauntlet AUSD", "0x9540441C503D763094921dbE4f13268E6d1d3B56"],
        # ],
    },
}


MARKETS_RISK_1 = {
    Chain.MAINNET: [
        "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",  # WBTC/USDC -> lltv 86%, oracle: chainlink WBTC/BTC, chainlink BTC/USD and chainlink USDC/USD
        "0xb323495f7e4148be5643a4ea4a8221eef163e4bccfdedc2a6f4696baacbc86cc",  # wstETH/USDC -> lltv 86%, oracle: compound oracle wstETH/ETH, chainlink ETH/USD and chainlink USDC/USD
        "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd",  # WETH/USDC -> lltv 86%, oracle: MorphoChainlinkOracleV2, Chainlink ETH/USD; no quote feed (USDC = $1).
        "0x64d65c9a2d91c36d56fbc42d69e979335320169b3df63bf92789e2c8883fcc64",  # cbBTC/USDC -> lltv 86%, oracle: chainlink BTC/USD and chainlink USDC/USD
        "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e",  # wstETH/WETH -> lltv 96.5%, oracle: lido exchange rate
        "0xc54d7acf14de29e0e5527cabd7a576506870346a78a11a6762e2cca66322ec41",  # wstETH/WETH -> lltv 94.5%, oracle: compound oracle wstETH/ETH
        "0xd0e50cdac92fe2172043f5e0c36532c6369d24947e40968f34a5e8819ca9ec5d",  # wstETH/WETH -> lltv 94.5%, oracle: lido exchange rate
        "0x138eec0e4a1937eb92ebc70043ed539661dd7ed5a89fb92a720b341650288a40",  # WBTC/WETH -> lltv 91.5%, oracle: chainlink BTC/ETH
        "0x2cbfb38723a8d9a2ad1607015591a78cfe3a5949561b39bde42c242b22874ec0",  # cbBTC/WETH -> lltv 91.5%, oracle: chainlink BTC/USD and chainlink ETH/USD
        "0x1929f8139224cb7d5db8c270addc9ce366d37ad279e1135f73c0adce74b0f936",  # sDAI/WETH -> lltv 86%, oracle: chainlink DAI/ETH
        "0x46981f15ab56d2fdff819d9c2b9c33ed9ce8086e0cce70939175ac7e55377c7f",  # sDAI/USDC -> lltv 96.5%, oracle: sDAI vault
        "0xa921ef34e2fc7a27ccc50ae7e4b154e16c9799d3387076c421423ef52ac4df99",  # WBTC/USDT -> lltv 86%, oracle: chainlink WBTC/BTC, chainlink BTC/USD and chainlink USDT/USD
        "0x3274643db77a064abd3bc851de77556a4ad2e2f502f4f0c80845fa8f909ecf0b",  # sUSDS/USDT -> lltv 96.5%, oracle: chainlink USDT/USD, chainlink DAI/USD and sUSDS vault
        "0xe7e9694b754c4d4f7e21faf7223f6fa71abaeb10296a4c43a54a7977149687d2",  # wstETH/USDT -> lltv 86%, oracle: compound oracle wstETH/ETH, chainlink ETH/USD and chainlink USDT/USD
        "0x1ca7ff6b26581fe3155f391f3960d32a033b5f7d537b1f1932b2021a6cf4f706",  # sDAI/USDT -> lltv 94.5%, oracle: sDAI vault, chainlink DAI/USD and chainlink USDT/USD
        "0xb1eac1c0f3ad13fb45b01beac8458c055c903b1bff8cb882346635996a774f77",  # sDAI/DAI -> lltv 98%, oracle: sDAI vault
        "0x37e7484d642d90f14451f1910ba4b7b8e4c3ccdd0ec28f8b2bdb35479e472ba7",  # weETH/WETH -> lltv 94.5%, oracle: origami weETH/ETH which calls WEETH.getRate(). Alike assets.
        "0x45671fb8d5dea1c4fbca0b8548ad742f6643300eeb8dbd34ad64a658b2b05bca",  # cbBTC/USDT -> lltv 86%, oracle: chainlink BTC/USD, hardcoded USDT=USD.
    ],
    Chain.BASE: [
        "0x7fc498ddcb7707d6f85f6dc81f61edb6dc8d7f1b47a83b55808904790564929a",  # cbETH/EURC -> lltv 86%, oracle: Chainlink cbETH/ETH and Chainlink ETH/USD and Chainlink EURC/USD.
        "0xa9b5142fa687a24c275faf731f13b52faa9873252bb4e1cb6077aa1f412edb0b",  # WETH/EURC -> lltv 86%, oracle: Chainlink ETH/USD and Chainlink EURC/USD.
        "0x67ebd84b2fb39e3bc5a13d97e4c07abe1ea617e40654826e9abce252e95f049e",  # cbBTC/EURC -> lltv 86%, oracle: Chainlink BTC/USD and Chainlink EURC/USD.
        "0xf7e40290f8ca1d5848b3c129502599aa0f0602eb5f5235218797a34242719561",  # wstETH/EURC -> lltv 86%, oracle: Chainlink wstETH-stETH Exchange Rate and Chainlink ETH/USD and Chainlink EURC/USD.
        "0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda",  # WETH/USDC -> lltv 86%, oracle: Chainlink ETH/USD and Chainlink USDC/USD.
        "0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836",  # cbBTC/USDC -> lltv 86%, oracle: Chainlink BTC/USD and Chainlink USDC/USD.
        "0x13c42741a359ac4a8aa8287d2be109dcf28344484f91185f9a79bd5a805a55ae",  # wstETH/USDC -> lltv 86%, oracle: Chainlink wstETH-stETH Exchange Rate and Chainlink ETH/USD and Chainlink USDC/USD.
        "0x1c21c59df9db44bf6f645d854ee710a8ca17b479451447e9f56758aee10a2fad",  # cbETH/USDC -> lltv 86%, oracle: Chainlink cbETH/ETH and Chainlink ETH/USD and Chainlink USDC/USD.
        "0xdb0bc9f10a174f29a345c5f30a719933f71ccea7a2a75a632a281929bba1b535",  # rETH/USDC -> lltv 86%, oracle: Chainlink rETH/ETH and Chainlink ETH/USD and Chainlink USDC/USD.
        "0x3a4048c64ba1b375330d376b1ce40e4047d03b47ab4d48af484edec9fec801ba",  # wstETH/WETH -> lltv 94.5%, oracle: Chainlink wstETH-stETH Exchange Rate
        "0x84662b4f95b85d6b082b68d32cf71bb565b3f22f216a65509cc2ede7dccdfe8c",  # cbETH/WETH -> lltv 94.5%, oracle: Chainlink cbETH-ETH Exchange Rate
        "0x5dffffc7d75dc5abfa8dbe6fad9cbdadf6680cbe1428bafe661497520c84a94c",  # cbBTC/WETH -> lltv 91.5%, oracle: Chainlink BTC/USD and Chainlink ETH/USD
        "0xa7813c754ddd6a24e1a1a29ff3ea877803ac63d09efc2f121b1cf3f0bf3af2f6",  # WETH/cbBTC -> lltv 91.5%, oracle: Chainlink ETH/USD and Chainlink BTC/USD
        "0x78d11c03944e0dc298398f0545dc8195ad201a18b0388cb8058b1bcb89440971",  # weWETH/WETH -> lltv 91.5%, oracle: Chainlink weETH/ETH exchange rate
        "0x3b3769cfca57be2eaed03fcc5299c25691b77781a1e124e7a8d520eb9a7eabb5",  # USDC/WETH -> lltv 86.5%, oracle: Chainlink USDC/USD and Chainlink ETH/USD
    ],
    Chain.KATANA: [
        "0xcd2dc555dced7422a3144a4126286675449019366f83e9717be7c2deb3daae3e",  # vbWBTC/vbUSDC -> lltv 86%, oracle: Chainlink WBTC/BTC, Chainlink BTC/USD and Chainlink USDC/USD
        "0x2fb14719030835b8e0a39a1461b384ad6a9c8392550197a7c857cf9fcbd6c534",  # vbETH/vbUSDC -> lltv 86%, oracle: Chainlink ETH/USD and Chainlink USDC/USD
        "0x60b54e17d55b765955a20908ed5143192a48df7fd3833f7f7fe86504bf6c4c1a",  # LBTC/vbBTC -> lltv 91.5%,  oracle: RedStone Price Feed for LBTC_FUNDAMENTAL -> Katana WBTC vault bridge accepts LBTC markets as collateral, no additional risk when using LBTC on Katana chain
        "0xd3b3c992070b5a6271b11acde46cdad575e4187e499782e084d73e523153f1ed",  # wstETH/vbUSDC -> lltv 86%, oracle: Chainlink wsteth/ETH, Chainlink ETH/USD and Chainlink USDC/USD
        "0x4b7a328d4c03ea974acac4a4c5f092870afe707df88aa4c5d834f93d96894050",  # vbETH/vbUSDC -> lltv 86%, oracle: Api3 ETH/USD and Api3 USDC/USD
        "0x499a1b2827cff06de432a00b5e8c4509d4c2a7eafc638c0df6a09a8fa1c8d649",  # vbWBTC/vbUSDC -> lltv 86%, oracle: Api3 BTC/USD and Api3 USDC/USD
        "0x4bc9c84a5271f5196357c0ed18af783614851f23ac11652e78b9934e34baa5d1",  # vbETH/vbUSDT -> lltv 86%, oracle: Api3 ETH/USD and Api3 USDT/USD
        "0x9c95ce191559ba7652c7a2d74568590824c1166a2994fcef696b413c18efe7ee",  # vbWBTC/vbUSDT -> lltv 86%, oracle: Api3 BTC/USD and Api3 USDT/USD
        "0x1e74d36ffbda65b8a45d72754b349cdd5ce807c5fa814f91ba8e3cd27881c34b",  # weETH/vbETH -> lltv 91.5%, oracle: Redstone weETH/ETH fundamental price
        "0x22f9f76056c10ee3496dea6fefeaf2f98198ef597eda6f480c148c6d3aaa70db",  # wstETH/vbETH -> lltv 91.5%, oracle: Redstone wstETH/ETH fundamental price
        "0xc149387c455f7abf7a1f430ccc6639df55fcd366a2f5b055f611289ed1b8a956",  # LBTC/vbUSDT -> lltv 86%, oracle: Redstone LBTC/BTC fundamental price and Redstone BTC/USD
        "0xa0cd6b9d1fcc6baded4f7f8f93697dbe7f24f6e1fc22602a625c7a80b8e8e6ef",  # LBTC/vbUSDC -> lltv 86%, oracle: Chainlink LBTC/USD and Chainlink USDC/USD
        "0xcdaf57d98c2f75bffb8f0d3f7aa79bbacda4a479c47e316aab14af1ca6d85ffc",  # yvUSDT/vbUSDC -> lltv 86%, oracle: yvUSDT vault rate. Chainlink USDT/USD and Chainlink USDC/USD
        "0x6691cdcadd5d23ac68d2c1cf54dc97ab8242d2a888230de411094480252c2ed3",  # yvUSDC/vbUSDT -> lltv 86%, oracle: yvUSDC vault rate. Chainlink USDC/USD and Chainlink USDT/USD
        "0xd4ab732112fa9087c9c3c3566cd25bc78ee7be4f1b8bdfe20d6328debb818656",  # vbWBTC/vbUSDT -> lltv 86%, oracle: Chainlink WBTC/USD
        "0x9e03fc0dc3110daf28bc6bd23b32cb20b150a6da151856ead9540d491069db1c",  # vbETH/vbUSDT -> lltv 86%, oracle: Chainlink ETH/USD
        "0x08f67ef41398456dbc5ff72d43c8b6f7917abfd01498a9fc6c89dabe6eb78b8c",  # yvvbETH/USDC -> lltv 77%, oracle: yearn vault exchange rate. Chainlink ETH/USD and Chainlink USDC/USD.
        "0x3a22063bd258f3f75e3135cac4ec53435dfa5b47b3d5173bb8fd5278e6c1b305",  # yvvbWBTC/USDC -> lltv 77%, oracle: yearn vault exchange rate. Chainlink WBTC/BTC, Chainlink BTC/USD and Chainlink USDC/USD.
        "0xcfac40b9f06194a33d9526f73642f6849b908c2b6d8669ad9d2d4a3e7dcb017a",  # yvAUSD/USDC -> lltv 86%, oracle: yearn vault exchange rate. Chainlink AUSD/USD and Chainlink USDC/USD.
        "0xbeb2f6ad6de1a9eead3302ad57a0180f67d127a22e53fa29bc724147b96cb20d",  # WBTC/AUSD -> lltv 86%, oracle: Chainlink WBTC/BTC, Chainlink BTC/USD and Chainlink AUSD/USD.
        "0x02a77b251cb27b04b5ddab89c852bdc77ee85d359c082170389001d71571a967",  # vbWETH/AUSD -> lltv 86%, oracle: Chainlink ETH/USD and Chainlink AUSD/USD.
        "0x0c909f9c866c4250cb2f15ef916b1eed5b1022b34ccd7ca947810011f5758c4f",  # BTCK/AUSD -> lltv 86%, oracle: RedStone Price Feed for BTC. USD=AUSD.
        "0xa6ce59291d90ae348b2fa956cc66f31df605a3304a9325e494c94e2cf5b0485a",  # weETH/vbUSDT -> lltv 77%, oracle: RedStone weETH/ETH fundamental price, Chainlink ETH/USD and Chainlink USDT/USD.
        "0x76e311d4b0e2e6ae88ad9bab18063452a6d39837d7104c430ff62457b91cb2cb",  # weETH/vbUSDC -> lltv 77%, oracle: RedStone weETH/ETH fundamental price, Chainlink ETH/USD and Chainlink USDC/USD.
        "0xbb4fb94ca819744df6a8f3932fffad47d31e8d76d3c48216878295c4cf588caf",  # weETH/vbUSDT -> lltv 86%, oracle: RedStone weETH/ETH fundamental price, Chainlink ETH/USD and Chainlink USDT/USD.
    ],
}

MARKETS_RISK_2 = {
    Chain.MAINNET: [
        "0x85c7f4374f3a403b36d54cc284983b2b02bbd8581ee0f3c36494447b87d9fcab",  # sUSDe/USDC -> lltv 91.5%, oracle: sUSDe vault
        "0x39d11026eae1c6ec02aa4c0910778664089cdd97c3fd23f68f7cd05e2e95af48",  # sUSDe / DAI -> lltv 86%, same value asset but using hardcoded oracle 1:1 USDe : DAI, sUSDe vault conversion for USDe
        "0x1247f1c237eceae0602eab1470a5061a6dd8f734ba88c7cdc5d6109fb0026b28",  # sUSDe / DAI -> lltv 91.5%, same value asset but using hardcoded oracle 1:1 USDe : DAI, sUSDe vault conversion for USDe
        "0xe475337d11be1db07f7c5a156e511f05d1844308e66e17d2ba5da0839d3b34d9",  # sUSDe / DAI -> lltv 41.5%, same value asset but using hardcoded oracle 1:1 USDe : USDC, sUSDe vault conversion for USDe
        "0x8e6aeb10c401de3279ac79b4b2ea15fc94b7d9cfc098d6c2a1ff7b2b26d9d02c",  # USDe / DAI -> lltv 91.5%, same value asset but using hardcoded oracle
        "0xc581c5f70bd1afa283eed57d1418c6432cbff1d862f94eaf58fdd4e46afbb67f",  # USDe / USDC -> lltv 86%, same value asset but using hardcoded oracle
        "0x5f8a138ba332398a9116910f4d5e5dcd9b207024c5290ce5bc87bc2dbd8e4a86",  # ETH+/WETH -> lltv 94.5%, oracle: ETH+ / USD exchange rate adapter and Chainlink: ETH/USD. ETH+ token has monitoring.
        "0x85ab69d50add7daa0934b5224889af0a882f2e3b4572d82c771dd0875f4eaa9b",  # pufETH/WETH -> lltv 94.5%, oracle: pufETH vault exchange rate. Alike assets.
        "0xbf02d6c6852fa0b8247d5514d0c91e6c1fbde9a168ac3fd2033028b5ee5ce6d0",  # LBTC/USDC -> lltv 86%, oracle: Redstone LBTC / BTC Redstone redemption price feed and Chainlink BTC/USD. More info on LBTC/BTC: https://docs.redstone.finance/docs/data/lombard/#how-redstone-delivers-lbtcbtc-fundamental-price
        "0xf6a056627a51e511ec7f48332421432ea6971fc148d8f3c451e14ea108026549",  # LBTC/WBTC -> lltv 94.5%, oracle: readstone exchange rate LBTC/BTC and chainlink WBTC/BTC
        "0xdb8938f97571aeab0deb0c34cf7e6278cff969538f49eebe6f4fc75a9a111293",  # ETH+/USDC -> lltv 86%, oracle: ETH+ / USD exchange rate adapter and Chainlink: USDC/USD. ETH+ token has monitoring.
        "0xe4cfbee9af4ad713b41bf79f009ca02b17c001a0c0e7bd2e6a89b1111b3d3f08",  # tBTC/USDC -> lltv 77%, oracle: tBTC/USD UMA oracle that captures OEV and USDC/USD UMA oracle.
        "0x550edc2e9fe71158ccfa7c478a31f4e60ef508d94ada3931dc2aee4f666f8f81",  # yvUSDC-1/USDC -> lltv 91.5%, oracle: yvUSDC-1 vault rate.
        "0x973e9dd45799efe8775417bcc420a3ab84a583587b2108985746e2fe201d0c83",  # YFI/USDC -> lltv 77%, oracle: Chainlink YFI/USD and Chainlink USDC/USD.
        "0xb8fef900b383db2dbbf4458c7f46acf5b140f26d603a6d1829963f241b82510e",  # OETH/USDC -> lltv 86%, oracle: Chainlink ETH/USD and Chainlink USDC/USD. OETH = ETH
        "0xeb17955ea422baeddbfb0b8d8c9086c5be7a9cfdefb292119a102e981a30062e",  # stcUSD/USDC -> lltv 91.5%, oracle: Ojo Yield Risk Engine stcUSD/cUSD Exchange Rate, RedStone Price Feed for cUSD_FUNDAMENTAL and Chainlink USDC/USD.
        "0x2fb3713487c7812e7309935b034f40228841666f6b048faf31fd2110ae674f20",  # PT-stcUSD-23JUL2026/USDC -> lltv 91.5%, oracle: OjoPTFeed oracle for stcUSD. RedStone Price Feed for cUSD_FUNDAMENTAL and Redstone USDC/USD v2.
        "0x702b7ec7628de2622e51e1bb34a7e6ad9e95f3a25a2ed361e4ce621f23f5e642",  # PT-cUSD-23JUL2026/USDC -> lltv 91.5%, oracle: OjoPTFeed oracle for cUSD. RedStone Price Feed for cUSD_FUNDAMENTAL and Redstone USDC/USD v2.
        "0x729badf297ee9f2f6b3f717b96fd355fc6ec00422284ce1968e76647b258cf44",  # syrupUSDC/USDC -> lltv 91.5%, oracle: syrupUSDC MaplePool vault rate. Oracle is using convertToAssets() to get the price but maple pool returns different amount, it should use convertToExitAssets() instead.
        "0x61765602144e91e5ac9f9e98b8584eae308f9951596fd7f5e0f59f21cd2bf664",  # weETH/USDC -> lltv 91.5%, oracle: redstone weETH/usdc exchange rate
        "0xb7843fe78e7e7fd3106a1b939645367967d1f986c2e45edb8932ad1896450877",  # XAUT/USDT -> lltv 77%, oracle: Chainlink XAUT/USD and Chainlink USDT/USD.
        "0xc3b37a18d5b15f8e5b78bcdc014ffb3f22933bde4e5f6a36dedf36db87e68585",  # WETH/RLUSD -> lltv 86%, oracle: Chainlink ETH/USD and Chainlink RLUSD/USD.
        "0xc0ae375fd761ff19b3f04de5534c0f1ec110f80e1c2ede27c42c1c43c3040394",  # syrupUSDC/RLUSD -> lltv 91.5%, oracle: syrupUSDC MaplePool ERC4626 to USDC, chainlink USDC/USD and Chainlink RLUSD/USD.
        "0xffd010618ed3cb39bb2c5de0e3e58d3d2ec9f52187a180f29723c31756a939bc",  # cbBTC/RLUSD -> lltv 86%, oracle: chainlink cbBTC/USD and Chainlink RLUSD/USD.
        "0xea4bfb18df0ee6bffb7b3f0270899a8adb92ab6b684709634c8276128813cfd4",  # weETH/RLUSD -> lltv 86%, oracle: chainlink weETH/ETH and chainlink ETH/USD and Chainlink RLUSD/USD.
        "0x88abdf8693e663144c3544b9442e9b04520016d6ebc57aa76424c00ab1683c9d",  # wstETH/RLUSD -> lltv 86%, oracle: Compound wstETH/ETH price feed, chainlink ETH/USD and Chainlink RLUSD/USD.
        "0x48a0da254e4df7b1046baa5ef11beb7916203886ce153a07a6d28c5d63cf8fad",  # sUSDe/RLUSD -> lltv 91.5%, oracle: sUSDe ERC4626 vault, chainlink USDe/USD and Chainlink RLUSD/USD.
        "0xf5c5df23559b0fb56560a7578ea17d81e245153ba64b8132df026c9358864d27",  # wstETH/PYUSD -> lltv 86%, oracle: Compound wstETH/ETH feed, chainlink ETH/USD and Chainlink PYUSD/USD.
        "0xa5beccdffd156dfe8c0871f143648c512f0a34f37c8a4ae2ff31ebfe944641d1",  # sUSDS/PYUSD -> lltv 94.5%, oracle: sUSDS ERC4626 vault, chainlink USDS/USD and Chainlink PYUSD/USD.
        "0xc9629945524f3fde56c7e8854a6c3d48e76b9d97236abbe73c750fcc7aeb8501",  # syrupUSDC/PYUSD -> lltv 91.5%, oracle: syrupUSDC MaplePool ERC4626 to USDC, chainlink USDC/USD and Chainlink PYUSD/USD.
        "0x6a7e36eb088bd501d73f7ab4c5b8671358559341a78ce521c9e499dc0bc642b9",  # LBTC/PYUSD -> lltv 86%, oracle: Redstone LBTC_FUNDAMENTAL, chainlink BTC/USD and Chainlink PYUSD/USD.
        "0x85d59152eeeab7ca024804895b358868d8dd1e134171be400d7792d5604a212c",  # weETH/PYUSD -> lltv 86%, oracle: chainlink weETH/ETH and chainlink ETH/USD and Chainlink PYUSD/USD.
        "0x90ef0c5a0dc7c4de4ad4585002d44e9d411d212d2f6258e94948beecf8b4c0d5",  # sUSDe/PYUSD -> lltv 91.5%, oracle: sUSDe ERC4626 vault, chainlink USDe/USD and Chainlink PYUSD/USD.
        "0xcb12dcbc7c6c4f20ca1537a3cc1a41ec27501f85a3e322a710d9a16a88a28c0e",  # PT-sUSDE-7MAY2026/PYUSD -> lltv 91.5%, oracle: Pendle oracle PT to USDe, chainlink USDe/USD and Chainlink PYUSD/USD.
        "0xd8a8e6667f58aa9229e8979bd619742b1660ee856c200a93e407dbccb7222323",  # cbBTC/PYUSD -> lltv 86%, oracle: chainlink cbBTC/USD and Chainlink PYUSD/USD.
    ],
    Chain.BASE: [
        "0x6aa81f51dfc955df598e18006deae56ce907ac02b0b5358705f1a28fcea23cc0",  # wstETH/WETH -> lltv 96.5%, oracle: Chainlink wstETH-stETH Exchange Rate
        "0x6600aae6c56d242fa6ba68bd527aff1a146e77813074413186828fd3f1cdca91",  # cbETH/WETH -> lltv 96.5%, oracle: cbETH-ETH logocbETH-ETH Exchange Rate
        "0x78d11c03944e0dc298398f0545dc8195ad201a18b0388cb8058b1bcb89440971",  # weETH/WETH -> lltv 91.5%, oracle: Chainlink weETH / eETH Exchange Rate
        "0xfd0895ba253889c243bf59bc4b96fd1e06d68631241383947b04d1c293a0cfea",  # weETH/WETH -> lltv 94.5%, oracle: Chainlink weETH / eETH Exchange Rate
        "0xdaa04f6819210b11fe4e3b65300c725c32e55755e3598671559b9ae3bac453d7",  # AERO/USDC -> lltv 62.5%, oracle: Chainlink AERO/USD and Chainlink USDC/USD
        "0x5189c48e1d333d250642a96b90dc926c53f897d8b8f9e8fea71a4b14e9053fde",  # steakSUSDS/USDC -> lltv: 96.5%, oracle: Maker's SSR oracle for sUSDS / USDS and dummy oracle for USDC returns 1. USDS = USDC
        "0xdba352d93a64b17c71104cbddc6aef85cd432322a1446b5b65163cbbc615cd0c",  # cbETH/USDC -> lltv 86.5%, oracle: Chainlink cbETH/ETH and Chainlink ETH/USD and Chainlink USDC/USD -> but low liquidity
        "0x7f90d72667171d72d10d62b5828d6a5ef7254b1e33718fe0c1f7dcf56dd1edc7",  # bsdETH/WETH -> lltv 91.5%, oracle: bsdETH total supply. bsdETH token has internal monitoring.
        "0x144bf18d6bf4c59602548a825034f73bf1d20177fc5f975fc69d5a5eba929b45",  # wsuperOETHb/WETH -> lltv 91.5%, oracle: Vault exchange rate for wsuperOETHb/superOETHb, superOETHb=ETH. wsuperOETHb token has internal monitoring.
        "0x67a66cbacb2fe48ec4326932d4528215ad11656a86135f2795f5b90e501eb538",  # superOETHb/USDC -> lltv 77%, oracle: Chainlink ETH/USD and Chainlink USDC/USD, 1ETH=1superOETHb
        "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109",  # cbXRP/USDC -> lltv 62.5%, oracle: Chainlink XRP/USD
        "0x9125d0fa03c3137166df68bcc72283477830de2a4a5536512374c573ad4583c3",  # cbLTC/USDC -> lltv 62.5%, oracle: Chainlink LTC/USD
        "0x30767836635facec1282e6ef4a5981406ed4e72727b3a63a3a72c74e8279a8d7",  # LBTC/cbBTC -> lltv 94.5%, oracle: RedStone Price Feed for LBTC_FUNDAMENTAL: https://app.redstone.finance/app/feeds/base/lbtc_fundamental/
    ],
    Chain.KATANA: [
        "0xfe6cb1b88d8830a884f2459962f4b96ae6e38416af086b8ae49f5d0f7f9fc0cd",  # POL/vbUSDC -> lltv 77%, oracle: Chainlink POL/USD and Chainlink USDC/USD
        "0xdf0f160d591f02931e44010763f892a51a480257a5ff21c41ebff874b0c7d258",  # BTCK/vbUSDT -> lltv 77%, oracle: Redstone BTC/USD
        "0x0e9d558490ed0cd523681a8c51d171fd5568b04311d0906fec47d668fb55f5d9",  # BTCK/vbUSDC -> lltv 77%, oracle: Redstone BTC/USD
        "0x913c787d438ca1dab5f5485c2d2d6e2aa2dfee47a5f02edd11331ad25b219dcf",  # KAT/vbUSDC -> lltv 62.5%, oracle: Chainlink KAT/USD and Chainlink USDC/USD.
        "0x24e50037bacb39950700c00851d5260e61975fb79f252ae2adbf7fdbd8db7290",  # KAT/vbUSDT -> lltv 62.5%, oracle: Chainlink KAT/USD and Chainlink USDT/USD.
        "0x071ed2047610c7b33e1540e49fcc0a6852cb783cca0dd7dc428f32fd791a020f",  # wstETH/AUSD -> lltv 86%, oracle: RedStone Price Feed for wstETH. USD=AUSD.
        "0xa7cd449cc319d65be3d0926d6b6f599a8c3434bd95ba3e91bbf1ee5e80e72b56",  # LBTC/vbUSDC -> lltv 86%, oracle: RedStone Price Feed for LBTC_FUNDAMENTAL and RedStone BTC/USD. USD=vbUSDC.
        "0x2c4f26c76b4de51d3c9260c15a796cd2a35efab17786d0aa78ca2e638b0f8ba8",  # yvvbUSDC/vbETH -> lltv 77%, oracle: yearn vault exchange rate. Chainlink ETH/USD and Chainlink USDC/USD.
        "0x61fcb4d6d1534eedeb0e0bea361745f727d73f14569d231c4a2b39232b6b7312",  # yvvbUSDT/vbWBTC -> lltv 77%, oracle: yearn vault exchange rate. Chainlink WBTC/USD and Chainlink USDT/USD.
        "0x09c2ecea0580698a91be0cff2bad3648b00744453c14a9bfb6be5ca7b9950908",  # PT-yvvbUSDC/vbUSDT -> lltv 86%, oracle: Pendle PT exchange rate(PT to asset) yvvbUSDC with yearn vault rate. Chainlink USDC/USD and Chainlink USDT/USD.
    ],
}

MARKETS_RISK_3 = {
    Chain.MAINNET: [
        "0x0cd36e6ecd9d846cffd921d011d2507bc4c2c421929cec65205b3cd72925367c",  # Curve TricryptoLLAMA LP / crvUSD -> collaterals: crvUSD, wstETH, tBTC.
        "0x198132864e7974fb451dfebeb098b3b7e7e65566667fb1cf1116db4fb2ad23f9",  # PT-LBTC-27MAR2025 / WBTC -> lltv 86%, oracle: Pendle PT exchange rate, readstone exchange rate LBTC/BTC and chainlink WBTC/BTC.
        "0x8a0384fe5b1a68ff217845752287f432029b20754fbce577b6a5f8a80030a825",  # PT-LBTC-26JUN2025 / WBTC -> lltv 91.5%, oracle: Pendle PT exchange rate, readstone exchange rate LBTC/BTC
        "0xba761af4134efb0855adfba638945f454f0a704af11fc93439e20c7c5ebab942",  # rsETH/WETH -> lltv 94.5%, oracle: origami rsETH/ETH which calls KELP_LRT_ORACLE.rsETHPrice(). Oracle address: https://etherscan.io/address/0x349A73444b1a310BAe67ef67973022020d70020d
        "0xa0534c78620867b7c8706e3b6df9e69a2bc67c783281b7a77e034ed75cee012e",  # ezETH/WETH -> lltv 94.5%, oracle: origami ezETH/ETH which calls renzoOracle()).calculateRedeemAmount(). It is hypothetical price, not the actual price.
        "0x8e7cc042d739a365c43d0a52d5f24160fa7ae9b7e7c9a479bd02a56041d4cf77",  # USR/USDC -> lltv 91.5%, oracle: USR/USD price aggregator which is checking reserves and defining max price as 1
        "0x97bb820669a19ba5fa6de964a466292edd67957849f9631eb8b830c382f58b7f",  # MKR/USDC -> lltv 77%, oracle: Chainlink MKR/USD and Chainlink USDC/USD.
        "0x718af3af39b183758849486340b69466e3e89b84b7884188323416621ee91cb7",  # UNI/USDC -> lltv 62%, oracle: Chainlink UNI/USD and Chainlink USDC/USD.
        "0x9c765f69d8a8e40d2174824bc5107d05d7f0d0f81181048c9403262aeb1ab457",  # LINK/USDC -> lltv 77%, oracle: Chainlink LINK/USD and Chainlink USDC/USD.
        "0xb7ad412532006bf876534ccae59900ddd9d1d1e394959065cb39b12b22f94ff5",  # agETH/WETH -> lltv 91.5%, oracle: rsETH/ETH exchange rateainlink ETH/USD. Alike assets.
        "0x1eda1b67414336cab3914316cb58339ddaef9e43f939af1fed162a989c98bc20",  # USD0++/USDC -> lltv 96.5%, oracle: Naked USD0++ price feed adapter
        "0xf9e56386e74f06af6099340525788eec624fd9c0fc0ad9a647702d3f75e3b6a9",  # clUSD/USDC -> lltv 96.5%, oracle: Chainlink clUSD/USD
        "0xd9e34b1eed46d123ac1b69b224de1881dbc88798bc7b70f504920f62f58f28cc",  # wstUSR/USDC -> lltv 91.5%, oracle: wstUSR vault rate. USR/USD price aggregator which is checking reserves and defining max price as 1
        "0x53ed197357128ed96070e20ba9f5af4250cda6c67dcac5246876beb483f51303",  # sDOLA/USDC -> lltv 91.5%, oracle: sDOLA vault rate. DOLA = USDC hardcoded oracle.
        "0x7a7018e22a8bb2d08112eae9391e09f065a8ae7ae502c1c23dc96c21411a6efd",  # EIGEN/USDC -> lltv 77%, oracle: Redstone EIGEN/USD. USD = USDC.
        "0xce68c7aa336675e42bbc8eaa8b5ecc7ebd816bf8625b5316330c6ac2dabc4cf2",  # SolvBTC/BTC -> lltv 94.5%, oracle: upgradeable MetaOracleDeviationTimelock with prime oracle morpho oracle with 1:1 hardcoded rate. same assets
        "0xbbf7ce1b40d32d3e3048f5cf27eeaa6de8cb27b80194690aab191a63381d8c99",  # siUSD/USDC -> lltv 91.5%, oracle: infinity accouting contract provides the price iUSD, vault rate siUSD to iUSD. usdc = 1 using dummy oracle.
        "0xaac3ffcdf8a75919657e789fa72ab742a7bbfdf5bb0b87e4bbeb3c29bbbbb05c",  # PT-siUSD-26MAR2026/USDC -> lltv 91.5%, oracle: ChainlinkOracleV2 — Pendle Chainlink-compatible PT feed, InfiniFi RT oracle (baseFeedTwo), dummy USDC feed (quote).
        "0xdf034d0351a4c0af947e1a37ecd5ccbce60d72eac90de6fcad48c74e2869d14c",  # PT-iUSD-25JUN2026/USDC -> lltv 91.5%, oracle: same stack as PT-siUSD row but Ojo PT Feed (Pendle-compatible) for PT leg; InfiniFi RT + dummy USDC.
        "0xc6ae8e71e11ef511acee3f6cc6ad2af67b862877d459e3789905f537c85db5e3",  # PT-sUSDE-25SEP2025/DAI -> lltv 91.5%, oracle: PendleSparkLinearDiscountOracle with linear discount oracle for sUSDE. No price oracle for DAI, USDe = DAI.
        "0x27b9a0a5bfee98a31eb51e3850250d103a9f8e41673c782defc66aa943af0e65",  # PT-srUSDe-2APR2026/USDC -> lltv 91.5%, oracle: Pendle PT exchange rate(PT to asset) srUSDe. USDC = 1 using dummy oracle.
        "0x15bb2a6af0c909eed19fb1f2ceeead34ecbdcba626de752c6b09389ee14eec32",  # kBTC/RLUSD -> lltv 86%, oracle: Chainlink BTC/USD and Chainlink RLUSD/USD.
    ],
    Chain.BASE: [
        "0x4944a1169bc07b441473b830308ffe5bb535c10a9f824e33988b60738120c48e",  # LBTC/cbBTC -> lltv 91.5%, oracle: Custom moonwell oracle. Base feed is fetched from upgradeable oracle which uses 2 oracles. Primary oracle is redstone oracle, if the price changes more than 2% than it uses fallback oracle chainlink oracle. Chainlink didn't have an exchange rate feed. Redstone was the only provider for the LBTC reserves.
        "0x214c2bf3c899c913efda9c4a49adff23f77bbc2dc525af7c05be7ec93f32d561",  # wrsETH/WETH -> lltv 94.5%, oracle: Chainlink wrsETH/ETH exchange rate
        "0x6a331b22b56c9c0ee32a1a7d6f852d2c682ea8b27a1b0f99a9c484a37a951eb7",  # weETH/USDC -> lltv 77%, oracle: Chainlink weETH / eETH Exchange Rate and Chainlink ETH/USD and Chainlink USDC/USD
        "0x52a2a376586d0775e3e80621facc464f6e96d81c8cb70fd461527dde195a079f",  # LBTC/USDC -> lltv 86%, oracle: RedStone Price Feed for LBTC/BTC  and Chainlink BTC/USD
        "0xfdfecf85a4dd90a7637ae2aaf28b35061166f0e62bfc714c565eed9f7e959783",  # cbXRP/USDC -> lltv 77%, oracle: Chainlink XRP/USD, Higher LLTV.
        "0xdc69cf2caae7b7d1783fb5a9576dc875888afad17ab3d1a3fc102f741441c165",  # rETH/WETH -> lltv 94.5%, oracle: Chainlink rETH/ETH, high risk oracle
        "0x0103cbcd14c690f68a91ec7c84607153311e9954c94ac6eac06c9462db3fabb6",  # rETH/EURC -> lltv 94.5%, oracle: Chainlink rETH/ETH, high risk oracle
        "0x73527ddd796e6d4f48387adaae36f6f3d49d606d7f2a15eb0c931416a58875d8",  # cbDOGE/USDC -> lltv 62.5%, oracle: Chainlink DOGE/USD
    ],
    Chain.KATANA: [
        "0xd8a93a4cd16f843c385391e208a9a9f2fd75aedfcca05e4810e5fbfcaa6baec6",  # wsrUSD/vbUSDC -> lltv 91.5%, oracle: API3 wsrUSD/rUSD Exchange Rate, rUSD = vbUSDC.
        "0xf7fc5cc82200ddf8f23188ddbd6727eda2c8bc41863e91fb767bbc6e4f71890e",  # siUSD/vbUSDC -> lltv 86%, oracle: MorphoChainlinkOracleV2, Chainlink SIUSD/USD; no quote feed (vbUSDC = USD).
    ],
    Chain.ARBITRUM: [
        "0x71c2954e00c8f72864600c9d1d1cd70fa15202c4294cd938d80add3be2eced26",  # sUSDai/USDC -> lltv 91.5%, oracle: Chronicle sUSDai/USD and Chainlink USDC/USD.
        "0x8147c63f3f6f5a0825c84bf2cb11443c72b609fa39cf9a362e3d4dc2c5ca76c4",  # PT-USDai-19FEB2026/USDC -> lltv 91.5% oracle: MetaOracleDeviationTimelock by Steakhouse with primary oracle set to Pendle PT where USDai=USDC and backup oracle set to
        "0x7717f1e04510390518811b3133ea47c298094ddd1d806ed8f8867d88c727bad7",  # PT-sUSDai-19FEB2026/USDC -> lltv 86%, oracle: Pendle PT exchange rate(PT to asset) sUSDai with ERC4626 vault rate sUSDai to USDai. Chainlink oracle USDC/USD.
    ],
}

MARKETS_RISK_4 = {
    Chain.MAINNET: [
        "0x3c83f77bde9541f8d3d82533b19bbc1f97eb2f1098bb991728acbfbede09cc5d",  # rETH/WETH -> lltv 94.5%, oracle: gravita rETH/ETH. Can change owner and aggregator.
        "0xe95187ba4e7668ab4434bbb17d1dfd7b87e878242eee3e73dac9fdb79a4d0d99",  # EIGEN/USDC -> lltv 77%, oracle: Redstone EIGEN/USD. USD = USDC.
        "0x444327b909aa41043cc4f20209eefb2fbb37f1c38ff9ca312374a4ecc3f0a871",  # SolvBTC/USDC -> lltv 86%, oracle: chainlink BTC/USD and chainlink USDC/USD
        "0x2287407f0f42ad5ad224f70e4d9da37f02770f79959df703d6cfee8afc548e0d",  # STONE/WETH -> lltv 94.5%, centralization risk
        "0xf78b7d3a62437f78097745a5e3117a50c56a02ec5f072cba8d988a129c6d4fb6",  # beraSTONE/WETH -> lltv 91.5%, centralization risk. chainlink ETH/USD
        "0xcacd4c39af872ddecd48b650557ff5bcc7d3338194c0f5b2038e0d4dec5dc022",  # rswETH/WETH -> lltv 94.5%, unknown asset
        "0x0eed5a89c7d397d02fd0b9b8e42811ca67e50ed5aeaa4f22e506516c716cfbbf",  # pufETH/WETH -> lltv 86%, oracle: pufETH vault exchange rate. Low liquidity market.
        "0x7e9c708876fa3816c46aeb08937b51aa0461c2af3865ecb306433db8a80b1d1b",  # pufETH/USDC -> lltv 77%, oracle: pufETH vault exchange rate. Low liquidity market.
        "0x514efda728a646dcafe4fdc9afe4ea214709e110ac1b2b78185ae00c1782cc82",  # swBTC/WBTC -> lltv 94.5%, same asset, check swBTC liquidity before moving up
        "0x20c488469064c8e2f892dab33e8c7a631260817f0db57f7425d4ef1d126efccb",  # Re7wstETH/WETH -> lltv 91.5%, unknown asset
        "0xd925961ad5df1d12f677ff14cf20bac37ea5ef3b325d64d5a9f4c0cc013a1d47",  # stUSD/USDC -> lltv 96.5%, oracle: stUSD vault rate. Angle transmuter handles USDA -> USDC conversion.
        "0xbf6687cb042a09451e66ebc11d7716c49fb8ccc75f484f7fab0eed6624bd5838",  # mMEV/USDC -> lltv 91.5%, oracle: Midas price oracle mMEV/USD. More info at: https://docs.midas.app/defi-integration/price-oracle
        "0x83b7ad16905809ea36482f4fbf6cfee9c9f316d128de9a5da1952607d5e4df5e",  # csUSDL/USDC -> lltv 96.5%, oracle: wUSDL / USDL vault rate.
        "0xe1b65304edd8ceaea9b629df4c3c926a37d1216e27900505c04f14b2ed279f33",  # RLP/USDC -> lltv 86%, oracle: RLP oracle where the price is set manually, but must be in bounds. Owner of the proxy is multisig.
        "0x8b1bc4d682b04a16309a8adf77b35de0c42063a7944016cfc37a79ccac0007b6",  # slvlUSD/USDC -> lltv 91.5%, oracle: slvlUSD vault rate. lvlUSD = USDC
        "0x95c28d447950ca6c8bbfd25fc05b80b1fd7a1cdd17a3610b4b3f1ffc8dc2e2ed",  # mHYPER/USDC -> lltv 86%, oracle: MHyperCustomAggregatorFeed
        "0xe83d72fa5b00dcd46d9e0e860d95aa540d5ec106da5833108a9f826f21f36f52",  # AA_FalconXUSDC/USDC -> lltv 77%, oracle: TranchesChainlinkOracle virtual price of current tranches.
    ],
    Chain.BASE: [
        "0xff0f2bd52ca786a4f8149f96622885e880222d8bed12bbbf5950296be8d03f89",  # USR/USDC -> lltv 91.5%, oracle: pyth USR/USD and pyth USDC/USD
    ],
    Chain.KATANA: [],
}

MARKETS_RISK_5 = {
    Chain.MAINNET: [
        "0xbfed072faee09b963949defcdb91094465c34c6c62d798b906274ef3563c9cac",  # srUSD/USDC -> lltv 91.5%, oracle: saving rate module price. rUSD(USD) is underlying asset. rUSD = USDC hardcoded oracle.
        "0x0f9563442d64ab3bd3bcb27058db0b0d4046a4c46f0acd811dacae9551d2b129",  # sdeUSD/USDC -> lltv 91.5%, oracle: sdeUSD vault rate. Redstone oracle deusd/usd price, 24hour heartbeat, deviation 0.2%: https://app.redstone.finance/app/feeds/ethereum-mainnet/deusd_fundamental/
    ],
    Chain.BASE: [],
    Chain.KATANA: [
        "0x16ded80178992b02f7c467c373cfc9f4eee7f0356df672f6a768ec92b2ffdeff",  # yUSD/vbUSDC -> lltv 86%, oracle: yUSD vault rate. yUSD = vbUSDC hardcoded oracle.
    ],
}

# Define base allocation tiers
ALLOCATION_TIERS = {
    1: 1.01,  # Risk tier 1 max allocation # TODO: think about lowering this to 0.80 but some vaults use 100% allocation to one market
    2: 0.30,  # Risk tier 2 max allocation
    3: 0.10,  # Risk tier 3 max allocation
    4: 0.05,  # Risk tier 4 max allocation
    5: 0.01,  # Unknown market max allocation
}

# Define max risk thresholds by risk level
MAX_RISK_THRESHOLDS = {
    1: 1.10,  # Risk tier 1 max total risk
    2: 2.20,  # Risk tier 2 max total risk
    3: 3.30,  # Risk tier 3 max total risk
    4: 4.40,  # Risk tier 4 max total risk
    5: 5.00,  # Risk tier 5 max total risk
}


def get_market_allocation_threshold(market_risk_level: int, vault_risk_level: int) -> float:
    """
    Get allocation threshold based on market and vault risk levels.
    For higher vault risk levels, thresholds shift up (become more permissive).
    For example, if vault risk level is 2, then market risk level 1 is 0.80, market risk level 2 is 0.30, etc.

    Args:
        market_risk_level: Risk level of the market (1-5)
        vault_risk_level: Risk level of the vault (1-5)

    Returns:
        Allocation threshold as a decimal (0-1)
    """
    # Shift market risk level down based on vault risk level
    adjusted_risk = max(1, market_risk_level - (vault_risk_level - 1))
    return ALLOCATION_TIERS[adjusted_risk]


def get_chain_name(chain: Chain) -> str:
    """Convert chain to name used in Morpho URLs."""
    if chain == Chain.MAINNET:
        return "ethereum"
    else:
        return chain.name.lower()


def get_market_url(market: Dict[str, Any]) -> str:
    """Generate URL for a Morpho market."""
    chain_id = market["collateralAsset"]["chain"]["id"]
    chain = Chain.from_chain_id(chain_id)
    return f"{MORPHO_URL}/{get_chain_name(chain)}/market/{market['uniqueKey']}"


def get_vault_url(vault_data: Dict[str, Any]) -> str:
    """Generate URL for a Morpho vault."""
    chain_id = vault_data["chain"]["id"]
    chain = Chain.from_chain_id(chain_id)
    return f"{MORPHO_URL}/{get_chain_name(chain)}/vault/{vault_data['address']}"


def bad_debt_alert(
    markets: List[Dict[str, Any]],
    vault_name: str,
    vault_url: str,
    chain: Chain,
    alerted_markets: set[str],
) -> None:
    """
    Send telegram message if bad debt is detected in any market.

    Args:
        markets: List of market data
        vault_name: Name of the vault (for alert message)
        vault_url: URL of the vault
        chain: Chain the vault is on
        alerted_markets: Set of market unique keys already alerted (prevents duplicates across vaults)
    """
    for market in markets:
        unique_key = market["uniqueKey"]
        if unique_key in alerted_markets:
            continue

        bad_debt = market["badDebt"]["usd"]
        borrowed_tvl = market["state"]["borrowAssetsUsd"]

        # Skip markets with no borrows
        if borrowed_tvl == 0:
            continue

        # Alert if bad debt ratio exceeds threshold
        if bad_debt / borrowed_tvl > BAD_DEBT_RATIO:
            alerted_markets.add(unique_key)
            market_url = get_market_url(market)
            market_name = f"{market['collateralAsset']['symbol']}/{market['loanAsset']['symbol']}"

            message = (
                f"🚨 Bad debt detected in [{vault_name}]({vault_url}) on {chain.name}\n"
                f"💹 Market: [{market_name}]({market_url})\n"
                f"💸 Bad debt: ${bad_debt:,.2f} ({(bad_debt / borrowed_tvl):.2%} of borrowed)\n"
            )

            send_telegram_message(message, PROTOCOL)


def check_allocation_and_risk(vault_data):
    """
    Check per-market allocation and total vault risk level.
    Sends a consolidated alert if any markets exceed allocation thresholds.
    Sends a separate alert if total risk level exceeds the vault's maximum.
    """
    total_assets = vault_data.get("state", {}).get("totalAssetsUsd", 0) or 0
    if total_assets == 0:
        return

    vault_name = vault_data["name"]
    vault_url = get_vault_url(vault_data)
    chain = Chain.from_chain_id(vault_data["chain"]["id"])
    # Find vault in VAULTS_BY_CHAIN to get risk level
    vault_address = vault_data["address"]
    risk_level = None
    for vault in VAULTS_BY_CHAIN[chain]:
        if vault[1].lower() == vault_address.lower():
            risk_level = vault[2]
            break

    if risk_level is None:
        # Throw error if vault not found in config
        raise ValueError(f"Vault {vault_address} not found in VAULTS_BY_CHAIN config")

    total_risk_level = 0.0
    allocation_violations: list[str] = []

    for allocation in vault_data["state"]["allocation"]:
        # market without collateral asset is idle asset
        if not allocation["enabled"] or allocation["market"]["collateralAsset"] is None:
            continue

        market = allocation["market"]
        unique_key = market["uniqueKey"]
        market_supply = allocation.get("supplyAssetsUsd", 0) or 0
        if market_supply == 0:
            logger.info("Skipping market %s with no supply assets", unique_key)
            continue
        allocation_ratio = min(market_supply / total_assets, 1.0)  # prevent allocation ratio from exceeding 100%

        # Determine market risk level
        if unique_key in MARKETS_RISK_1[chain]:
            market_risk_level = 1
        elif unique_key in MARKETS_RISK_2[chain]:
            market_risk_level = 2
        elif unique_key in MARKETS_RISK_3[chain]:
            market_risk_level = 3
        elif unique_key in MARKETS_RISK_4[chain]:
            market_risk_level = 4
        else:
            market_risk_level = 5

        allocation_threshold = get_market_allocation_threshold(market_risk_level, risk_level)
        risk_multiplier = market_risk_level

        if allocation_ratio > allocation_threshold:
            market_url = get_market_url(market)
            market_name = f"{market['collateralAsset']['symbol']}/{market['loanAsset']['symbol']}"
            allocation_violations.append(
                f"- [{market_name}]({market_url}) (risk {market_risk_level}): "
                f"{allocation_ratio:.1%} (max: {allocation_threshold:.1%})"
            )

        # Calculate weighted risk score for each market allocation
        # risk_multiplier: market risk tier (1-5, higher = riskier)
        # allocation_ratio: percentage of vault's assets in this market
        # total_risk_level: sum of (risk_tier * allocation) across all markets
        total_risk_level += risk_multiplier * allocation_ratio

    # Send consolidated allocation alert if any markets exceed thresholds
    if allocation_violations:
        violations_text = "\n".join(allocation_violations)
        message = (
            f"🔺 High allocation in [{vault_name}]({vault_url}) (risk {risk_level}) on {chain.name}\n"
            f"{violations_text}\n"
        )
        send_telegram_message(message, PROTOCOL)

    # print total risk level and vault name
    logger.info("Total risk level: %s, vault: %s on %s", f"{total_risk_level:.2f}", vault_name, chain.name)
    # round total_risk_level to 2 decimal places
    total_risk_level = round(total_risk_level, 2)
    if total_risk_level > MAX_RISK_THRESHOLDS[risk_level]:
        message = (
            f"⚠️ High risk level in [{vault_name}]({vault_url}) (risk {risk_level}) on {chain.name}\n"
            f"🔢 Risk level: {total_risk_level:.2f} (max: {MAX_RISK_THRESHOLDS[risk_level]:.2f})\n"
            f"🔢 Total assets: ${total_assets:,.2f}\n"
        )
        send_telegram_message(message, PROTOCOL)


def is_yv_collateral_vault(vault_address: str, chain: Chain) -> bool:
    """
    Check if a vault is used as YV collateral by looking in the asset-based config.

    Args:
        vault_address: Address of the vault to check
        chain: Chain the vault is on

    Returns:
        True if vault is used as YV collateral
    """
    if chain not in VAULTS_WITH_YV_COLLATERAL_BY_ASSET:
        return False

    # Check all asset groups for this chain
    for asset_address, vaults in VAULTS_WITH_YV_COLLATERAL_BY_ASSET[chain].items():
        for vault_name, vault_addr in vaults:
            if vault_addr.lower() == vault_address.lower():
                return True

    return False


def get_yv_collateral_vaults_by_asset(chain: Chain) -> Dict[str, List[str]]:
    """
    Get YV collateral vaults organized by asset address.

    Args:
        chain: Chain to get vaults for

    Returns:
        Dictionary with asset address as key and list of vault addresses as value
    """
    result = {}

    if chain not in VAULTS_WITH_YV_COLLATERAL_BY_ASSET:
        return result

    for asset_address, vaults in VAULTS_WITH_YV_COLLATERAL_BY_ASSET[chain].items():
        vault_addresses = [vault[1] for vault in vaults]  # Extract addresses from [name, address] pairs
        result[asset_address.lower()] = vault_addresses

    return result


def group_vaults_by_chain(vaults_data: List[Dict[str, Any]]) -> Dict[Chain, List[Dict[str, Any]]]:
    """Group vaults by their chain."""
    vaults_by_chain = {}
    for vault_data in vaults_data:
        chain = Chain.from_chain_id(vault_data["chain"]["id"])
        if chain not in vaults_by_chain:
            vaults_by_chain[chain] = []
        vaults_by_chain[chain].append(vault_data)
    return vaults_by_chain


def find_yv_vaults_for_asset(
    chain_vaults: List[Dict[str, Any]],
    asset_address: str,
    yv_vault_addresses: List[str],
) -> List[Dict[str, Any]]:
    """Find all YV collateral vaults for a specific asset."""
    asset_yv_vaults = []

    for vault_data in chain_vaults:
        vault_address = vault_data["address"]
        vault_asset_address = vault_data.get("asset", {}).get("address", "").lower()

        # Check if this vault is for the current asset and is YV collateral
        if vault_asset_address == asset_address and vault_address in yv_vault_addresses:
            asset_yv_vaults.append(vault_data)

    return asset_yv_vaults


def calculate_combined_metrics(asset_yv_vaults: List[Dict[str, Any]]) -> tuple[float, float, List[str]]:
    """Calculate combined total assets, liquidity, and vault names for a group of vaults."""
    combined_total_assets = 0
    combined_liquidity = 0
    vault_names = []

    for vault in asset_yv_vaults:
        total_assets = vault["state"]["totalAssetsUsd"] or 0
        liquidity = vault["liquidity"]["usd"] or 0

        # Only include vaults with meaningful assets (>= 10k)
        if total_assets >= 10_000:
            combined_total_assets += total_assets
            combined_liquidity += liquidity
            vault_names.append(vault["name"])

    return combined_total_assets, combined_liquidity, vault_names


def check_combined_liquidity_threshold(
    asset_symbol: str,
    chain: Chain,
    combined_total_assets: float,
    combined_liquidity: float,
    vault_names: List[str],
    vault_count: int,
) -> None:
    """Check if combined liquidity meets threshold and send alert if needed."""
    if combined_total_assets < 10_000:
        logger.info(
            "Skipping %s combined liquidity check: total assets $%s below threshold",
            asset_symbol,
            f"{combined_total_assets:,.2f}",
        )
        return

    combined_liquidity_ratio = combined_liquidity / combined_total_assets

    logger.info(
        "Combined %s liquidity check: %s vaults on %s, $%s total assets, %s liquidity ratio",
        asset_symbol,
        vault_count,
        chain.name,
        f"{combined_total_assets:,.2f}",
        f"{combined_liquidity_ratio:.1%}",
    )

    if combined_liquidity_ratio < LIQUIDITY_THRESHOLD_YV_COLLATERAL:
        vault_list = ", ".join(vault_names)
        message = (
            f"⚠️ Low combined liquidity for {asset_symbol} YV collateral vaults on {chain.name}\n"
            f"🏦 Vaults: {vault_list}\n"
            f"💰 Liquidity: ${combined_liquidity:,.2f} ({combined_liquidity_ratio:.1%} of ${combined_total_assets:,.2f})\n"
            f"📊 Min threshold: {LIQUIDITY_THRESHOLD_YV_COLLATERAL:.1%}\n"
        )
        send_telegram_message(message, PROTOCOL)


def check_yv_collateral_liquidity_for_chain(chain: Chain, chain_vaults: List[Dict[str, Any]]) -> None:
    """Check combined liquidity for YV collateral vaults on a specific chain."""
    yv_vaults_by_asset = get_yv_collateral_vaults_by_asset(chain)

    # Check each asset group for combined liquidity
    for asset_address, yv_vault_addresses in yv_vaults_by_asset.items():
        # Find all vaults for this asset that are YV collateral
        asset_yv_vaults = find_yv_vaults_for_asset(chain_vaults, asset_address, yv_vault_addresses)

        if not asset_yv_vaults:
            continue

        # Get asset symbol from first vault
        asset_symbol = asset_yv_vaults[0].get("asset", {}).get("symbol", "UNKNOWN")

        # Calculate combined metrics for this asset
        (
            combined_total_assets,
            combined_liquidity,
            vault_names,
        ) = calculate_combined_metrics(asset_yv_vaults)

        # Check threshold and alert if needed
        check_combined_liquidity_threshold(
            asset_symbol,
            chain,
            combined_total_assets,
            combined_liquidity,
            vault_names,
            len(asset_yv_vaults),
        )


def check_individual_liquidity_for_chain(chain: Chain, chain_vaults: List[Dict[str, Any]]) -> None:
    """Check individual liquidity for non-YV collateral vaults on a specific chain."""
    for vault_data in chain_vaults:
        vault_address = vault_data["address"]
        if not is_yv_collateral_vault(vault_address, chain):
            check_low_liquidity(vault_data)


def check_low_liquidity_combined(vaults_data: List[Dict[str, Any]]) -> None:
    """
    Check liquidity for vaults, with special logic for VAULTS_WITH_YV_COLLATERAL_BY_ASSET.
    For YV collateral vaults, combine all vaults with the same asset and check if
    the combined liquidity passes the threshold.
    """
    # Group vaults by chain for processing
    vaults_by_chain = group_vaults_by_chain(vaults_data)

    # Process each chain separately
    for chain, chain_vaults in vaults_by_chain.items():
        # Check combined liquidity for YV collateral vaults
        check_yv_collateral_liquidity_for_chain(chain, chain_vaults)

        # Check individual liquidity for non-YV collateral vaults
        check_individual_liquidity_for_chain(chain, chain_vaults)


def check_low_liquidity(vault_data):
    """
    Send telegram message if low liquidity is detected.
    """
    vault_name = vault_data["name"]
    vault_url = get_vault_url(vault_data)
    total_assets = vault_data["state"]["totalAssetsUsd"]
    liquidity = vault_data["liquidity"]["usd"]
    chain = Chain.from_chain_id(vault_data["chain"]["id"])

    # Return early if total_assets is None or 0 or less than 10k
    if not total_assets or total_assets < 10_000:
        return

    # Default liquidity to 0 if it's None
    liquidity = liquidity or 0
    liquidity_ratio = liquidity / total_assets

    # standard liquidity check (YV collateral vaults are handled separately)
    if liquidity_ratio < LIQUIDITY_THRESHOLD:
        message = (
            f"⚠️ Low liquidity in [{vault_name}]({vault_url}) on {chain.name}\n"
            f"💰 Liquidity: ${liquidity:,.2f} ({liquidity_ratio:.1%} of ${total_assets:,.2f})\n"
            f"📊 Min threshold: {LIQUIDITY_THRESHOLD:.1%}\n"
        )
        send_telegram_message(message, PROTOCOL)


def main() -> None:
    """
    Check markets for low liquidity, high allocation and bad debt.
    Send telegram message if data cannot be fetched.
    """
    logger.info("Checking Morpho markets...")

    # Collect all vault addresses from all chains
    vault_addresses = []
    for chain, vaults in VAULTS_BY_CHAIN.items():
        vault_addresses.extend([vault[1] for vault in vaults])

    query = """
    query GetVaults($addresses: [String!]!) {
        vaults(where: { address_in: $addresses } ) {
            items {
                address
                name
                chain {
                  id
                }
                asset {
                    address
                    symbol
                    name
                }
                liquidity {
                  usd
                }
                state {
                    totalAssetsUsd
                    allocation {
                        enabled
                        supplyAssetsUsd
                        pendingSupplyCapUsd
                        pendingSupplyCapValidAt
                        market {
                            uniqueKey
                            loanAsset {
                                address
                                symbol
                            }
                            collateralAsset {
                                address
                                symbol
                                chain {
                                    id
                                }
                            }
                            state {
                                utilization
                                borrowAssetsUsd
                                supplyAssetsUsd
                            }
                            badDebt {
                                underlying
                                usd
                            }
                        }
                    }
                }
            }
        }
    }
    """

    json_data = {"query": query, "variables": {"addresses": vault_addresses}}

    try:
        response = request_with_retry("post", API_URL, json=json_data)
    except requests.RequestException as e:
        send_telegram_message(
            f"🚨 Problem with fetching data for Morpho markets: {e.response.status_code} 🚨",
            PROTOCOL,
            True,
            True,
        )
        return

    data = response.json()
    if "errors" in data:
        send_telegram_message(
            f"🚨 GraphQL error when fetching Morpho data. Response code: {response.status_code} 🚨",
            PROTOCOL,
            True,
            True,
        )
        return

    vaults_data = data.get("data", {}).get("vaults", {}).get("items", [])
    if len(vaults_data) == 0:
        send_telegram_message("🚨 No vaults data found 🚨", PROTOCOL)
        return

    # Check combined liquidity for all vaults (handles YV collateral grouping)
    check_low_liquidity_combined(vaults_data)

    alerted_markets: set[str] = set()

    for vault_data in vaults_data:
        # Check per-market allocation and total risk level
        check_allocation_and_risk(vault_data)

        # Check bad debt for each market in the vault
        vault_markets = []
        for allocation in vault_data["state"]["allocation"]:
            market_supply_usd = allocation.get("market", {}).get("state", {}).get("supplyAssetsUsd")
            if allocation["enabled"] and (market_supply_usd or 0) > 1e4:  # skip low value markets
                market = allocation["market"]
                if market["collateralAsset"] is not None:
                    # market without collateral asset is idle asset
                    vault_markets.append(market)

        vault_name = vault_data["name"]
        vault_url = get_vault_url(vault_data)
        chain = Chain.from_chain_id(vault_data["chain"]["id"])
        bad_debt_alert(vault_markets, vault_name, vault_url, chain, alerted_markets)


if __name__ == "__main__":
    main()
