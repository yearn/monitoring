from compound.collateral import check_collateral_risk
from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "comp"
logger = get_logger(PROTOCOL)
THRESHOLD_UR = 0.99

ABI_CTOKEN = load_abi("compound/abi/CTokenV3.json")

# Map addresses by chain: list of (address, name) tuples
ADDRESSES_BY_CHAIN = {
    Chain.MAINNET: [
        ("0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840", "cUSDTv3"),
        ("0xc3d688B66703497DAA19211EEdff47f25384cdc3", "cUSDCv3"),
        ("0xA17581A9E3356d9A858b789D68B4d866e593aE94", "cWETHv3"),
    ],
}


def print_stuff(chain_name: str, token_name: str, ur: float) -> None:
    logger.debug(f"Chain: {chain_name}, Token: {token_name}, UR: {ur}")
    if ur > THRESHOLD_UR:
        message = f"**BEEP BOP**\n💎 Market asset: {token_name}\n📊 Utilization rate: {ur:.2%}\n🌐 Chain: {chain_name}"
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


def process_assets(chain: Chain) -> None:
    client = ChainManager.get_client(chain)
    addresses = ADDRESSES_BY_CHAIN[chain]

    with client.batch_requests() as batch:
        for ctoken_address, _ in addresses:
            ctoken = client.eth.contract(address=ctoken_address, abi=ABI_CTOKEN)
            batch.add(ctoken.functions.getUtilization())

        responses = client.execute_batch(batch)
        if len(responses) != len(addresses):
            raise ValueError(f"Expected {len(addresses)} responses from batch, got: {len(responses)}")

    for (_, ctoken_name), response in zip(addresses, responses):
        ur = int(response) / 1e18
        print_stuff(chain.name, ctoken_name, ur)


def main():
    for chain in [Chain.MAINNET]:
        logger.info("Processing %s assets...", chain.name)
        process_assets(chain)
        check_collateral_risk(chain)


if __name__ == "__main__":
    main()
