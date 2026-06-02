"""Run the AI-explainer eval harness against the live LLM provider.

This makes real LLM + Etherscan + RPC calls and therefore costs money, so it is
NOT part of the default test suite. Run it manually after prompt/pipeline
changes:

    python -m tests.eval.run_eval

Requires the usual env (LLM_API_KEY, ETHERSCAN_TOKEN, PROVIDER_URL_*); load your
.env first. Exits non-zero if any case fails, so it can gate a manual release.
"""

import re
import sys

from tests.eval.fixtures import CASES, GLOBAL_CHECKS, EvalCase
from utils.llm.ai_explainer import Explanation, explain_transaction

_RISK_RE = re.compile(r"\b(LOW|MEDIUM|HIGH|CRITICAL)\b")


def _extract_risk(summary: str) -> str | None:
    """The risk tag is the last LOW/MEDIUM/HIGH/CRITICAL token in the summary."""
    tags = _RISK_RE.findall(summary.upper())
    return tags[-1] if tags else None


def check_case(case: EvalCase, explanation: Explanation | None) -> list[str]:
    """Return a list of assertion failures for one case ([] means pass)."""
    if explanation is None or not explanation.summary:
        return ["no explanation returned"]

    failures: list[str] = []
    text = f"{explanation.summary}\n{explanation.detail}".lower()

    risk = _extract_risk(explanation.summary)
    if case.expected_risk and risk not in case.expected_risk:
        failures.append(f"risk={risk} not in {case.expected_risk}")

    for needle in case.must_include:
        if needle.lower() not in text:
            failures.append(f"missing required substring {needle!r}")

    for group in case.must_include_any:
        if not any(opt.lower() in text for opt in group):
            failures.append(f"none of {group} present")

    for needle in case.must_not_include:
        if needle.lower() in text:
            failures.append(f"forbidden substring present: {needle!r}")

    for check in GLOBAL_CHECKS:
        err = check(text)
        if err:
            failures.append(err)

    return failures


def run() -> int:
    """Run all cases, print a report, and return an exit code (0 = all passed)."""
    passed = 0
    for case in CASES:
        explanation = explain_transaction(
            target=case.target,
            calldata=case.calldata,
            chain_id=case.chain_id,
            protocol=case.protocol,
            label=case.label,
            description=case.description,
        )
        failures = check_case(case, explanation)
        if failures:
            print(f"FAIL  {case.name}")
            for f in failures:
                print(f"        - {f}")
            if explanation is not None:
                print(f"        summary: {explanation.summary}")
        else:
            passed += 1
            risk = _extract_risk(explanation.summary) if explanation else "?"
            print(f"PASS  {case.name}  [{risk}]")

    total = len(CASES)
    print(f"\n{passed}/{total} cases passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    sys.exit(run())
