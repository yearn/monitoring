import hashlib
import itertools
import os
import re
import time
from dataclasses import replace

import requests
from dotenv import load_dotenv
from web3 import Web3

from protocols.safe.addresses import (
    ALL_SAFE_ADDRESSES,
    SAFE_QUEUE_API_URL,
    YEARN_EXPECTED_PROPOSERS,
    safe_address_network_prefix,
)
from protocols.safe.executed import (
    NonceCheck,
    NonceCheckState,
    SafeKey,
    TxId,
    load_nonce_check_state,
    plan_nonce_check,
    read_onchain_nonces,
    save_nonce_check_state,
)
from protocols.safe.filters import should_alert
from protocols.safe.multisend import (
    ZERO_ADDRESS,
    build_context_note,
    extract_inner_calls,
    is_simulatable_multisend,
    safe_utility_label,
)
from utils.cache import (
    cache_filename,
    get_last_value_for_key_from_file,
    nonces_filename,
    write_last_value_to_file,
)
from utils.chains import safe_network_to_chain_id
from utils.formatting import parse_wei
from utils.llm.ai_explainer import Explanation, explain_batch_transaction, explain_transaction, format_explanation_line
from utils.llm.credits import check_llm_credits
from utils.logger import get_logger
from utils.proxy import ProxyUpgrade, dedupe_upgrades, find_proxy_upgrades, format_upgrade_lines
from utils.telegram import (
    escape_markdown,
    send_error_message,
    send_telegram_message,
    telegram_credentials_configured,
)
from utils.upgrade_diff import (
    UpgradeDiffLink,
    apply_upgrade_diff_links,
    format_upgrade_report_links,
    resolve_upgrade_diff_links,
)

load_dotenv()
logger = get_logger("safe")

SAFE_WEBSITE_URL = "https://app.safe.global/transactions/queue?safe="
SAFE_HISTORY_URL = "https://app.safe.global/transactions/history?safe="
# Queue Service ``proposedVia`` values whose ``proposer`` / ``proposedBy`` semantics we rely on.
_KNOWN_PROPOSAL_ROUTES = frozenset({"OWNER", "DELEGATE", "NESTED_OWNER"})
# How much of an unexpected Safe API error body to put in the error alert.
_ERROR_BODY_CHARS = 500

# Values treated as "no key": unset env templates and serialized nulls.
_PLACEHOLDER_KEYS = {"null", "none", "nil", "undefined", "false", "0", "changeme"}


def _is_usable_api_key(value: str | None) -> bool:
    """Return whether an env value looks like a real API key rather than empty or a placeholder."""
    if value is None:
        return False
    value = value.strip().strip("\"'")
    if not value or value.lower() in _PLACEHOLDER_KEYS:
        return False
    # .env.example ships "your-api-key" / "your-second-api-key"; <...> is another common template.
    return not (value.lower().startswith("your-") or (value.startswith("<") and value.endswith(">")))


def load_safe_api_keys() -> list[str]:
    """Read SAFE_API_KEY, SAFE_API_KEY_2, SAFE_API_KEY_3, ... in order.

    Stops at the first missing, empty, or placeholder value, so keys must be
    numbered without gaps. Quota is 50,000 requests per 30-day window per developer
    account; keys on the same account share it.
    """
    keys: list[str] = []
    index = 1
    while True:
        name = "SAFE_API_KEY" if index == 1 else f"SAFE_API_KEY_{index}"
        value = os.getenv(name)
        if value is None or not _is_usable_api_key(value):
            break
        keys.append(value.strip().strip("\"'"))
        index += 1
    return keys


# Round-robin iterator over available Safe API keys.
_api_keys: list[str] = load_safe_api_keys()
if not _api_keys:
    raise ValueError("At least one SAFE_API_KEY must be set.")
_api_key_cycle = itertools.cycle(_api_keys)
# Keys whose account quota is used up this run. A 429 with ``x-ratelimit-remaining: 0``
# means retrying only burns requests. Extra keys on the same account share one quota.
_exhausted_api_keys: dict[str, int] = {}  # key -> seconds until its quota resets

CACHE_KEY_QUOTA_ALERTED_UNTIL = "SAFE_API_QUOTA_ALERTED_UNTIL"
# Per-key "exhausted until" unix time, so later runs skip a dead key instead of spending a
# request on it (and logging) every run until its window resets. Keyed by a hash so the
# cache never holds the key itself.
CACHE_KEY_EXHAUSTED_UNTIL_PREFIX = "SAFE_API_KEY_EXHAUSTED_UNTIL_"
# Crash and quota alerts are operational: they go to the internal errors channel with a
# ``[yearn]`` label and are stored as ``yearn-internal`` so they stay off the public
# Yearn monitoring page. Never send them to a protocol's public channel or topic.
ERROR_LABEL = "yearn"
ALERT_PROTOCOL = "yearn-internal"

# The Queue Service's maximum page size. One page covers every watched Safe's queue together.
SAFE_QUEUE_PAGE_SIZE = 100
# Hard cap on pages per run, so a runaway queue cannot drain the shared request quota.
SAFE_QUEUE_MAX_PAGES = 5
_SAFE_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
# Per (network, safe) cache entry listing the queued txs already alerted on (or skipped by the
# protocol's alert rule), as comma-separated ``nonce@safeTxHash`` items.
CACHE_KEY_HANDLED_TXS_PREFIX = "safe_handled_txs"
# The batch endpoint's cap on hashes per request.
SAFE_TX_HASH_BATCH_SIZE = 100


class SafeApiQuotaExhausted(Exception):
    """Raised when every Safe API key has used up its request quota."""

    def __init__(self, reset_seconds: int) -> None:
        super().__init__(f"all Safe API keys exhausted; quota resets in {reset_seconds}s")
        self.reset_seconds = reset_seconds


class SafeApiAuthError(Exception):
    """Raised when the Safe API rejects the API key; every safe in the run would fail the same way."""


class SafeApiRequestError(Exception):
    """Raised when the Safe API rejects a request, e.g. a Safe it cannot resolve."""


class SafeApiUnavailable(Exception):
    """Raised when Safe API retries run out; every Safe in the request is unchecked this run."""


def _next_api_key() -> str | None:
    """Return the next API key that still has quota, or None when all are exhausted."""
    for _ in range(len(_api_keys)):
        key = next(_api_key_cycle)
        if key not in _exhausted_api_keys:
            return key
    return None


def _exhausted_cache_key(api_key: str) -> str:
    """Return the cache key holding when ``api_key``'s quota resets."""
    return CACHE_KEY_EXHAUSTED_UNTIL_PREFIX + hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _mark_key_exhausted(api_key: str, reset_seconds: int, now: float | None = None) -> None:
    """Skip ``api_key`` for the rest of this run and persist its reset time for later runs."""
    now = time.time() if now is None else now
    _exhausted_api_keys[api_key] = reset_seconds
    write_last_value_to_file(cache_filename, _exhausted_cache_key(api_key), int(now + reset_seconds))
    key_number = _api_keys.index(api_key) + 1 if api_key in _api_keys else "?"
    logger.warning(
        "Safe API key #%s quota exhausted; skipping it until %s",
        key_number,
        time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now + reset_seconds)),
    )


def load_known_exhausted_keys(now: float | None = None) -> None:
    """Pre-mark keys whose quota a previous run found exhausted and has not reset yet."""
    now = time.time() if now is None else now
    for number, api_key in enumerate(_api_keys, start=1):
        try:
            exhausted_until = float(get_last_value_for_key_from_file(cache_filename, _exhausted_cache_key(api_key)))
        except TypeError, ValueError:
            continue
        if exhausted_until > now:
            _exhausted_api_keys[api_key] = int(exhausted_until - now)
            logger.info(
                "Skipping Safe API key #%s until %s (quota exhausted)",
                number,
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exhausted_until)),
            )


def _quota_reset_seconds(response: requests.Response) -> int | None:
    """Return seconds until quota reset when a 429 means the key's quota is used up, else None."""
    if response.headers.get("x-ratelimit-remaining") != "0":
        return None
    try:
        return int(response.headers.get("x-ratelimit-reset", "0"))
    except ValueError:
        return 0


def _queue_request(
    path: str, params: list[tuple[str, str | int]], context: str, max_retries: int
) -> dict | list | None:
    """Send one Queue Service GET request, retrying transient failures.

    Args:
        path: Endpoint path under ``/api/v1``.
        params: Query parameters; a repeated parameter appears once per value.
        context: What the request covers, for log and error messages.
        max_retries: Attempts for transport errors, rate limits and 5xx responses.

    Returns:
        The response body, or ``None`` when retries run out.

    Raises:
        SafeApiQuotaExhausted: When every API key has used up its quota.
        SafeApiAuthError: When the service rejects the API key.
        SafeApiRequestError: When the service rejects the request (e.g. 422 for a Safe it cannot resolve).
    """
    endpoint = f"{SAFE_QUEUE_API_URL}/api/v1/{path}"
    attempt = 0
    while attempt < max_retries:
        api_key = _next_api_key()
        if api_key is None:
            raise SafeApiQuotaExhausted(min(_exhausted_api_keys.values()))
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=20)
        except requests.exceptions.RequestException as e:
            # Transient transport failure (connection reset, read timeout, DNS).
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
            return response.json()
        elif response.status_code == 401:
            raise SafeApiAuthError("Invalid API key. Please check your SAFE_API_KEY.")
        elif response.status_code == 429:
            reset_seconds = _quota_reset_seconds(response)
            if reset_seconds is not None:
                # Quota used up: drop the key until it resets and try the next one at once.
                _mark_key_exhausted(api_key, reset_seconds)
                continue
            # Short-term rate limit - wait and retry
            wait_time = 2**attempt
            logger.warning("Rate limit hit, waiting %ss before retry...", wait_time)
            time.sleep(wait_time)
            attempt += 1
            continue
        elif response.status_code >= 500:
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
            # 422 means the service rejected a Safe in the request. Returning nothing here
            # would silently stop alerts, so surface it instead.
            raise SafeApiRequestError(
                f"Safe API returned {response.status_code} for {context}: {response.text[:_ERROR_BODY_CHARS]}"
            )

    logger.error("Failed after %s retries for %s", max_retries, context)
    return None


def fetch_queued_transactions(
    safes: list[SafeKey], max_retries: int = 3, max_pages: int = SAFE_QUEUE_MAX_PAGES
) -> tuple[dict[SafeKey, list[dict]], bool]:
    """List the executable queued txs of several Safes with one paged Queue Service request.

    Docs: https://docs.safe.global/core-api/safe-queue-service-reference

    The ``/queue`` endpoint returns only txs that are not executed and whose nonce is at
    or above their Safe's current nonce, so dead slots never come back and no onchain
    nonce read is needed. Every Safe shares the same request, so a run costs one request
    against the shared Safe API quota (more only past ``SAFE_QUEUE_PAGE_SIZE`` queued txs).

    Args:
        safes: ``(network_name, safe_address)`` pairs to list.
        max_retries: Attempts per page for transport errors, rate limits and 5xx responses.
        max_pages: Upper bound on pages fetched.

    Returns:
        Each requested Safe's queued txs (lowest nonce first), and whether the listing is
        complete. It is incomplete only when ``max_pages`` was hit; callers must not treat
        a tx missing from an incomplete listing as gone.

    Raises:
        SafeApiQuotaExhausted: When every API key has used up its quota.
        SafeApiAuthError: When the service rejects the API key.
        SafeApiRequestError: When the service rejects the request.
        SafeApiUnavailable: When retries run out for a page.
    """
    queues: dict[SafeKey, list[dict]] = {safe: [] for safe in safes}
    by_param = {_safe_param(network, address): (network, address) for network, address in safes}
    context = f"{len(safes)} Safe(s)" if len(safes) > 1 else f"{safes[0][1]} on {safes[0][0]}"
    seen: set[tuple[SafeKey, TxId]] = set()
    params: list[tuple[str, str | int]] = [("safes", param) for param in by_param]
    params += [("nonceOrder", "asc"), ("limit", SAFE_QUEUE_PAGE_SIZE)]
    for page in range(max_pages):
        page_params = params + ([("offset", page * SAFE_QUEUE_PAGE_SIZE)] if page else [])
        body = _queue_request("multisig-transactions/queue", page_params, context, max_retries)
        if not isinstance(body, dict):
            raise SafeApiUnavailable(f"Safe API retries exhausted for {context} at page {page + 1}")
        for tx in body.get("results", []):
            safe = by_param.get(f"{str(tx.get('safe', '')).lower()}:{tx.get('chainId')}")
            if safe is None:
                logger.warning(
                    "Ignoring queued tx for unrequested Safe %s on chain %s", tx.get("safe"), tx.get("chainId")
                )
                continue
            key = (safe, _tx_id(tx))
            # Pages fetched one after another can overlap when the queue changes in between.
            if key not in seen:
                seen.add(key)
                queues[safe].append(tx)
        if not body.get("next"):
            return queues, True
    logger.warning("Stopped after %s pages of queued txs for %s; higher nonces were not fetched", max_pages, context)
    return queues, False


def _safe_param(network_name: str, safe_address: str) -> str:
    """Return a Safe's ``address:chainId`` value for the Queue Service ``safes`` parameter."""
    return f"{safe_address.lower()}:{safe_network_to_chain_id(network_name)}"


def fetch_executed_safe_tx_hashes(safe_tx_hashes: list[str], max_retries: int = 3) -> set[str]:
    """Return which of ``safe_tx_hashes`` the Queue Service reports as executed.

    One request per ``SAFE_TX_HASH_BATCH_SIZE`` hashes, across every Safe: a
    ``safeTxHash`` commits to its chain and Safe, so the hashes never collide.

    Args:
        safe_tx_hashes: Lowercased ``safeTxHash`` values to look up.
        max_retries: Attempts per request for transport errors, rate limits and 5xx responses.

    Raises:
        SafeApiQuotaExhausted: When every API key has used up its quota.
        SafeApiAuthError: When the service rejects the API key.
        SafeApiRequestError: When the service rejects the request.
        SafeApiUnavailable: When retries run out.
    """
    executed: set[str] = set()
    for start in range(0, len(safe_tx_hashes), SAFE_TX_HASH_BATCH_SIZE):
        chunk = safe_tx_hashes[start : start + SAFE_TX_HASH_BATCH_SIZE]
        context = f"{len(chunk)} executed-tx lookup(s)"
        body = _queue_request("multisig-transactions/batch", [("safeTxHash", h) for h in chunk], context, max_retries)
        if not isinstance(body, list):
            raise SafeApiUnavailable(f"Safe API retries exhausted for {context}")
        executed.update(str(tx["safeTxHash"]).lower() for tx in body if tx.get("txHash"))
    return executed


def _safe_tx_hash(tx: dict) -> str:
    """Return the tx's ``safeTxHash`` when it is a well-formed 32-byte hex string, else ``""``."""
    safe_tx_hash = tx.get("safeTxHash")
    if isinstance(safe_tx_hash, str) and _SAFE_TX_HASH_RE.fullmatch(safe_tx_hash):
        return safe_tx_hash
    return ""


def _tx_id(tx: dict) -> TxId:
    """Return the ``(nonce, lowercased safeTxHash)`` a queued tx is deduplicated on.

    The safeTxHash commits to the chain, the Safe and the nonce, so it alone identifies
    the tx; the nonce rides along to keep the stored entries readable. A row without a usable hash falls back to ``(nonce, "")``,
    i.e. nonce-only deduplication.
    """
    return int(tx["nonce"]), _safe_tx_hash(tx).lower()


def _handled_txs_cache_key(safe_address: str, network_name: str) -> str:
    """Return the cache key for a Safe's handled txs; same-address Safes on other chains get their own."""
    return f"{CACHE_KEY_HANDLED_TXS_PREFIX}+{network_name}+{safe_address.lower()}"


def load_handled_txs(safe_address: str, network_name: str) -> set[TxId]:
    """Read the queued txs already alerted on (or skipped by the alert rule).

    Returns:
        The handled tx ids; empty when nothing was ever stored for this Safe.
    """
    raw = get_last_value_for_key_from_file(nonces_filename, _handled_txs_cache_key(safe_address, network_name))
    if not raw:
        return set()
    handled: set[TxId] = set()
    for item in str(raw).split(","):
        nonce, sep, safe_tx_hash = item.partition("@")
        if sep and nonce.isdigit():
            handled.add((int(nonce), safe_tx_hash))
    return handled


def save_handled_txs(safe_address: str, network_name: str, handled: set[TxId]) -> None:
    """Persist the handled tx ids as comma-separated ``nonce@safeTxHash`` items."""
    value = ",".join(f"{nonce}@{safe_tx_hash}" for nonce, safe_tx_hash in sorted(handled))
    write_last_value_to_file(nonces_filename, _handled_txs_cache_key(safe_address, network_name), value)


def mark_tx_handled(safe_address: str, network_name: str, tx: dict) -> None:
    """Record one tx as handled right away, so a later failure in the run cannot re-alert it."""
    handled = load_handled_txs(safe_address, network_name)
    handled.add(_tx_id(tx))
    save_handled_txs(safe_address, network_name, handled)


def get_pending_transactions(
    safe_address: str, network_name: str, queued_txs: list[dict], complete: bool = True
) -> list[dict]:
    """Return the queued txs of one Safe that have not been alerted on yet.

    Deduplicating on the ``safeTxHash`` rather than on a nonce high-water mark means a
    tx queued later at a lower pending nonce, or a replacement proposed at an
    already-alerted nonce, still alerts.

    A handled entry is pruned once it is out of the queue (executed, replaced or deleted)
    and its nonce is settled by the executed-nonce check, which needs the entry to tell
    an alerted execution from an unalerted one. Pruning only happens on a ``complete``
    listing: a tx missing from a truncated one may still be queued.

    Args:
        safe_address: The Safe's address.
        network_name: Safe network name from the watchlist.
        queued_txs: The Safe's executable queued txs from ``fetch_queued_transactions``.
        complete: Whether ``queued_txs`` is the Safe's whole queue.
    """
    stored = load_handled_txs(safe_address, network_name)
    handled = stored
    nonce_check = load_nonce_check_state((network_name, safe_address))
    if complete and nonce_check is not None:
        queued_ids = {_tx_id(tx) for tx in queued_txs}
        handled = {tx_id for tx_id in stored if tx_id in queued_ids or not nonce_check.is_settled(tx_id[0])}
    if handled != stored:
        save_handled_txs(safe_address, network_name, handled)

    pending = [tx for tx in queued_txs if _tx_id(tx) not in handled]
    logger.info(
        "safe %s on %s: queued=%d handled=%d new=%d",
        safe_address,
        network_name,
        len(queued_txs),
        len(handled),
        len(pending),
    )
    return pending


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
) -> Explanation | None:
    """Pick the right AI explainer path for a Safe transaction.

    Safe txs with operation=DELEGATECALL into a multisend utility go to the
    batch explainer (one call per inner tx) with a context note describing the
    delegated-execution semantics. When every inner tx is a plain CALL, the
    batch is simulated as one ordered bundle from the Safe — exactly what the
    multisend executes. An inner DELEGATECALL can't be modeled, so those
    batches skip simulation.
    """
    safe_tx_hash = _safe_tx_hash(tx)
    operation = int(tx.get("operation", 0) or 0)
    inner_calls = extract_inner_calls(tx, safe_address)

    if inner_calls:
        simulate = is_simulatable_multisend(tx, inner_calls)
        context_note = build_context_note(tx, safe_address, simulate_as_bundle=simulate)
        utility_label = safe_utility_label(target)
        label = utility_label or (additional_info or "")
        return explain_batch_transaction(
            calls=inner_calls,
            chain_id=chain_id,
            protocol=protocol,
            label=label,
            from_address=safe_address,
            skip_simulation=not simulate,
            context_note=context_note,
            refine=True,
            # A utility label names the multisend contract, not the Safe, so the
            # report's Contract link must point at the target in that case.
            label_address=target if utility_label else safe_address,
            safe_tx_hash=safe_tx_hash,
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
        safe_tx_hash=safe_tx_hash,
    )


def _find_safe_tx_upgrades(tx: dict, target: str, hex_data: str, safe_address: str) -> list[ProxyUpgrade]:
    """Proxy upgrades a Safe tx performs: direct, per multisend call, or inside a timelock call.

    Multisend upgrades carry ``multiSend call N`` in their path, because the
    alert's target is the multisend utility, not the call that upgrades.
    """
    inner_calls = extract_inner_calls(tx, safe_address)
    if not inner_calls:
        return find_proxy_upgrades(hex_data, target)

    upgrades: list[ProxyUpgrade] = []
    for i, call in enumerate(inner_calls, start=1):
        for upgrade in find_proxy_upgrades(call["data"], call["target"]):
            via = f"multiSend call {i}" + (f" → {upgrade.via}" if upgrade.via else "")
            upgrades.append(replace(upgrade, via=via))
    return dedupe_upgrades(upgrades)


def _upgrade_alert_lines(
    tx: dict, target: str, hex_data: str, safe_address: str, chain_id: int, diffs: list[UpgradeDiffLink]
) -> list[str]:
    """Collect the alert lines and diff links for each detected Safe upgrade."""
    lines: list[str] = []
    for upgrade in _find_safe_tx_upgrades(tx, target, hex_data, safe_address):
        lines.extend(format_upgrade_lines(upgrade, target, chain_id, diffs=diffs))
    return lines


def _proposer_and_delegate(tx: dict) -> tuple[str, str]:
    """Return the lowercased (proposer, delegate) a Yearn multisig tx is matched on.

    ``proposedBy`` counts as a delegate only when ``proposedVia`` is ``DELEGATE``.
    For ``NESTED_OWNER`` it is an EOA acting through an owner Safe, which must
    not make the tx look expected when that owner Safe (``proposer``) is not.

    Any other route (``UNKNOWN``, a missing value, or one the service adds later)
    has unknown semantics, so neither address is trusted and a Yearn multisig tx
    escalates as an unexpected proposer.
    """
    route = tx.get("proposedVia")
    if route not in _KNOWN_PROPOSAL_ROUTES:
        return "", ""
    proposer = (tx.get("proposer") or "").lower()
    delegate = (tx.get("proposedBy") or "").lower() if route == "DELEGATE" else ""
    return proposer, delegate


def check_for_pending_transactions(
    safe_address: str, network_name: str, protocol: str, queued_txs: list[dict], complete: bool = True
) -> None:
    """Alert on a Safe's queued txs that have not been alerted on yet.

    Args:
        safe_address: The Safe's address.
        network_name: Safe network name from the watchlist.
        protocol: Protocol key that picks the alert rule and Telegram channel.
        queued_txs: The Safe's executable queued txs.
        complete: Whether ``queued_txs`` is the Safe's whole queue.
    """
    pending_transactions = get_pending_transactions(safe_address, network_name, queued_txs, complete)
    expected_proposers = YEARN_EXPECTED_PROPOSERS.get((network_name, safe_address.lower()), set())

    if pending_transactions:
        for tx in pending_transactions:
            nonce = int(tx["nonce"])
            # The Queue Service returns null for empty calldata and for a zero-address
            # destination, and lowercases addresses.
            hex_data = tx.get("data") or "0x"
            target_contract = Web3.to_checksum_address(tx.get("to") or ZERO_ADDRESS)

            if not should_alert(protocol, tx, target_contract, hex_data):
                logger.info("Skipping nonce %s on %s: outside the %s alert rule", nonce, safe_address, protocol)
                # The rule only reads the tx's own fields, so it would skip this tx again.
                mark_tx_handled(safe_address, network_name, tx)
                continue

            if not telegram_credentials_configured(protocol):
                # An undelivered alert stays eligible, so without this check every poll would
                # re-run the explainer and simulation and store another skipped alert row.
                logger.warning(
                    "No Telegram destination for %s; leaving nonce %s on %s queued until one is configured",
                    protocol,
                    nonce,
                    safe_address,
                )
                continue

            # Yearn multisigs (expected_proposers is non-empty) always get an
            # alert so there's a record of every queued tx. Expected-proposer
            # txs are sent silently (low importance); a tx proposed by an
            # address that is NOT one of our known bots is escalated to a loud
            # critical alert — it could mean a compromised proposer.
            is_yearn_multisig = bool(expected_proposers)
            unexpected_proposer = False
            tx_proposer, tx_delegate = _proposer_and_delegate(tx)
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
                        "Nonce %s on %s proposed by UNEXPECTED address (proposer=%s via=%s proposedBy=%s)",
                        nonce,
                        safe_address,
                        tx_proposer or "?",
                        tx.get("proposedVia") or "?",
                        tx.get("proposedBy") or "?",
                    )

            message = ""
            if unexpected_proposer:
                message += (
                    "⚠️🚨 *CRITICAL: UNEXPECTED PROPOSER* 🚨⚠️\n"
                    "This Yearn multisig tx was NOT proposed by a known Yearn bot/EOA!\n"
                    f"👤 Proposer: {tx_proposer or 'unknown'}\n"
                    f"👤 Proposed via: {escape_markdown(str(tx.get('proposedVia') or 'unknown'))}"
                    f" by {tx.get('proposedBy') or 'none'}\n\n"
                )
            message += (
                "🚨 QUEUED TX DETECTED 🚨\n"
                f"🅿️ Protocol: {escape_markdown(protocol)}\n"
                f"🔐 Safe Address: {safe_address}\n"
                f"🔗 Safe URL: {get_safe_url(safe_address, network_name)}\n"
                f"#️⃣ Nonce: {nonce}\n"
                f"📜 Target Contract Address: {target_contract}\n"
                f"📅 Submission Date: {tx.get('created') or 'unknown'}"
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

            chain_id = safe_network_to_chain_id(network_name)
            # Proxy upgrades, including ones nested in a timelock call or a
            # multisend (best-effort: an RPC failure must not block the alert).
            diffs: list[UpgradeDiffLink] = []
            upgrade_lines: list[str] = []
            try:
                upgrade_lines = _upgrade_alert_lines(tx, target_contract, hex_data, safe_address, chain_id, diffs)
            except Exception:
                diffs.clear()
                logger.warning("Upgrade detection failed for Safe tx nonce=%s", nonce, exc_info=True)

            # AI explanation (best-effort, non-blocking). Empty and short
            # payloads still reach the explainer — they are native transfers
            # or unknown selectors, not "nothing to explain".
            explanation = None
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
            except Exception:
                logger.debug("AI explanation failed for Safe tx nonce=%s", nonce, exc_info=True)

            urls = resolve_upgrade_diff_links(diffs, chain_id)
            message += "".join(f"\n{line}" for line in apply_upgrade_diff_links(upgrade_lines, urls))
            if explanation:
                try:
                    message += format_explanation_line(
                        explanation, upgrade_diff_links=format_upgrade_report_links(diffs, urls, chain_id)
                    )
                except Exception:
                    logger.debug("AI report formatting failed for Safe tx nonce=%s", nonce, exc_info=True)

            # Silent for routine Yearn multisig txs (expected proposer); loud
            # for unexpected proposers and for all non-Yearn protocol alerts.
            disable_notification = is_yearn_multisig and not unexpected_proposer
            if send_telegram_message(message, protocol, disable_notification):
                mark_tx_handled(safe_address, network_name, tx)
    else:
        logger.info("No new queued transactions for %s on %s", safe_address, network_name)


def _alert_unalerted_executions(check: NonceCheck, nonces: list[int]) -> bool:
    """Send one loud alert listing a Safe's nonces that executed without a queued-tx alert.

    Returns:
        Whether Telegram accepted it. ``False`` keeps the nonces for the next run.
    """
    network_name, safe_address = check.safe
    if not telegram_credentials_configured(check.protocol):
        logger.warning(
            "No Telegram destination for %s; leaving unalerted executions %s on %s for the next run",
            check.protocol,
            nonces,
            safe_address,
        )
        return False
    nonce_list = ", ".join(str(n) for n in nonces)
    message = (
        "🚨 SAFE TX EXECUTED WITHOUT A QUEUED ALERT 🚨\n"
        f"🅿️ Protocol: {escape_markdown(check.protocol)}\n"
        f"🔐 Safe Address: {safe_address}\n"
        f"🔗 Safe history: {SAFE_HISTORY_URL}{safe_address_network_prefix[network_name]}:{safe_address}\n"
        f"#️⃣ Nonce: {nonce_list}\n"
        "No queued-tx alert covered what executed at this nonce. It was executed without being "
        "posted to the Safe service, replaced an alerted tx at the same nonce, or was proposed and "
        "executed between two polls. Check what executed."
    )
    logger.warning("Safe %s on %s executed nonces %s without a queued alert", safe_address, network_name, nonce_list)
    return send_telegram_message(message, check.protocol, False)


def check_executed_nonces(safes: list[list[str]]) -> None:
    """Alert on nonces each Safe executed without a queued-tx alert covering the executed tx.

    Runs after the queue alerts, so a tx alerted this run counts as alerted. A Safe's
    first successful nonce read only records the nonce.

    Args:
        safes: Watchlist entries (``[protocol, network_name, safe_address, ...]``).

    Raises:
        SafeApiQuotaExhausted, SafeApiAuthError, SafeApiRequestError, SafeApiUnavailable:
            From the executed-hash lookup. Every Safe then keeps its progress for the next run.
    """
    protocols = {(safe[1], safe[2]): safe[0] for safe in safes}
    onchain = read_onchain_nonces(list(protocols))
    checks: list[NonceCheck] = []
    for safe, protocol in protocols.items():
        current_nonce = onchain.get(safe)
        if current_nonce is None:
            continue
        state = load_nonce_check_state(safe)
        if state is None:
            save_nonce_check_state(safe, NonceCheckState(current_nonce))
            continue
        if current_nonce < state.next_nonce:
            logger.error("Onchain nonce %s of %s on %s went backwards", current_nonce, safe[1], safe[0])
            continue
        handled = load_handled_txs(safe[1], safe[0])
        checks.append(plan_nonce_check(safe, protocol, state, current_nonce, handled))

    lookups = sorted({h for check in checks for hashes in check.alerted_hashes.values() for h in hashes if h})
    executed = fetch_executed_safe_tx_hashes(lookups) if lookups else set()

    for check in checks:
        unalerted, awaiting = check.resolve(executed)
        if unalerted and not _alert_unalerted_executions(check, unalerted):
            continue  # Keep the stored progress so the next run alerts again.
        save_nonce_check_state(check.safe, NonceCheckState(check.current_nonce, awaiting))


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
    except TypeError, ValueError:
        alerted_until = 0.0
    if now < alerted_until:
        return
    reset_at = now + exc.reset_seconds
    send_error_message(
        "🚨 Safe API quota exhausted\n"
        f"All {len(_api_keys)} Safe API keys have used up their quota; pending Safe transactions "
        "are NOT being monitored until the earliest reset "
        f"({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(reset_at))}).\n"
        "Add a key from another Safe developer account as the next SAFE_API_KEY_N in /etc/monitoring/.env.",
        ERROR_LABEL,
        disable_notification=False,
        source="safe_api_quota",
        alert_protocol=ALERT_PROTOCOL,
    )
    write_last_value_to_file(cache_filename, CACHE_KEY_QUOTA_ALERTED_UNTIL, int(reset_at))


def report_safe_failure(protocol: str, network_name: str, safe_address: str, exc: Exception) -> None:
    """Alert that one safe could not be checked this run, and carry on with the others.

    Args:
        protocol: Protocol key from the watchlist.
        network_name: Safe network name.
        safe_address: The safe that failed.
        exc: The exception raised while checking it.
    """
    logger.error("Safe check failed for %s %s on %s", protocol, safe_address, network_name, exc_info=exc)
    try:
        send_error_message(
            f"Safe monitor could not check {protocol} Safe {safe_address} on {network_name}; "
            f"its queued txs are not being alerted on this run.\n{type(exc).__name__}: {exc}",
            ERROR_LABEL,
            source="safe_check_failed",
            alert_protocol=ALERT_PROTOCOL,
        )
    except Exception:  # noqa: BLE001 - a failed notification must not skip the remaining safes
        logger.exception("Could not send the Safe check failure alert for %s on %s", safe_address, network_name)


def report_executed_check_failure(exc: Exception) -> None:
    """Alert that the executed-nonce check failed this run; its progress is kept for the next run."""
    logger.error("Safe executed-nonce check failed", exc_info=exc)
    try:
        send_error_message(
            "Safe monitor could not check which executed Safe txs were alerted on this run; "
            f"it retries next run.\n{type(exc).__name__}: {exc}",
            ERROR_LABEL,
            source="safe_check_failed",
            alert_protocol=ALERT_PROTOCOL,
        )
    except Exception:  # noqa: BLE001 - a failed notification must not crash the run
        logger.exception("Could not send the Safe executed-nonce check failure alert")


def main():
    load_known_exhausted_keys()
    check_llm_credits(ERROR_LABEL, alert_protocol=ALERT_PROTOCOL)
    # One request lists every watched Safe's queue. SafeApiAuthError and SafeApiUnavailable
    # blind every Safe the same way, so they crash the run and alert once.
    try:
        queues, complete = fetch_queued_transactions([(safe[1], safe[2]) for safe in ALL_SAFE_ADDRESSES])
    except SafeApiQuotaExhausted as exc:
        report_quota_exhausted(exc)
        return
    except SafeApiRequestError as exc:
        # The service rejected a Safe in the batch: query each one alone, so only the bad
        # one goes unchecked. This costs one request per Safe until the watchlist is fixed.
        logger.error("Batched Safe queue request rejected (%s); querying each Safe separately", exc)
        queues, complete = None, False

    last_api_call_time = 0.0
    request_counter = 0
    for safe in ALL_SAFE_ADDRESSES:
        protocol, network_name, safe_address = safe[0], safe[1], safe[2]
        logger.info("Running for %s on %s", protocol, network_name)
        try:
            if queues is None:
                last_api_call_time, request_counter = check_api_limit(last_api_call_time, request_counter)
                request_counter += 1
                safe_queues, safe_complete = fetch_queued_transactions([(network_name, safe_address)], max_retries=1)
                queued_txs = safe_queues[(network_name, safe_address)]
            else:
                queued_txs, safe_complete = queues[(network_name, safe_address)], complete
            check_for_pending_transactions(safe_address, network_name, protocol, queued_txs, safe_complete)
        except SafeApiQuotaExhausted as exc:
            # Every remaining safe would hit the same exhausted keys; stop the run.
            report_quota_exhausted(exc)
            return
        except SafeApiAuthError:
            # A rejected key fails every safe the same way: let the run crash and alert once.
            raise
        except Exception as exc:  # noqa: BLE001 - one safe's failure must not skip the rest
            report_safe_failure(protocol, network_name, safe_address, exc)

    try:
        check_executed_nonces(ALL_SAFE_ADDRESSES)
    except SafeApiQuotaExhausted as exc:
        report_quota_exhausted(exc)
    except SafeApiAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - progress is kept, so the next run rechecks
        report_executed_check_failure(exc)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Multi-safe script with per-safe routing; crashes go to the internal errors channel.
    run_with_alert(main, ERROR_LABEL, alert_protocol=ALERT_PROTOCOL)
