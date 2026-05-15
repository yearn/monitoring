import itertools
import os
import time

import requests
from dotenv import load_dotenv

from safe.multisend import build_context_note, extract_inner_calls, safe_utility_label
from safe.specific import handle_pendle
from utils.cache import (
    get_last_executed_nonce_from_file,
    write_last_executed_nonce_to_file,
)
from utils.chains import safe_network_to_chain_id
from utils.llm.ai_explainer import explain_batch_transaction, explain_transaction, format_explanation_line
from utils.logging import get_logger
from utils.telegram import send_telegram_message

load_dotenv()
logger = get_logger("safe")

SAFE_WEBSITE_URL = "https://app.safe.global/transactions/queue?safe="
provider_url_mainnet = os.getenv("PROVIDER_URL_MAINNET")
provider_url_arb = os.getenv("PROVIDER_URL_ARBITRUM")

# Round-robin iterator over available Safe API keys.
_api_keys: list[str] = [k for k in [os.getenv("SAFE_API_KEY"), os.getenv("SAFE_API_KEY_2")] if k]
if not _api_keys:
    raise ValueError("At least one SAFE_API_KEY must be set.")
_api_key_cycle = itertools.cycle(_api_keys)

safe_address_network_prefix = {
    "mainnet": "eth",
    "arbitrum-main": "arb1",
    "optimism-main": "oeth",
    "polygon-main": "matic",
    "optim-yearn": "oeth",
    "base-main": "base",
}

safe_apis = {
    "mainnet": "https://api.safe.global/tx-service/eth",
    "arbitrum-main": "https://api.safe.global/tx-service/arb1",
    "optimism-main": "https://api.safe.global/tx-service/oeth",
    "polygon-main": "https://api.safe.global/tx-service/pol",
    "base-main": "https://api.safe.global/tx-service/base",
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

# combined addresses, add more addresses if needed, last item is optional for additional info message
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
        "MORPHO",
        "mainnet",
        "0x84258B3C495d8e9b10D0d4A7867392F149Da4274",
        "Morpho eUSDe predeposit vault owner",
    ],  # eUSDe predeposit vault owner, token used by DAI vault on morpho
    # [
    #     "RESOLV",
    #     "mainnet",
    #     "0xD6889F307BE1b83Bb355d5DA7d4478FB0d2Af547",
    #     "RESOLV contract",
    # ],
    [
        "LRT",
        "mainnet",
        "0xb7cB7131FFc18f87eEc66991BECD18f2FF70d2af",
        "LBTC boring vault big boss",
    ],  # LBTC boring vault big boss
    # [
    #     "LRT",
    #     "base-main",
    #     "0x92A19381444A001d62cE67BaFF066fA1111d7202",
    #     "Origin admin multisig. Markets used on Base",
    # ],  # origin admin
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
    # NOTE: Moonwell multisig monitoring is disabled for now
    # [
    #     "MOONWELL",
    #     "base-main",
    #     "0x446342AF4F3bCD374276891C6bb3411bf2F8779E",
    #     "Moonwell Admin of timelock controller",
    # ],  # admin of timelock controller
    # [
    #     "MOONWELL",
    #     "base-main",
    #     "0xB9d4acf113a423Bc4A64110B8738a52E51C2AB38",
    #     "Moonwell Pause guardian of comptroller contract",
    # ],  # pause guardian of comptroller contract
    # [
    #     "INFINIFI",
    #     "mainnet",
    #     "0x80608f852D152024c0a2087b16939235fEc2400c",
    #     "Infinifi Team Multisig",
    # ],
    # [
    #     "USD0",
    #     "mainnet",
    #     "0x6e9d65eC80D69b1f508560Bc7aeA5003db1f7FB7",
    # ],  # USD0 protocol governance
    # no active stargate strategies
    # ["STARGATE", "mainnet", "0x65bb797c2B9830d891D87288F029ed8dACc19705"],
    # ["STARGATE", "polygon-main", "0x47290DE56E71DC6f46C26e50776fe86cc8b21656"],
    # ["STARGATE", "optimism-main", "0x392AC17A9028515a3bFA6CCe51F8b70306C6bd43"],
    # ["STARGATE", "arbitrum-main", "0x9CD50907aeb5D16F29Bddf7e1aBb10018Ee8717d"],
    # TEST: yearn ms in mainnet 0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52
]


def get_safe_transactions(
    safe_address: str, network_name: str, executed: bool | None = None, limit: int = 10, max_retries: int = 3
) -> list[dict]:
    """
    Docs: https://docs.safe.global/core-api/transaction-service-reference/mainnet#List-a-Safe's-Multisig-Transactions
    """

    base_url = safe_apis[network_name] + "/api/v2"
    endpoint = f"{base_url}/safes/{safe_address}/multisig-transactions/"

    params = {"limit": limit, "ordering": "-nonce"}  # Order by nonce descending

    if executed is not None:
        params["executed"] = str(executed).lower()

    api_key = next(_api_key_cycle)

    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(max_retries):
        response = requests.get(endpoint, params=params, headers=headers)

        if response.status_code == 200:
            return response.json()["results"]
        elif response.status_code == 401:
            raise ValueError("Invalid API key. Please check your SAFE_API_KEY.")
        elif response.status_code == 429:
            # rate limit - wait and retry
            wait_time = 2**attempt
            logger.warning("Rate limit hit, waiting %ss before retry...", wait_time)
            time.sleep(wait_time)
            continue
        elif response.status_code >= 500:
            # server error - wait and retry with exponential backoff
            wait_time = 2**attempt
            logger.warning(
                "Server error %s, waiting %ss before retry (attempt %s/%s)...",
                response.status_code,
                wait_time,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait_time)
            continue
        else:
            logger.error("Error: %s", response.status_code)
            logger.error("Response text: %s", response.text)
            return []

    logger.error("Failed after %s retries for %s on %s", max_retries, safe_address, network_name)
    return []


def get_pending_transactions(safe_address: str, network_name: str) -> list[dict]:
    """Fetch pending transactions with nonce higher than the last cached nonce."""
    last_cached_nonce = get_last_executed_nonce_from_file(safe_address)
    pending_txs = get_safe_transactions(safe_address, network_name, executed=False)
    return [tx for tx in pending_txs if int(tx["nonce"]) > last_cached_nonce]


def get_safe_url(safe_address: str, network_name: str) -> str:
    return f"{SAFE_WEBSITE_URL}{safe_address_network_prefix[network_name]}:{safe_address}"


def _explain_safe_tx(
    tx: dict,
    target: str,
    hex_data: str,
    chain_id: int,
    protocol: str,
    safe_address: str,
    additional_info: str | None,
):
    """Pick the right AI explainer path for a Safe transaction.

    Safe txs with operation=DELEGATECALL into a multisend utility can't be
    modeled by our plain-CALL Tenderly simulator. Route them to the batch
    explainer (one call per inner tx) with simulation skipped, and feed the
    LLM a context note describing the delegated-execution semantics.
    """
    operation = int(tx.get("operation", 0) or 0)
    inner_calls = extract_inner_calls(tx) if operation == 1 else []

    if inner_calls:
        context_note = build_context_note(tx, safe_address)
        utility_label = safe_utility_label(target)
        label = utility_label or (additional_info or "")
        return explain_batch_transaction(
            calls=inner_calls,
            chain_id=chain_id,
            protocol=protocol,
            label=label,
            from_address=safe_address,
            skip_simulation=True,
            context_note=context_note,
            refine=True,
        )

    # Non-multisend DELEGATECALLs (rare): skip sim but still try to explain.
    # Plain CALL txs: behave exactly as before.
    skip_sim = operation == 1
    context_note = build_context_note(tx, safe_address) if skip_sim else ""
    return explain_transaction(
        target=target,
        calldata=hex_data,
        chain_id=chain_id,
        value=int(tx.get("value", 0)),
        protocol=protocol,
        label=additional_info or "",
        from_address=safe_address,
        skip_simulation=skip_sim,
        context_note=context_note,
        refine=True,
    )


def check_for_pending_transactions(safe_address: str, network_name: str, protocol: str) -> None:
    pending_transactions = get_pending_transactions(safe_address, network_name)

    if pending_transactions:
        for tx in pending_transactions:
            nonce = int(tx["nonce"])

            target_contract = tx["to"]

            if protocol == "EULER" and target_contract != "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9":
                # send message for txs that target only vaults that we use in our strategies
                continue

            message = (
                "🚨 QUEUED TX DETECTED 🚨\n"
                f"🅿️ Protocol: {protocol}\n"
                f"🔐 Safe Address: {safe_address}\n"
                f"🔗 Safe URL: {get_safe_url(safe_address, network_name)}\n"
                f"#️⃣ Nonce: {nonce}\n"
                f"📜 Target Contract Address: {target_contract}\n"
                f"💰 Value: {tx['value']}\n"
                f"📅 Submission Date: {tx['submissionDate']}"
            )
            # Find the additional info for the current safe address
            additional_info = None
            for safe in ALL_SAFE_ADDRESSES:
                if safe[2].lower() == safe_address.lower():
                    if len(safe) > 3:
                        additional_info = safe[3]
                    break  # Found the safe, no need to continue loop

            if additional_info:
                message += f"\nℹ️ Additional Info: {additional_info}"

            # pendle uses specific owner of the contracts where we need to decode the data
            if protocol == "PENDLE":
                hex_data = tx["data"]
                # if hex data doesnt contain any of the proxy upgrade signatures, skip
                if not any(signature in hex_data for signature in PROXY_UPGRADE_SIGNATURES):
                    logger.info("Skipping tx with nonce %s as it does not contain any proxy upgrade signatures.", nonce)
                    continue

                try:
                    if network_name == "mainnet":
                        message += handle_pendle(provider_url_mainnet, hex_data)
                    elif network_name == "arbitrum-main":
                        message += handle_pendle(provider_url_arb, hex_data)
                except Exception as e:
                    logger.error("Cannot decode Pendle aggregate calls: %s", e)

            # AI explanation (best-effort, non-blocking)
            hex_data = tx.get("data", "0x")
            if hex_data and len(hex_data) >= 10:
                chain_id = safe_network_to_chain_id(network_name)
                try:
                    explanation = _explain_safe_tx(
                        tx=tx,
                        target=target_contract,
                        hex_data=hex_data,
                        chain_id=chain_id,
                        protocol=protocol,
                        safe_address=safe_address,
                        additional_info=additional_info,
                    )
                    if explanation:
                        message += format_explanation_line(explanation)
                except Exception:
                    logger.debug("AI explanation failed for Safe tx nonce=%s", nonce, exc_info=True)

            send_telegram_message(message, protocol, False)  # explicitly enable notification
            # write the last executed nonce to file
            write_last_executed_nonce_to_file(safe_address, nonce)
    else:
        logger.info("No pending transactions found with higher nonce than the last executed transaction.")


def check_api_limit(last_api_call_time: float, request_counter: int) -> tuple[float, int]:
    current_time = time.time()
    if current_time - last_api_call_time > 1:
        last_api_call_time = current_time
        request_counter = 0
    elif request_counter >= 4:
        time.sleep(1)
        request_counter = 0
        last_api_call_time = time.time()

    return last_api_call_time, request_counter


def run_for_network(network_name: str, safe_address: str, protocol: str) -> None:
    check_for_pending_transactions(safe_address, network_name, protocol)


def main():
    last_api_call_time = 0
    request_counter = 0
    # loop all
    for safe in ALL_SAFE_ADDRESSES:
        logger.info("Running for %s on %s", safe[0], safe[1])
        last_api_call_time, request_counter = check_api_limit(last_api_call_time, request_counter)
        run_for_network(safe[1], safe[2], safe[0])
        request_counter += 1


if __name__ == "__main__":
    main()
