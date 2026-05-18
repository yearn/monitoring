from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

THRESHOLD_UR = 0.98
PROTOCOL = "silo"
logger = get_logger(PROTOCOL)

# Define addresses by chain (following aave pattern)
ADDRESSES_BY_CHAIN = {
    Chain.ARBITRUM: {
        "lens": "0xBDb843c7a7e48Dc543424474d7Aa63b61B5D9536",
        "usdc_e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
        "silos": [
            ["Silo WSTETH-USDC.e", "0xA8897b4552c075e884BDB8e7b704eB10DB29BF0D"],
            ["Silo WBTC-USDC.e", "0x69eC552BE56E6505703f0C861c40039e5702037A"],
            ["Silo ARB-USDC.E", "0x0696E6808EE11a5750733a3d821F9bB847E584FB"],
        ],
    }
}

# Load ABI once
ABI_SILO_LENS = load_abi("silo/abi/SiloLens.json")


def print_stuff(chain_name, token_name, ur):
    if ur > THRESHOLD_UR:
        message = f"**BEEP BOP**\n💎 Market asset: {token_name}\n📊 Utilization rate: {ur:.2%}\n🌐 Chain: {chain_name}"
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


def process_assets(chain: Chain):
    chain_data = ADDRESSES_BY_CHAIN[chain]
    client = ChainManager.get_client(chain)
    silo_lens = client.eth.contract(address=chain_data["lens"], abi=ABI_SILO_LENS)

    with client.batch_requests() as batch:
        for silo_name, silo_address in chain_data["silos"]:
            batch.add(silo_lens.functions.getUtilization(silo_address, chain_data["usdc_e"]))

        responses = client.execute_batch(batch)
        if len(responses) != len(chain_data["silos"]):
            raise ValueError(f"Expected {len(chain_data['silos'])} responses from batch, got: {len(responses)}")

        for (silo_name, _), ur in zip(chain_data["silos"], responses):
            human_readable_ur = ur / 1e18
            print_stuff(chain.name, silo_name, human_readable_ur)


def main():
    for chain in [Chain.ARBITRUM]:
        logger.info("Processing %s Silos...", chain.name)
        process_assets(chain)


if __name__ == "__main__":
    main()
