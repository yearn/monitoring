import itertools
import os
import time

import requests
from dotenv import load_dotenv

from protocols.safe.addresses import (
    ALL_SAFE_ADDRESSES,
    PROXY_UPGRADE_SIGNATURES,
    YEARN_EXPECTED_PROPOSERS,
    safe_address_network_prefix,
    safe_apis,
)
from protocols.safe.multisend import build_context_note, extract_inner_calls, safe_utility_label
from protocols.safe.specific import handle_pendle
from utils.cache import (
    get_last_executed_nonce_from_file,
    write_last_executed_nonce_to_file,
)
from utils.chains import safe_network_to_chain_id
from utils.llm.ai_explainer import explain_batch_transaction, explain_transaction, format_explanation_line
from utils.logging import get_logger
from utils.telegram import escape_markdown, send_telegram_message

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
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            # Transient transport failure (connection reset, read timeout, DNS).
            # Retry with backoff instead of letting it bubble up and crash the run.
            wait_time = 2**attempt
            logger.warning(
                "Request error talking to Safe API (%s), waiting %ss before retry (attempt %s/%s)...",
                e,
                wait_time,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait_time)
            continue

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


def get_safe_current_nonce(safe_address: str, network_name: str) -> int | None:
    """Fetch the safe's current onchain nonce (next nonce to use).

    Uses the v1 Safe-info endpoint (v2 returns 404 for this resource). Returns
    None if the call fails so callers can fall back gracefully.
    """
    base_url = safe_apis[network_name] + "/api/v1"
    endpoint = f"{base_url}/safes/{safe_address}/"
    api_key = next(_api_key_cycle)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(endpoint, headers=headers, timeout=10)
        response.raise_for_status()
        return int(response.json()["nonce"])
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning("Failed to fetch current nonce for %s on %s: %s", safe_address, network_name, e)
        return None


def get_pending_transactions(safe_address: str, network_name: str) -> list[dict]:
    """Fetch pending transactions worth alerting on.

    Filters out two classes of noise:
    - Already-cached: nonce <= last_cached_nonce (we've alerted on them).
    - Dead-slot: nonce < safe.currentNonce. These remain in the API as
      ``executed=false`` because a competing tx at the same nonce executed
      first, but they will never run themselves.
    """
    last_cached_nonce = get_last_executed_nonce_from_file(safe_address)
    current_safe_nonce = get_safe_current_nonce(safe_address, network_name)
    pending_txs = get_safe_transactions(safe_address, network_name, executed=False)

    baseline = last_cached_nonce
    if current_safe_nonce is not None:
        chain_baseline = current_safe_nonce - 1
        if chain_baseline > last_cached_nonce:
            write_last_executed_nonce_to_file(safe_address, chain_baseline)
        baseline = max(baseline, chain_baseline)

    return [tx for tx in pending_txs if int(tx["nonce"]) > baseline]


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
    expected_proposers = YEARN_EXPECTED_PROPOSERS.get((network_name, safe_address.lower()), set())

    if pending_transactions:
        for tx in pending_transactions:
            nonce = int(tx["nonce"])

            target_contract = tx["to"]

            if protocol == "EULER" and target_contract != "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9":
                # send message for txs that target only vaults that we use in our strategies
                continue

            # Yearn multisigs (expected_proposers is non-empty) always get an
            # alert so there's a record of every queued tx. Expected-proposer
            # txs are sent silently (low importance); a tx proposed by an
            # address that is NOT one of our known bots is escalated to a loud
            # critical alert — it could mean a compromised proposer.
            is_yearn_multisig = bool(expected_proposers)
            unexpected_proposer = False
            tx_proposer = (tx.get("proposer") or "").lower()
            tx_delegate = (tx.get("proposedByDelegate") or "").lower()
            if is_yearn_multisig:
                matched = next((a for a in (tx_proposer, tx_delegate) if a and a in expected_proposers), None)
                if matched:
                    logger.info(
                        "Nonce %s on %s proposed by expected address %s",
                        nonce,
                        safe_address,
                        matched,
                    )
                else:
                    unexpected_proposer = True
                    logger.warning(
                        "Nonce %s on %s proposed by UNEXPECTED address (proposer=%s delegate=%s)",
                        nonce,
                        safe_address,
                        tx_proposer or "?",
                        tx_delegate or "?",
                    )

            message = ""
            if unexpected_proposer:
                message += (
                    "⚠️🚨 *CRITICAL: UNEXPECTED PROPOSER* 🚨⚠️\n"
                    "This Yearn multisig tx was NOT proposed by a known Yearn bot/EOA!\n"
                    f"👤 Proposer: {tx_proposer or 'unknown'}\n"
                    f"👤 Proposed by delegate: {tx_delegate or 'none'}\n\n"
                )
            message += (
                "🚨 QUEUED TX DETECTED 🚨\n"
                f"🅿️ Protocol: {escape_markdown(protocol)}\n"
                f"🔐 Safe Address: {safe_address}\n"
                f"🔗 Safe URL: {get_safe_url(safe_address, network_name)}\n"
                f"#️⃣ Nonce: {nonce}\n"
                f"📜 Target Contract Address: {target_contract}\n"
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
                message += f"\nℹ️ Additional Info: {escape_markdown(additional_info)}"

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

            # Silent for routine Yearn multisig txs (expected proposer); loud
            # for unexpected proposers and for all non-Yearn protocol alerts.
            disable_notification = is_yearn_multisig and not unexpected_proposer
            send_telegram_message(message, protocol, disable_notification)
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
    from utils.runner import run_with_alert

    # Multi-safe script with per-safe routing; crash alerts go to the general ops channel.
    run_with_alert(main, "yearn")
