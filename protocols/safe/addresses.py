"""Static configuration for the Safe-multisig monitor.

Split out from safe/main.py to keep that file focused on logic.
"""

# Maps Safe API network names to short prefixes used in app.safe.global URLs.
safe_address_network_prefix = {
    "mainnet": "eth",
    "arbitrum-main": "arb1",
    "optimism-main": "oeth",
    "polygon-main": "matic",
    "optim-yearn": "oeth",
    "base-main": "base",
    "katana-main": "katana",
}

# Maps Safe API network names to their transaction-service base URL.
safe_apis = {
    "mainnet": "https://api.safe.global/tx-service/eth",
    "arbitrum-main": "https://api.safe.global/tx-service/arb1",
    "optimism-main": "https://api.safe.global/tx-service/oeth",
    "polygon-main": "https://api.safe.global/tx-service/pol",
    "base-main": "https://api.safe.global/tx-service/base",
    "katana-main": "https://api.safe.global/tx-service/katana",
    # "optim-yearn": "https://safe-transaction-optimism.safe.global",
}

PROXY_UPGRADE_SIGNATURES = [
    # Standard Proxy (OpenZeppelin, UUPS, Transparent)
    "3659cfe6",  # bytes4(keccak256("upgradeTo(address)"))
    "4f1ef286",  # upgradeToAndCall(address,bytes)
    "f2fde38b",  # changeProxyAdmin(address,address)
    # Diamond Proxy (EIP-2535)
    "1f931c1c",  # diamondCut((address,uint8,bytes4[])[],address,bytes)
]

# Watched non-yearn protocol multisigs. Format: [protocol, network, address, optional label].
ALL_SAFE_ADDRESSES = [
    [
        "LIDO",
        "mainnet",
        "0x73b047fe6337183A454c5217241D780a932777bD",
    ],  # https://docs.lido.fi/multisigs/emergency-brakes/#12-emergency-brakes-ethereum
    [
        "LIDO",
        "mainnet",
        "0x8772E3a2D86B9347A2688f9bc1808A6d8917760C",
    ],  # https://docs.lido.fi/multisigs/emergency-brakes/#11-gateseal-committee -> expires on 1 April 2025.
    ["PENDLE", "mainnet", "0x8119EC16F0573B7dAc7C0CB94EB504FB32456ee1"],
    ["PENDLE", "arbitrum-main", "0x7877AdFaDEd756f3248a0EBfe8Ac2E2eF87b75Ac"],
    ["EULER", "mainnet", "0xcAD001c30E96765aC90307669d578219D4fb1DCe"],
    [
        "AAVE",
        "mainnet",
        "0x2CFe3ec4d5a6811f4B8067F0DE7e47DfA938Aa30",
    ],  # aave Protocol Guardian Safe: https://app.aave.com/governance/v3/proposal/?proposalId=184
    ["AAVE", "polygon-main", "0xCb45E82419baeBCC9bA8b1e5c7858e48A3B26Ea6"],
    ["AAVE", "arbitrum-main", "0xCb45E82419baeBCC9bA8b1e5c7858e48A3B26Ea6"],
    [
        "AAVE",
        "mainnet",
        "0xCe52ab41C40575B072A18C9700091Ccbe4A06710",
    ],  # aave Governance Guardian Safe
    ["AAVE", "polygon-main", "0x1A0581dd5C7C3DA4Ba1CDa7e0BcA7286afc4973b"],
    ["AAVE", "arbitrum-main", "0x1A0581dd5C7C3DA4Ba1CDa7e0BcA7286afc4973b"],
    [
        "SPARK",
        "mainnet",
        "0x44efFc473e81632B12486866AA1678edbb7BEeC3",
        "SparkLend Freezer Multisig",
    ],
    [
        "MORPHO",
        "mainnet",
        "0x84258B3C495d8e9b10D0d4A7867392F149Da4274",
        "Morpho eUSDe predeposit vault owner",
    ],  # eUSDe predeposit vault owner, token used by DAI vault on morpho
    [
        "LRT",
        "mainnet",
        "0xb7cB7131FFc18f87eEc66991BECD18f2FF70d2af",
        "LBTC boring vault big boss",
    ],  # LBTC boring vault big boss
    [
        "LRT",
        "base-main",
        "0x92A19381444A001d62cE67BaFF066fA1111d7202",
        "Origin admin multisig. Markets used on Base",
    ],  # origin admin
    [
        "LRT",
        "mainnet",
        "0x9F6e831c8F8939DC0C830C6e492e7cEf4f9C2F5f",
        "tBTC bridge owner multisig. aka, Council Multisig",
    ],  # tBTC bridge owner multisig (Council Multisig)
    [
        "USDAI",
        "arbitrum-main",
        "0xF223F8d92465CfC303B3395fA3A25bfaE02AED51",
        "USDai Admin Safe",
    ],
    [
        "USDAI",
        "arbitrum-main",
        "0x783B08aA21DE056717173f72E04Be0E91328A07b",
        "sUSDai Admin Safe",
    ],
    [
        "CAP MONEY",
        "mainnet",
        "0xb8FC49402dF3ee4f8587268FB89fda4d621a8793",
        "Cap Money Multisig",
    ],
    [
        "MAPLE",
        "mainnet",
        "0xd6d4Bcde6c816F17889f1Dd3000aF0261B03a196",
        "Maple DAO Multisig (syrupUSDC)",
    ],
    [
        "STRATA",
        "mainnet",
        "0xA27cA9292268ee0f0258B749f1D5740c9Bb68B50",
        "Strata Admin Multisig (3/4)",
    ],
    # [
    #     "INFINIFI",
    #     "mainnet",
    #     "0x80608f852D152024c0a2087b16939235fEc2400c",
    #     "Infinifi Team Multisig",
    # ],
]

# Yearn bots/EOAs that routinely propose txs on monitored multisigs.
YEARN_PROPOSER_BOTS: dict[str, str] = {
    "chad": "0x5e69fb460c9950f5ae90daffc4c4f32ecafacaa5",
    "strategist": "0xce434267f53926d4f6bbb12a5c2a3ef3873db254",
    "curation": "0x80a3887ba60f76acab48ee4aead0a71a0774a8b2",
}

# 5th field: key into YEARN_PROPOSER_BOTS.
YEARN_MULTISIGS: list[list[str]] = [
    ["YEARN_MS", "mainnet", "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52", "yChad (Yearn multisig/daddy)", "chad"],
    ["YEARN_MS", "base-main", "0xbfAABa9F56A39B814281D68d2Ad949e88D06b02E", "bChad Multisig", "chad"],
    ["YEARN_MS", "katana-main", "0xe6ad5A88f5da0F276C903d9Ac2647A937c917162", "kChad Multisig", "chad"],
    [
        "YEARN_MS",
        "mainnet",
        "0x16388463d60FFE0661Cf7F1f31a7D658aC790ff7",
        "Strategist Multisig (brain.ychad.eth)",
        "strategist",
    ],
    ["YEARN_MS", "base-main", "0x01fE3347316b2223961B20689C65eaeA71348e93", "Strategist Multisig (base)", "strategist"],
    [
        "YEARN_MS",
        "katana-main",
        "0xBe7c7efc1ef3245d37E3157F76A512108D6D7aE6",
        "Strategist Multisig (katana)",
        "strategist",
    ],
    ["YEARN_MS", "mainnet", "0xe5e2Baf96198c56380dDD5E992D7d1ADa0e989c0", "SAM Multisig (mainnet)", "curation"],
    ["YEARN_MS", "base-main", "0xFEaE2F855250c36A77b8C68dB07C4dD9711fE36F", "SAM Multisig (base)", "curation"],
    ["YEARN_MS", "katana-main", "0x518C21DC88D9780c0A1Be566433c571461A70149", "SAM Multisig (katana)", "curation"],
    ["YEARN_MS", "mainnet", "0x90D0f26025571295D18a6c041E47450B81886B51", "Curation Multisig (mainnet)", "curation"],
    ["YEARN_MS", "base-main", "0x90D0f26025571295D18a6c041E47450B81886B51", "Curation Multisig (base)", "curation"],
    ["YEARN_MS", "katana-main", "0x90D0f26025571295D18a6c041E47450B81886B51", "Curation Multisig (katana)", "curation"],
]

ALL_SAFE_ADDRESSES += YEARN_MULTISIGS

YEARN_EXPECTED_PROPOSERS: dict[tuple[str, str], set[str]] = {
    (entry[1], entry[2].lower()): {YEARN_PROPOSER_BOTS[entry[4]]} for entry in YEARN_MULTISIGS
}
