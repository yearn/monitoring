"""AI-powered transaction explainer.

Combines Tenderly simulation results with decoded calldata and sends
them to an LLM to produce human-readable explanations for governance
transactions (timelocks and Safe multisigs).
"""

from dataclasses import dataclass

from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.llm import get_llm_provider
from utils.llm.base import LLMError
from utils.logging import get_logger
from utils.paste import upload_to_paste
from utils.proxy import build_diff_url, detect_proxy_upgrade, get_current_implementation
from utils.source_context import SourceContext, format_source_context, get_source_context
from utils.telegram import escape_markdown
from utils.tenderly.simulation import SimulationResult, simulate_transaction

logger = get_logger("utils.llm.ai_explainer")

SYSTEM_PROMPT = """You are a DeFi risk analyst explaining governance transactions to a monitoring team.
Given the decoded calldata and simulation results, provide two sections:

TLDR: A short summary in 1-3 sentences. Focus on what the transaction does and any risk implications.

DETAIL: A thorough analysis covering:
- What each call does and why
- Parameter values and their significance
- Asset/token flow changes
- State changes and their impact
- Risk assessment (LOW/MEDIUM/HIGH/CRITICAL)
- Any concerns or notable observations

Critical rules for parameter interpretation:
- Do NOT assume the semantic meaning of a parameter from its function name. DeFi protocols
  use inverted or non-standard conventions. For example, a "maxSlippage" value may represent
  a minimum-output ratio (where 0.99e18 means tight 1% protection), not a maximum tolerated
  deviation. A "fee" may be scaled to 1e4, 1e6, or 1e18.
- Whenever a Contract Source Context section is provided below, trust the natspec comments
  there over your prior assumptions about the function name.
- If source context is NOT provided and the unit/meaning is ambiguous, say so explicitly
  ("the unit cannot be confirmed without source context") rather than guessing. Quote the
  raw value and its 1e18-normalized form.
- Never assign HIGH/CRITICAL risk on the basis of a guessed unit interpretation."""

FORMAT_REMINDER = """
Format your response exactly as:
TLDR: <your short summary>

DETAIL:
<your detailed analysis>"""


@dataclass(frozen=True)
class Explanation:
    """AI-generated transaction explanation with short and detailed versions."""

    summary: str
    detail: str


def _collect_source_contexts(
    targets_and_calls: list[tuple[str, DecodedCall]],
    chain_id: int,
) -> list[SourceContext]:
    """Fetch source context for each (target, decoded_call) pair, best-effort.

    Deduplicates by (target, function_name). Silent on failure so a missing
    Etherscan key or unverified contract never blocks an explanation.
    """
    contexts: list[SourceContext] = []
    seen: set[tuple[str, str]] = set()
    for target, decoded in targets_and_calls:
        if not target or not decoded.function_name:
            continue
        key = (target.lower(), decoded.function_name)
        if key in seen:
            continue
        seen.add(key)
        try:
            ctx = get_source_context(chain_id, target, decoded.function_name)
        except Exception as e:  # noqa: BLE001
            logger.info("Source context fetch failed for %s.%s: %s", target, decoded.function_name, e)
            continue
        if ctx:
            contexts.append(ctx)
    return contexts


def _get_proxy_upgrade_info(calldata: str, target: str, chain_id: int) -> str:
    """Detect proxy upgrade and return context string for the LLM prompt."""
    new_impl = detect_proxy_upgrade(calldata)
    if not new_impl:
        return ""

    old_impl = get_current_implementation(target, chain_id)
    if old_impl:
        info = (
            f"This is a PROXY UPGRADE on {target}.\nCurrent implementation: {old_impl}\nNew implementation: {new_impl}"
        )
        diff_url = build_diff_url(old_impl, new_impl, chain_id)
        if diff_url:
            info += f"\nDiff: {diff_url}"
        return info

    return f"This is a PROXY UPGRADE on {target}.\nNew implementation: {new_impl}"


def _format_decoded_calls(calls: list[DecodedCall]) -> str:
    """Format decoded calls into a readable string for the LLM prompt."""
    parts: list[str] = []
    for i, call in enumerate(calls):
        lines = [f"Call {i + 1}: {call.signature}"]
        for type_str, value in call.params:
            lines.append(f"  {type_str}: {value}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _format_simulation_context(sim: SimulationResult) -> str:
    """Format simulation results into a readable string for the LLM prompt."""
    parts: list[str] = []

    parts.append(f"Simulation: {'SUCCESS' if sim.success else 'FAILED'}")
    if sim.error_message:
        parts.append(f"Error: {sim.error_message}")
    parts.append(f"Gas used: {sim.gas_used:,}")

    if sim.asset_changes:
        parts.append("\nToken transfers:")
        for change in sim.asset_changes:
            amount = change.amount
            parts.append(f"  {amount} {change.token_symbol} from {change.from_address} to {change.to_address}")

    if sim.state_changes:
        # Show up to 10 most relevant state changes to avoid prompt bloat
        shown = sim.state_changes[:10]
        parts.append(f"\nState changes ({len(sim.state_changes)} total, showing {len(shown)}):")
        for sc in shown:
            parts.append(f"  Contract {sc.contract_address}: {sc.key}")
            parts.append(f"    {sc.original} -> {sc.dirty}")

    if sim.logs:
        shown_logs = sim.logs[:10]
        parts.append(f"\nEvents emitted ({len(sim.logs)} total, showing {len(shown_logs)}):")
        for log_entry in shown_logs:
            name = log_entry.get("name", "Unknown")
            inputs = log_entry.get("inputs") or []
            input_strs = [f"{inp.get('soltype', {}).get('name', '?')}={inp.get('value', '?')}" for inp in inputs]
            parts.append(f"  {name}({', '.join(input_strs)})")

    return "\n".join(parts)


def _build_prompt(
    target: str,
    value: int,
    decoded_calls: list[DecodedCall],
    simulation: SimulationResult | None,
    protocol: str = "",
    label: str = "",
    proxy_upgrade_info: str = "",
    source_contexts: list[SourceContext] | None = None,
    context_note: str = "",
) -> str:
    """Build the full prompt for the LLM."""
    parts: list[str] = [SYSTEM_PROMPT, ""]

    if protocol:
        parts.append(f"Protocol: {protocol}")
    if label:
        parts.append(f"Contract: {label}")
    parts.append(f"Target: {target}")
    if value > 0:
        parts.append(f"ETH Value: {value / 1e18:.6f} ETH")

    if context_note:
        parts.append(f"\n--- Execution Context ---\n{context_note}")

    parts.append(f"\n--- Decoded Calldata ---\n{_format_decoded_calls(decoded_calls)}")

    if source_contexts:
        rendered = "\n\n".join(format_source_context(ctx) for ctx in source_contexts)
        parts.append(f"\n--- Contract Source Context ---\n{rendered}")

    if proxy_upgrade_info:
        parts.append(f"\n--- Proxy Upgrade ---\n{proxy_upgrade_info}")

    if simulation:
        parts.append(f"\n--- Simulation Results ---\n{_format_simulation_context(simulation)}")

    parts.append(FORMAT_REMINDER)

    return "\n".join(parts)


def _find_marker(text: str, keyword: str) -> tuple[int, int]:
    """Find a section marker like 'TLDR:' or '### DETAIL' and return (start_of_marker, start_of_content).

    Handles variations: 'KEYWORD:', '## KEYWORD', '**KEYWORD**', '**KEYWORD:**', etc.
    Returns (-1, -1) if not found.
    """
    import re

    heading = r"#{1,4}"  # fmt: skip
    pattern = rf"(?:^|\n)\s*(?:{heading}\s+)?(?:\*{{2}})?{keyword}(?:\*{{2}})?[:\s]*"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.start(), match.end()
    return -1, -1


def _parse_explanation(raw: str) -> Explanation:
    """Parse LLM response into summary and detail sections.

    Expected format:
        TLDR: <short summary>

        DETAIL:
        <detailed analysis>

    Falls back gracefully if the LLM doesn't follow the format exactly.
    Handles markdown-style headers like '### DETAIL' or '**TLDR:**'.
    """
    tldr_start, tldr_content = _find_marker(raw, "TLDR")
    detail_start, detail_content = _find_marker(raw, "DETAIL")

    if tldr_start != -1 and detail_start != -1:
        summary = raw[tldr_content:detail_start].strip()
        detail = raw[detail_content:].strip()
        return Explanation(summary=summary, detail=detail)

    if tldr_start != -1:
        summary = raw[tldr_content:].strip()
        return Explanation(summary=summary, detail="")

    if detail_start != -1:
        summary = raw[:detail_start].strip()
        detail = raw[detail_content:].strip()
        return Explanation(summary=summary, detail=detail)

    # No markers — use full response as summary (backward compatible)
    return Explanation(summary=raw.strip(), detail="")


def explain_transaction(
    target: str,
    calldata: str,
    chain_id: int,
    value: int = 0,
    protocol: str = "",
    label: str = "",
    from_address: str = "0x0000000000000000000000000000000000000000",
    skip_simulation: bool = False,
    context_note: str = "",
) -> Explanation | None:
    """Generate an AI explanation for a governance transaction.

    Decodes calldata, simulates via Tenderly, and sends context to the LLM.
    Returns None if explanation cannot be generated (missing API keys, errors, etc.).

    Args:
        target: Target contract address.
        calldata: Hex-encoded calldata (with 0x prefix).
        chain_id: Chain ID (e.g. 1 for mainnet).
        value: ETH value in wei.
        protocol: Protocol name for context (e.g. "AAVE").
        label: Human-readable label for the contract.
        from_address: Sender address for simulation.
        skip_simulation: If True, do not call Tenderly. Use when the caller knows
            our plain-CALL simulator can't model the real execution (e.g. Safe
            DELEGATECALL into MultiSendCallOnly).
        context_note: Optional preamble injected into the prompt to give the LLM
            context that isn't in the calldata (e.g. "this is delegated from
            a Safe; msg.sender of inner calls is the Safe itself").

    Returns:
        Explanation with summary and detail, or None on failure.
    """
    if not calldata or len(calldata) < 10:
        return None

    decoded = decode_calldata(calldata)
    if not decoded:
        logger.info("Could not decode calldata for %s, skipping AI explanation", target)
        return None

    decoded_calls = [decoded]
    proxy_upgrade_info = _get_proxy_upgrade_info(calldata, target, chain_id)
    source_contexts = _collect_source_contexts([(target, decoded)], chain_id)

    simulation: SimulationResult | None = None
    if not skip_simulation:
        simulation = simulate_transaction(
            target=target,
            calldata=calldata,
            chain_id=chain_id,
            value=value,
            from_address=from_address,
        )
        if simulation:
            logger.info("Simulation completed: success=%s gas=%s", simulation.success, simulation.gas_used)
        else:
            logger.info("Simulation unavailable, proceeding with decoded calldata only")

    prompt = _build_prompt(
        target=target,
        value=value,
        decoded_calls=decoded_calls,
        simulation=simulation,
        protocol=protocol,
        label=label,
        proxy_upgrade_info=proxy_upgrade_info,
        source_contexts=source_contexts,
        context_note=context_note,
    )
    logger.info("Full AI context for %s:\n%s", target, prompt)

    try:
        provider = get_llm_provider()
        raw = provider.complete(prompt)
        explanation = _parse_explanation(raw)
        logger.info("AI summary using %s:\n%s", provider.model_name, explanation.summary)
        if explanation.detail:
            logger.info("AI detail:\n%s", explanation.detail)
        return explanation
    except LLMError as e:
        logger.error("Failed to generate AI explanation: %s", e)
        return None


def explain_batch_transaction(
    calls: list[dict[str, str]],
    chain_id: int,
    protocol: str = "",
    label: str = "",
    from_address: str = "0x0000000000000000000000000000000000000000",
    skip_simulation: bool = False,
    context_note: str = "",
) -> Explanation | None:
    """Generate an AI explanation for a batch/multicall governance transaction.

    Args:
        calls: List of dicts with keys: target, data, value.
        chain_id: Chain ID.
        protocol: Protocol name for context.
        label: Human-readable label for the timelock/safe.
        from_address: Sender address for simulations.
        skip_simulation: If True, do not call Tenderly. Useful for Safe multisend
            batches where independent per-call simulation would break state-
            dependent flows (approve+transferFrom, swapOwner+swapOwner, etc).
        context_note: Optional preamble describing the execution context (e.g.
            DELEGATECALL semantics) that the LLM can't infer from calldata alone.

    Returns:
        Explanation with summary and detail, or None on failure.
    """
    if not calls:
        return None

    decoded_calls: list[DecodedCall] = []
    decoded_with_target: list[tuple[str, DecodedCall]] = []
    simulations: list[SimulationResult | None] = []

    for call in calls:
        target = call.get("target", "")
        data = call.get("data", "0x")
        value = int(call.get("value", "0"))

        decoded = decode_calldata(data)
        if decoded:
            decoded_calls.append(decoded)
            decoded_with_target.append((target, decoded))

        if not skip_simulation:
            sim = simulate_transaction(
                target=target,
                calldata=data,
                chain_id=chain_id,
                value=value,
                from_address=from_address,
            )
            simulations.append(sim)

    if not decoded_calls:
        return None

    simulation = next((s for s in simulations if s is not None), None)

    upgrade_parts: list[str] = []
    for call in calls:
        info = _get_proxy_upgrade_info(call.get("data", "0x"), call.get("target", ""), chain_id)
        if info:
            upgrade_parts.append(info)
    proxy_upgrade_info = "\n".join(upgrade_parts)

    source_contexts = _collect_source_contexts(decoded_with_target, chain_id)

    targets = ", ".join(c.get("target", "?") for c in calls)
    total_value = sum(int(c.get("value", "0")) for c in calls)

    prompt = _build_prompt(
        target=targets,
        value=total_value,
        decoded_calls=decoded_calls,
        simulation=simulation,
        protocol=protocol,
        label=label,
        proxy_upgrade_info=proxy_upgrade_info,
        source_contexts=source_contexts,
        context_note=context_note,
    )
    logger.info("Full AI context for batch (%s calls):\n%s", len(calls), prompt)

    try:
        provider = get_llm_provider()
        raw = provider.complete(prompt)
        explanation = _parse_explanation(raw)
        logger.info("Batch AI summary using %s:\n%s", provider.model_name, explanation.summary)
        if explanation.detail:
            logger.info("Batch AI detail:\n%s", explanation.detail)
        return explanation
    except LLMError as e:
        logger.error("Failed to generate batch AI explanation: %s", e)
        return None


def format_explanation_line(explanation: Explanation) -> str:
    """Format the AI explanation for inclusion in a Telegram alert message.

    Uses the short summary for the Telegram message. The detailed analysis
    is uploaded to a paste service (dpaste.org) for easy access.
    """
    line = f"\n🤖 *AI Summary:*\n{escape_markdown(explanation.summary)}"
    if explanation.detail:
        paste_url = upload_to_paste(explanation.detail, title="AI Transaction Analysis")
        if paste_url:
            line += f"\n[Full details]({paste_url})"
    return line
