"""Golden fixtures for the AI-explainer eval harness.

Each case is a real mainnet transaction whose expected risk band and key facts
we can assert loosely. LLM output is non-deterministic, so assertions are
tolerant: a risk tag within an acceptable set, plus a few substrings that must
(or must-not) appear. The goal is to catch prompt/pipeline regressions, not to
pin exact wording.

Add a case whenever a prompt change fixes a specific failure mode — that's the
regression guard.
"""

from dataclasses import dataclass, field
from typing import Callable

from eth_abi import encode

# Well-known verified mainnet addresses used across fixtures.
COMPOUND_COMPTROLLER = "0x3d9819210A31b4961b30EF54bE2aeD79B9c9Cd3B"  # Unitroller proxy
USDC_PROXY = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # FiatTokenProxy
USDC_IMPL_V2_1 = "0xa2327a938Febf5FEC13baCFb16Ae10EcBc4cbDCF"  # older verified impl


def _close_factor_call(fraction_1e18: int) -> str:
    """Compound Comptroller._setCloseFactor(uint256) calldata."""
    return "0x317b0b77" + encode(["uint256"], [fraction_1e18]).hex()


def _upgrade_to_call(new_impl: str) -> str:
    """upgradeTo(address) calldata."""
    return "0x3659cfe6" + encode(["address"], [new_impl]).hex()


@dataclass(frozen=True)
class EvalCase:
    """One eval scenario plus tolerant assertions over the explanation."""

    name: str
    target: str
    calldata: str
    chain_id: int = 1
    protocol: str = ""
    label: str = ""
    description: str = ""
    # Risk tag (parsed from the summary) must be one of these.
    expected_risk: tuple[str, ...] = ()
    # Every substring must appear somewhere in summary+detail (case-insensitive).
    must_include: tuple[str, ...] = ()
    # For each group, at least one substring must appear.
    must_include_any: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    # None of these may appear (case-insensitive).
    must_not_include: tuple[str, ...] = ()


CASES: list[EvalCase] = [
    EvalCase(
        name="compound_set_close_factor_routine",
        target=COMPOUND_COMPTROLLER,
        calldata=_close_factor_call(int(0.5e18)),
        protocol="COMPOUND",
        label="Comptroller",
        expected_risk=("LOW", "MEDIUM"),
        must_include_any=(("close factor", "closefactor", "closeFactorMantissa", "liquidat"),),
    ),
    EvalCase(
        name="usdc_proxy_upgrade",
        target=USDC_PROXY,
        calldata=_upgrade_to_call(USDC_IMPL_V2_1),
        protocol="USDC",
        label="USDC FiatTokenProxy",
        expected_risk=("HIGH", "CRITICAL"),
        must_include_any=(("upgrade", "implementation"),),
    ),
    EvalCase(
        name="intent_mismatch_close_factor",
        target=COMPOUND_COMPTROLLER,
        calldata=_close_factor_call(int(0.9e18)),
        protocol="COMPOUND",
        label="Comptroller",
        description="Routine documentation update. No parameter or risk changes.",
        # A misleading description must NOT downgrade a real parameter change.
        expected_risk=("MEDIUM", "HIGH", "CRITICAL"),
        must_include_any=(("contradict", "mismatch", "red flag", "stated intent", "does not match"),),
    ),
    EvalCase(
        name="intent_honest_close_factor",
        target=COMPOUND_COMPTROLLER,
        calldata=_close_factor_call(int(0.9e18)),
        protocol="COMPOUND",
        label="Comptroller",
        description="Raise the close factor to 0.9 to allow larger liquidations.",
        # Honest description should describe the change without false alarms.
        expected_risk=("LOW", "MEDIUM", "HIGH"),
        must_include_any=(("close factor", "closefactor", "liquidat"),),
    ),
]


# Extra programmatic invariants applied to every case, beyond the per-case
# substrings. Each returns an error string or "" on success.
GLOBAL_CHECKS: list[Callable[[str], str]] = []
