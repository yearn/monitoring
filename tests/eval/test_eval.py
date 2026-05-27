"""Pytest entry point for the eval harness — skipped unless explicitly enabled.

The eval makes live LLM/Etherscan/RPC calls (costs money, needs network), so it
must not run in the normal suite. Enable with:

    RUN_LLM_EVAL=1 python -m pytest tests/eval/test_eval.py -v

Each fixture becomes its own parametrized test so failures are isolated.
"""

import os

import pytest

from tests.eval.fixtures import CASES
from tests.eval.run_eval import check_case
from utils.llm.ai_explainer import explain_transaction

_ENABLED = os.getenv("RUN_LLM_EVAL", "").strip().lower() in ("1", "true", "yes", "on")


@pytest.mark.skipif(not _ENABLED, reason="set RUN_LLM_EVAL=1 to run the live LLM eval (costs money)")
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_eval_case(case) -> None:
    explanation = explain_transaction(
        target=case.target,
        calldata=case.calldata,
        chain_id=case.chain_id,
        protocol=case.protocol,
        label=case.label,
        description=case.description,
    )
    failures = check_case(case, explanation)
    assert not failures, "; ".join(failures)
