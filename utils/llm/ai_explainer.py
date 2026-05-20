"""AI-powered transaction explainer.

Combines Tenderly simulation results with decoded calldata and sends
them to an LLM to produce human-readable explanations for governance
transactions (timelocks and Safe multisigs).
"""

from dataclasses import dataclass

from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.impl_diff import diff_implementations, format_impl_diff
from utils.llm import get_llm_provider
from utils.llm.base import LLMError
from utils.logging import get_logger
from utils.on_chain_state import StateRead, format_state_reads, read_before_state
from utils.paste import upload_to_paste
from utils.proxy import build_diff_url, detect_proxy_upgrade, get_current_implementation
from utils.source_context import SourceContext, format_source_context, get_source_context
from utils.telegram import escape_markdown
from utils.tenderly.simulation import SimulationResult, simulate_transaction

logger = get_logger("utils.llm.ai_explainer")

SYSTEM_PROMPT = """You are a DeFi risk analyst writing alerts for a monitoring team. Output two sections.

TLDR: ≤25 words. Start with a verb describing the effect. Do NOT open with
"This transaction", "The proposal", or similar — the reader already knows
what kind of tx this is. End with a risk tag in caps: LOW / MEDIUM / HIGH / CRITICAL.

Good example: "Lowers swap fee 30→25 bps on USDC/USDT pool. Marginal LP revenue cut. LOW."
Bad example:  "This governance transaction adjusts the swap fee parameter on the USDC/USDT pool from 30 basis points to 25 basis points, which slightly reduces revenue for liquidity providers. Risk is LOW."

DETAIL: thorough analysis covering:
- What each call does and why
- Parameter values and their significance (use Current State section if present to compute deltas)
- Asset/token flow changes
- State changes and their impact
- Risk assessment with explicit reasoning
- Any concerns or notable observations

Critical rules for parameter interpretation:
- Do NOT assume the semantic meaning of a parameter from its function name. DeFi protocols
  use inverted or non-standard conventions (a "maxSlippage" may be a min-output ratio;
  a "fee" may be scaled to 1e4, 1e6, or 1e18).
- When a Contract Source Context section is provided, trust the natspec over your prior
  assumptions about the function name.
- When a Current State section is provided, quote concrete before→after deltas.
- If a unit is ambiguous and no source context resolves it, say so explicitly rather than
  guessing. Quote the raw value plus its 1e18-normalized form.
- Never assign HIGH/CRITICAL risk on the basis of a guessed unit interpretation."""

FORMAT_REMINDER = """
Format your response exactly as:
TLDR: <your short summary>

DETAIL:
<your detailed analysis>"""

REFINE_TASK = """--- Critique Task ---
Check the draft above against this checklist. Each item is a yes/no question:

1. Does the TLDR start with a verb (NOT "This transaction" / "The proposal" /
   "The transaction" / "This governance")?
2. Is the TLDR ≤25 words?
3. Does the TLDR end with a risk tag in CAPS (LOW / MEDIUM / HIGH / CRITICAL)?
4. Are all numeric magnitudes/units in the draft supported by either the
   Contract Source Context section or the Current State section above? Or
   does the draft explicitly say the unit cannot be confirmed?
5. If a Current State section showed before→after values, does the DETAIL
   quote the concrete delta (e.g., "10× relaxation", "20% reduction")?
6. Does the risk verdict match the magnitude of change shown in Current State?
   (A 10× change to a critical parameter is rarely LOW; a no-op is rarely HIGH.)

Hard rules for the revision (if you choose to revise):
- Do NOT introduce a unit/scale assumption that wasn't in the draft. If the draft
  says "raw values 1e15–8e15", do NOT rewrite as "<0.008 ETH". You don't know the
  decimals unless the source context or state reads tell you.
- Do NOT escalate a justifiable LOW out of caution.
- Do NOT remove an explicit hedge ("unit cannot be confirmed", "without source
  context", etc.).
- Do NOT polish for style alone. Only edit if there's a concrete, specific issue
  from items 1-6.

If every check is satisfied AND no hard rule would be violated by the draft as-is,
output exactly:
PASS

Otherwise output the revised explanation in the same format:
TLDR: <revised>

DETAIL:
<revised>"""


@dataclass(frozen=True)
class Explanation:
    """AI-generated transaction explanation with short and detailed versions."""

    summary: str
    detail: str


def _collect_state_reads(
    targets_and_calls: list[tuple[str, DecodedCall]],
    chain_id: int,
) -> list[tuple[str, list[StateRead]]]:
    """Best-effort: read current on-chain values for state vars each call will write.

    Returns a list of (target, reads) tuples in the same order as the input. Empty
    reads are still returned (so callers can show per-call ordering); the formatter
    skips them.
    """
    out: list[tuple[str, list[StateRead]]] = []
    seen: set[tuple[str, str]] = set()
    for target, decoded in targets_and_calls:
        if not target:
            continue
        key = (target.lower(), decoded.function_name)
        if key in seen:
            continue
        seen.add(key)
        try:
            reads = read_before_state(chain_id, target, decoded)
        except Exception as e:  # noqa: BLE001
            logger.info("State read failed for %s.%s: %s", target, decoded.function_name, e)
            reads = []
        if reads:
            out.append((target, reads))
    return out


def _format_batch_param_constants(decoded_calls: list[DecodedCall]) -> str:
    """For batch txs, surface arg positions that hold the same value across all calls.

    Helps the LLM notice things like "all 4 setCoverageCap calls share market_id X".
    Returns "" if there's nothing notable (single call, or no position is uniform).
    """
    if len(decoded_calls) < 2:
        return ""

    # Only meaningful when all calls share the same signature
    sigs = {c.signature for c in decoded_calls}
    if len(sigs) != 1:
        return ""

    first = decoded_calls[0]
    if not first.params:
        return ""

    notes: list[str] = []
    for i, (type_str, value) in enumerate(first.params):
        if all(c.params[i][1] == value for c in decoded_calls[1:]):
            notes.append(f"  arg[{i}] ({type_str}) is identical across all {len(decoded_calls)} calls: {value!r}")
    return "\n".join(notes)


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
    """Detect proxy upgrade, fetch impl diff, and return context string for the prompt."""
    upgrade = detect_proxy_upgrade(calldata, target)
    if not upgrade:
        return ""

    proxy = upgrade.proxy_address
    new_impl = upgrade.new_implementation
    old_impl = get_current_implementation(proxy, chain_id)
    if not old_impl:
        return f"This is a PROXY UPGRADE on {proxy}.\nNew implementation: {new_impl}"

    info = f"This is a PROXY UPGRADE on {proxy}.\nCurrent implementation: {old_impl}\nNew implementation: {new_impl}"
    diff_url = build_diff_url(old_impl, new_impl, chain_id)
    if diff_url:
        info += f"\nDiff: {diff_url}"

    try:
        impl_diff = diff_implementations(old_impl, new_impl, chain_id)
        if impl_diff:
            info += "\n\n" + format_impl_diff(impl_diff)
    except Exception as e:  # noqa: BLE001 - best-effort enrichment
        logger.info("Impl diff failed for %s → %s: %s", old_impl, new_impl, e)

    return info


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
    state_reads: list[tuple[str, list[StateRead]]] | None = None,
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

    constants_note = _format_batch_param_constants(decoded_calls)
    if constants_note:
        parts.append(f"\n--- Shared Across Batch ---\n{constants_note}")

    if source_contexts:
        rendered = "\n\n".join(format_source_context(ctx) for ctx in source_contexts)
        parts.append(f"\n--- Contract Source Context ---\n{rendered}")

    if state_reads:
        rendered_state: list[str] = []
        for tgt, reads in state_reads:
            rendered_state.append(f"On {tgt}:")
            rendered_state.append(format_state_reads(reads))
        parts.append("\n--- Current State (before this call) ---\n" + "\n".join(rendered_state))

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


def _refine_explanation(original_prompt: str, draft: Explanation, provider) -> Explanation:
    """Self-critique then revise. Returns the draft unchanged on PASS or any error."""
    refine_prompt = (
        f"{original_prompt}\n\n"
        f"--- Your Previous Draft ---\n"
        f"TLDR: {draft.summary}\n\n"
        f"DETAIL:\n{draft.detail}\n\n"
        f"{REFINE_TASK}"
    )

    try:
        raw = provider.complete(refine_prompt)
    except LLMError as e:
        logger.warning("Refine pass failed (%s); keeping draft", e)
        return draft

    if not raw or not raw.strip():
        return draft

    if raw.strip().upper().startswith("PASS"):
        logger.info("Refine pass: PASS (no changes)")
        return draft

    revised = _parse_explanation(raw)
    if not revised.summary:
        logger.warning("Refine pass returned empty summary; keeping draft")
        return draft

    logger.info("Refine pass produced a revision (TLDR %d→%d chars)", len(draft.summary), len(revised.summary))
    return revised


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
    refine: bool = False,
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
        refine: If True, runs a second LLM call that critiques the draft against
            a checklist and revises only if it finds concrete issues. ~2× cost.

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
    state_reads = _collect_state_reads([(target, decoded)], chain_id)

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
        state_reads=state_reads,
    )
    logger.info("Full AI context for %s:\n%s", target, prompt)

    try:
        provider = get_llm_provider()
        raw = provider.complete(prompt)
        explanation = _parse_explanation(raw)
        if refine:
            explanation = _refine_explanation(prompt, explanation, provider)
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
    refine: bool = False,
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
        refine: If True, runs a second LLM call that critiques the draft against
            a checklist and revises only if it finds concrete issues. ~2× cost.

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
    state_reads = _collect_state_reads(decoded_with_target, chain_id)

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
        state_reads=state_reads,
    )
    logger.info("Full AI context for batch (%s calls):\n%s", len(calls), prompt)

    try:
        provider = get_llm_provider()
        raw = provider.complete(prompt)
        explanation = _parse_explanation(raw)
        if refine:
            explanation = _refine_explanation(prompt, explanation, provider)
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
