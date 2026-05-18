import datetime

from web3 import Web3

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.config import Config
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

# Constants
PROTOCOL = "usdai"
logger = get_logger(PROTOCOL)

USDAI_VAULT_ADDR = Web3.to_checksum_address("0x0A1a1A107E45b7Ced86833863f482BC5f4ed82EF")
PYUSD_TOKEN_ADDR = Web3.to_checksum_address("0x46850aD61C2B7d64d08c9C754F45254596696984")
# ERC20 decimals/symbol at USDAI_VAULT_ADDR and PYUSD_TOKEN_ADDR on Arbitrum (verified on-chain).
USDAI_DECIMALS = 18
PYUSD_DECIMALS = 6
PYUSD_SYMBOL = "PYUSD"
SUSDAI_ADDR = Web3.to_checksum_address("0x0B2b2B2076d95dda7817e785989fE353fe955ef9")
LOAN_ROUTER_ADDR = Web3.to_checksum_address("0x0C2ED170F2bB1DF1a44292Ad621B577b3C9597D1")
USDAI_INVARIANT_BREACH_THRESHOLD_RAW = Config.get_env_int("USDAI_INVARIANT_BREACH_THRESHOLD_RAW", 100 * 10**18)

USDAI_PROXY_READ_ABI = [
    {
        "inputs": [],
        "name": "bridgedSupply",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def send_breach_alert_once(cache_key, alert_message, severity=AlertSeverity.HIGH):
    last_state = int(get_last_value_for_key_from_file(cache_filename, cache_key))
    if last_state == 0:
        send_alert(Alert(severity, alert_message, PROTOCOL))
        write_last_value_to_file(cache_filename, cache_key, 1)


def clear_breach_state(cache_key):
    last_state = int(get_last_value_for_key_from_file(cache_filename, cache_key))
    if last_state == 1:
        write_last_value_to_file(cache_filename, cache_key, 0)


def get_loan_details(client, owner_addr):
    """
    Reads active loan NFTs held by the owner.
    Returns list of (token_id, principal, maturity).
    """
    loans = []
    try:
        abi = load_abi("common-abi/LoanRouter.json")
        router = client.get_contract(LOAN_ROUTER_ADDR, abi)
        count = router.functions.balanceOf(owner_addr).call()

        if count > 0:
            # 1. Get all token IDs
            with client.batch_requests() as batch:
                for i in range(count):
                    batch.add(router.functions.tokenOfOwnerByIndex(owner_addr, i))

                token_ids = client.execute_batch(batch)

            # 2. Get Loan States
            with client.batch_requests() as batch:
                for token_id in token_ids:
                    batch.add(router.functions.loanState(token_id))

                loan_states = client.execute_batch(batch)

            for token_id, state_tuple in zip(token_ids, loan_states):
                # state_tuple: (state, maturity, start, principal)
                # We need maturity (index 1) and principal (index 3)
                if state_tuple and len(state_tuple) >= 4:
                    maturity = state_tuple[1]
                    principal = state_tuple[3]
                    loans.append({"id": token_id, "principal": principal / 1e18, "maturity": maturity})

    except Exception as e:
        logger.error("Loan scan error: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"Loan scan error: {e}", PROTOCOL), plain_text=True)

    return loans


def main():
    client = ChainManager.get_client(Chain.ARBITRUM)

    # Common ABI
    erc20_abi = load_abi("common-abi/ERC20.json")

    usdai = client.get_contract(USDAI_VAULT_ADDR, erc20_abi)
    usdai_read = client.get_contract(USDAI_VAULT_ADDR, USDAI_PROXY_READ_ABI)
    pyusd = client.get_contract(PYUSD_TOKEN_ADDR, erc20_abi)

    try:
        # --- 1) USDai Invariant Inputs ---
        with client.batch_requests() as batch:
            batch.add(usdai.functions.totalSupply())
            batch.add(usdai_read.functions.bridgedSupply())
            batch.add(pyusd.functions.balanceOf(USDAI_VAULT_ADDR))
            usdai_supply_raw, bridged_supply_raw, pyusd_assets_raw = client.execute_batch(batch)

        usdai_supply_fmt = usdai_supply_raw / (10**USDAI_DECIMALS)
        bridged_supply_fmt = bridged_supply_raw / (10**USDAI_DECIMALS)
        pyusd_assets_fmt = pyusd_assets_raw / (10**PYUSD_DECIMALS)
        pyusd_assets_18_raw = pyusd_assets_raw * (10 ** max(USDAI_DECIMALS - PYUSD_DECIMALS, 0))

        logger.info("--- USDai Stats ---")
        logger.info("USDai Supply:    $%s", f"{usdai_supply_fmt:,.2f}")
        logger.info("USDai Bridged:   $%s", f"{bridged_supply_fmt:,.2f}")
        logger.info("%s Assets:    $%s", PYUSD_SYMBOL, f"{pyusd_assets_fmt:,.2f}")

        # Invariant:
        # totalSupply + bridgedSupply <= pyUSD.balanceOf(USDai)  (all in 1e18 scale)
        required_backing_raw = usdai_supply_raw + bridged_supply_raw
        invariant_gap_raw = required_backing_raw - pyusd_assets_18_raw
        invariant_gap_fmt = invariant_gap_raw / (10**USDAI_DECIMALS)
        required_backing_fmt = required_backing_raw / (10**USDAI_DECIMALS)

        logger.info("Required Backing: $%s (supply + bridged)", f"{required_backing_fmt:,.2f}")
        logger.info("Invariant Gap:    $%s (required - pyUSD assets)", f"{invariant_gap_fmt:,.2f}")

        cache_key_invariant_breach = f"{PROTOCOL}_backing_invariant_breach"
        if invariant_gap_raw >= USDAI_INVARIANT_BREACH_THRESHOLD_RAW:
            send_breach_alert_once(
                cache_key=cache_key_invariant_breach,
                severity=AlertSeverity.HIGH,
                alert_message=(
                    "*USDai Backing Invariant Breach*\n\n"
                    f"Invariant violated by at least {USDAI_INVARIANT_BREACH_THRESHOLD_RAW / 1e18:,.0f} USDai.\n"
                    f"totalSupply + bridgedSupply: ${required_backing_fmt:,.2f}\n"
                    f"{PYUSD_SYMBOL} balance in USDai contract: ${pyusd_assets_fmt:,.2f}\n"
                    f"Excess (required - assets): ${invariant_gap_fmt:,.2f}"
                ),
            )
        else:
            clear_breach_state(cache_key_invariant_breach)

        # --- Loan Monitoring (GPU Loans) ---
        all_loans = get_loan_details(client, SUSDAI_ADDR)

        # --- Manual Adjustment for Legacy Loan ---
        # There is one active loan (NVIDIA H200s) originated before the Loan Router deployment.
        # Principal: ~$560k (Remaining Principal from UI: $559,542)
        # We hardcode this to ensure accurate Total Principal tracking.
        legacy_loan_principal = 559_542.00

        # Legacy Loan Expiry Alert
        legacy_loan_expiry = 1848258000  # 2028-07-27

        if legacy_loan_expiry > 0:
            if datetime.datetime.now().timestamp() > legacy_loan_expiry:
                send_alert(
                    Alert(
                        AlertSeverity.MEDIUM,
                        "*Legacy Loan Expired*\n\nThe legacy loan (NVIDIA H200s) has expired.",
                        PROTOCOL,
                    )
                )

        if all_loans or legacy_loan_principal > 0:
            logger.info("--- Active Loan NFTs (Direct Read) ---")
            total_verified_principal = 0

            # Add Legacy Loan
            logger.info("Legacy Loan (H200s): $%s (Hardcoded)", f"{legacy_loan_principal:,.2f}")
            total_verified_principal += legacy_loan_principal

            for loan in all_loans:
                mat_date = datetime.datetime.fromtimestamp(loan["maturity"]).strftime("%Y-%m-%d")
                logger.info("Loan #%04d...: $%s (Mat: %s)", loan["id"] % 10000, f"{loan['principal']:,.2f}", mat_date)
                total_verified_principal += loan["principal"]

            logger.info("Total Verified Principal: $%s", f"{total_verified_principal:,.2f}")

            # Calculate Ratio to Total Supply
            verified_ratio = (total_verified_principal / usdai_supply_fmt * 100) if usdai_supply_fmt > 0 else 0
            logger.info("Verified Loan Ratio: %s%% of Total Supply", f"{verified_ratio:.2f}")

            # --- Alerting on Principal Change ---
            cache_key_principal = f"{PROTOCOL}_verified_principal"
            last_principal = float(get_last_value_for_key_from_file(cache_filename, cache_key_principal))

            diff = abs(total_verified_principal - last_principal)

            # Check for change (allow small dust difference < $1.00 and <1% of total loans)
            percent_change = (diff / last_principal * 100) if last_principal > 0 else 0
            if last_principal != 0 and diff > 1.0 and percent_change >= 1.0:
                change_type = (
                    "increased (New Loan)" if total_verified_principal > last_principal else "reduced (Repayment)"
                )

                msg = (
                    "*sUSDai Loan Activity*\n\n"
                    f"Total Verified Principal has {change_type}.\n"
                    f"Change: ${diff:,.2f} ({percent_change:.2f}% of Total Loans)\n"
                    f"Old Total: ${last_principal:,.2f}\n"
                    f"New Total: ${total_verified_principal:,.2f}\n"
                    f"Current Ratio: {verified_ratio:.2f}% of Supply"
                )
                send_alert(Alert(AlertSeverity.LOW, msg, PROTOCOL))

            # Update cache
            write_last_value_to_file(cache_filename, cache_key_principal, total_verified_principal)

    except Exception as e:
        logger.error("Error: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"USDai monitoring failed: {e}", PROTOCOL), plain_text=True)


if __name__ == "__main__":
    main()
