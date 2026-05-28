from utils.abi import load_abi
from utils.chains import Chain
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "lido"
PEG_THRESHOLD = 0.05  # 5% threshold

# Load ABIs
ABI_CURVE_POOL = load_abi("common-abi/CurvePool.json")
ABI_STETH = load_abi("lido/steth/abi/Steth.json")

# Contract addresses
STETH_ADDRESS = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
CURVE_POOL_ADDRESS = "0xDC24316b9AE028F1497c275EB9192a3Ea0f67022"


def check_steth_validator_rate(client):
    steth = client.eth.contract(address=STETH_ADDRESS, abi=ABI_STETH)

    with client.batch_requests() as batch:
        batch.add(steth.functions.totalSupply())
        batch.add(steth.functions.getTotalPooledEther())

        responses = client.execute_batch(batch)
        if len(responses) != 2:
            raise ValueError(f"Expected 2 responses from batch, got: {len(responses)}")

        ts, ta = responses
        return ta / ts


def check_peg(validator_rate, curve_rate):
    if curve_rate == 0:
        return False
    difference = abs(validator_rate - curve_rate)
    percentage_diff = difference / validator_rate
    return percentage_diff >= PEG_THRESHOLD


def main():
    client = ChainManager.get_client(Chain.MAINNET)
    curve_pool = client.eth.contract(address=CURVE_POOL_ADDRESS, abi=ABI_CURVE_POOL)

    validator_rate_unscaled = check_steth_validator_rate(client)
    message = f"🔄 1 stETH is: {validator_rate_unscaled:.5f} ETH in Lido\n"

    amounts = [1e18, 100e18, 1000e18]

    with client.batch_requests() as batch:
        for amount in amounts:
            batch.add(curve_pool.functions.get_dy(1, 0, int(amount)))

        try:
            curve_rates = client.execute_batch(batch)
            if len(curve_rates) != len(amounts):
                error_message = f"Batch response length mismatch. Expected: {len(amounts)}, Got: {len(curve_rates)}"
                send_telegram_message(error_message, PROTOCOL, True, True)
                return
        except Exception as e:
            error_message = f"Error executing batch curve pool calls: {e}"
            send_telegram_message(error_message, PROTOCOL, True, True)
            return

        for amount, curve_rate in zip(amounts, curve_rates):
            try:
                validator_rate_scaled = validator_rate_unscaled * amount
                if curve_rate is not None and check_peg(validator_rate_scaled, curve_rate):
                    human_readable_amount = amount / 1e18
                    human_readable_result = curve_rate / 1e18
                    message += f"📊 Swap result for amount {human_readable_amount:.5f}: {human_readable_result:.5f}"
                    send_telegram_message(message, PROTOCOL)
            except Exception as e:
                error_message = f"Error processing curve pool rate for amount {amount / 1e18:.2f}: {e}"
                send_telegram_message(error_message, PROTOCOL, True, True)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)
