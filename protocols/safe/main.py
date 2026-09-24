import itertools
import os
import time

import requests
from dotenv import load_dotenv
from web3 import Web3

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
    cache_filename,
    get_last_executed_nonce_from_file,
    get_last_value_for_key_from_file,
    write_last_executed_nonce_to_file,
    write_last_value_to_file,
)
from utils.chains import Chain, safe_network_to_chain_id
from utils.formatting import parse_wei
from utils.llm.ai_explainer import explain_batch_transaction, explain_transaction, format_explanation_line
from utils.logger import get_logger
from utils.telegram import escape_markdown, send_telegram_message
from utils.web3_wrapper import ChainManager

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
# Keys whose quota window is used up this run. Each key allows 50,000 requests per
# window; a 429 with ``x-ratelimit-remaining: 0`` means retrying only burns requests.
_exhausted_api_keys: dict[str, int] = {}  # key -> seconds until its quota resets

CACHE_KEY_QUOTA_ALERTED_UNTIL = "SAFE_API_QUOTA_ALERTED_UNTIL"
# Crash and quota alerts for this multi-safe script go to the general ops channel.
OPS_CHANNEL = "yearn"

SAFE_NONCE_ABI = [
    {"inputs": [], "name": "nonce", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}
]


class SafeApiQuotaExhausted(Exception):
    """Raised when every Safe API key has used up its request quota."""

    def __init__(self, reset_seconds: int) -> None:
        super().__init__(f"all Safe API keys exhausted; quota resets in {reset_seconds}s")
        self.reset_seconds = reset_seconds


def _next_api_key() -> str | None:
    """Return the next API key that still has quota, or None when all are exhausted."""
    for _ in range(len(_api_keys)):
        key = next(_api_key_cycle)
        if key not in _exhausted_api_keys:
            return key
    return None


def _quota_reset_seconds(response: requests.Response) -> int | None:
    """Return seconds until quota reset when a 429 means the key's quota is used up, else None."""
    if response.headers.get("x-ratelimit-remaining") != "0":
        return None
    try:
        return int(response.headers.get("x-ratelimit-reset", "0"))
    except ValueError:
        return 0


def get_safe_transactions(
    safe_address: str, network_name: str, executed: bool | None = None, limit: int = 10, max_retries: int = 3
) -> list[dict]:
    """
    Docs: https://docs.safe.global/core-api/transaction-service-reference/mainnet#List-a-Safe's-Multisig-Transactions

    Raises:
        SafeApiQuotaExhausted: When every API key has used up its quota; retrying
            would only burn requests until the window resets.
    """

    base_url = safe_apis[network_name] + "/api/v2"
    endpoint = f"{base_url}/safes/{safe_address}/multisig-transactions/"

    params = {"limit": limit, "ordering": "-nonce"}  # Order by nonce descending

    if executed is not None:
        params["executed"] = str(executed).lower()

    attempt = 0
    while attempt < max_retries:
        api_key = _next_api_key()
        if api_key is None:
            raise SafeApiQuotaExhausted(min(_exhausted_api_keys.values()))
        headers = {"Authorization": f"Bearer {api_key}"}

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
            attempt += 1
            continue

        if response.status_code == 200:
            return response.json()["results"]
        elif response.status_code == 401:
            raise ValueError("Invalid API key. Please check your SAFE_API_KEY.")
        elif response.status_code == 429:
            reset_seconds = _quota_reset_seconds(response)
            if reset_seconds is not None:
                # Quota used up: drop the key for this run and try the next one at once.
                _exhausted_api_keys[api_key] = reset_seconds
                logger.error("Safe API key quota exhausted; resets in %ss", reset_seconds)
                continue
            # Short-term rate limit - wait and retry
            wait_time = 2**attempt
            logger.warning("Rate limit hit, waiting %ss before retry...", wait_time)
            time.sleep(wait_time)
            attempt += 1
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
            attempt += 1
            continue
        else:
            logger.error("Error: %s\nResponse text: %s", response.status_code, response.text)
            return []

    logger.error("Failed after %s retries for %s on %s", max_retries, safe_address, network_name)
    return []


def get_safe_current_nonce(safe_address: str, network_name: str) -> int | None:
    """Read the safe's current onchain nonce (next nonce to use) over RPC.

    Read on-chain rather than from the Safe API so each safe costs one API
    request per run instead of two. Returns None if the call fails so callers
    can fail closed.
    """
    try:
        chain = Chain.from_chain_id(safe_network_to_chain_id(network_name))
        client = ChainManager.get_client(chain)
        safe = client.eth.contract(address=Web3.to_checksum_address(safe_address), abi=SAFE_NONCE_ABI)
        return int(safe.functions.nonce().call())
    except Exception as e:
        logger.warning("Failed to fetch current nonce for %s on %s: %s", safe_address, network_name, e)
        return None


def _is_executed_safe_tx(tx: dict) -> bool:
    """Return True for tx-service rows that are already executed.

    The API request asks for ``executed=false``, but the monitor treats the
    response defensively: if an item carries any executed marker, it must never
    reach the alert path.
    """
    return tx.get("isExecuted") is True or bool(tx.get("executionDate")) or bool(tx.get("transactionHash"))


def get_pending_transactions(safe_address: str, network_name: str) -> list[dict]:
    """Fetch pending transactions worth alerting on.

    Filters out two classes of noise:
    - Already-cached: nonce <= last_cached_nonce (we've alerted on them).
    - Dead-slot: nonce < safe.currentNonce. These remain in the API as
      ``executed=false`` because a competing tx at the same nonce executed
      first, but they will never run themselves.
    - Executed rows: any tx-service row with executed markers. These must never
      alert even if the API includes them in an ``executed=false`` response.

    The baseline is the higher of (a) the last nonce we already alerted on
    and (b) ``currentNonce - 1`` (the last *executed* nonce, when we could
    fetch it). A tx is eligible iff ``nonce > baseline``.

    When ``currentNonce`` is unknown (e.g. the RPC read failed), the chain
    baseline can't be computed safely, so we fail closed and skip alerts for
    this safe until the next run. That prevents dead-slot txs from alerting as
    queued after a competing tx at the same nonce has already executed.
    """
    last_cached_nonce = get_last_executed_nonce_from_file(safe_address)
    current_safe_nonce = get_safe_current_nonce(safe_address, network_name)
    if current_safe_nonce is None:
        logger.warning(
            "Skipping pending tx alerts for %s on %s because currentNonce could not be fetched",
            safe_address,
            network_name,
        )
        return []
    pending_txs = get_safe_transactions(safe_address, network_name, executed=False)

    baseline = last_cached_nonce
    chain_baseline = current_safe_nonce - 1
    if chain_baseline > last_cached_nonce:
        write_last_executed_nonce_to_file(safe_address, chain_baseline)
    baseline = max(baseline, chain_baseline)

    return [tx for tx in pending_txs if not _is_executed_safe_tx(tx) and int(tx["nonce"]) > baseline]


def _pending_filter_diag(safe_address: str, network_name: str) -> dict:
    """Snapshot the filter inputs for diagnostic logging.

    Re-reads the on-disk cache and the live ``currentNonce`` so
    ``check_for_pending_transactions`` can log the exact numbers that
    decided what to alert on, without leaking them through the
    ``get_pending_transactions`` return value.
    """
    last_cached = get_last_executed_nonce_from_file(safe_address)
    current = get_safe_current_nonce(safe_address, network_name)
    chain_baseline = (current - 1) if current is not None else None
    baseline = max(last_cached, chain_baseline) if chain_baseline is not None else last_cached
    return {
        "last_cached_nonce": last_cached,
        "current_safe_nonce": current,
        "chain_baseline": chain_baseline,
        "baseline": baseline,
    }


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
            # A utility label names the multisend contract, not the Safe, so the
            # report's Contract link must point at the target in that case.
            label_address=target if utility_label else safe_address,
        )

    # Non-multisend DELEGATECALLs (rare): skip sim but still try to explain.
    # Plain CALL txs: behave exactly as before.
    skip_sim = operation == 1
    context_note = build_context_note(tx, safe_address) if skip_sim else ""
    return explain_transaction(
        target=target,
        calldata=hex_data,
        chain_id=chain_id,
        value=parse_wei(tx.get("value")),
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
        diag = _pending_filter_diag(safe_address, network_name)
        logger.info(
            "safe %s on %s: last_cached=%s currentNonce=%s chain_baseline=%s baseline=%s pending=%d",
            safe_address,
            network_name,
            diag["last_cached_nonce"],
            diag["current_safe_nonce"],
            diag["chain_baseline"],
            diag["baseline"],
            len(pending_transactions),
        )
        highest_alerted_nonce = None
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

            # AI explanation (best-effort, non-blocking). Empty and short
            # payloads still reach the explainer — they are native transfers
            # or unknown selectors, not "nothing to explain".
            hex_data = tx.get("data") or "0x"
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
            if highest_alerted_nonce is None or nonce > highest_alerted_nonce:
                highest_alerted_nonce = nonce
                # Persist progress after each delivered alert, but never move
                # the scalar nonce cache backwards. The Safe tx-service returns
                # pending txs in DESCENDING nonce order, so a lower nonce must
                # not overwrite a higher nonce already alerted in this batch.
                write_last_executed_nonce_to_file(safe_address, highest_alerted_nonce)
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


def report_quota_exhausted(exc: SafeApiQuotaExhausted, now: float | None = None) -> None:
    """Alert once per quota window that Safe queue monitoring is blind.

    Args:
        exc: The exhaustion error, carrying seconds until the earliest key resets.
        now: Current unix time; defaults to ``time.time()``.
    """
    now = time.time() if now is None else now
    logger.error("Safe monitoring skipped: %s", exc)
    try:
        alerted_until = float(get_last_value_for_key_from_file(cache_filename, CACHE_KEY_QUOTA_ALERTED_UNTIL))
    except (TypeError, ValueError):
        alerted_until = 0.0
    if now < alerted_until:
        return
    reset_at = now + exc.reset_seconds
    send_telegram_message(
        "🚨 *Safe API quota exhausted*\n"
        f"All {len(_api_keys)} Safe API keys have used up their quota; pending Safe transactions "
        f"are NOT being monitored until the earliest reset "
        f"({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(reset_at))}).\n"
        f"Add a fresh key to {escape_markdown('SAFE_API_KEY')} or {escape_markdown('SAFE_API_KEY_2')} "
        "in /etc/monitoring/.env.",
        OPS_CHANNEL,
    )
    write_last_value_to_file(cache_filename, CACHE_KEY_QUOTA_ALERTED_UNTIL, int(reset_at))


def main():
    last_api_call_time = 0
    request_counter = 0
    # loop all
    for safe in ALL_SAFE_ADDRESSES:
        logger.info("Running for %s on %s", safe[0], safe[1])
        last_api_call_time, request_counter = check_api_limit(last_api_call_time, request_counter)
        try:
            run_for_network(safe[1], safe[2], safe[0])
        except SafeApiQuotaExhausted as exc:
            # Every remaining safe would hit the same exhausted keys; stop the run.
            report_quota_exhausted(exc)
            return
        request_counter += 1


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Multi-safe script with per-safe routing; crash alerts go to the general ops channel.
    run_with_alert(main, OPS_CHANNEL)
