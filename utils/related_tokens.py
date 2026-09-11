"""Resolve the ERC20 tokens a contract is denominated in.

Governance calls routinely carry an amount with no address argument to hang
decimals off — ``setEpochEmissions(uint256 epoch, uint256 emissions)`` being the
case that prompted this. Without a token the explainer can only report raw
units and hedge ("token decimals are unconfirmed"), which is accurate but noisy.

Most such contracts do expose their token: a zero-arg view getter returning an
address (``jane()``, ``token()``, ``asset()``, ``rewardToken()``). This module
reads the target's verified ABI, calls every such getter, and keeps the results
that are actually ERC20 — so ``owner()`` filters itself out without a name
blocklist, and no per-protocol configuration is needed.

Best-effort by design: any failure yields an empty list and the caller proceeds
exactly as it did before.
"""

from dataclasses import dataclass

from eth_utils import to_checksum_address

from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.logger import get_logger
from utils.source_context import fetch_abi_entries
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.related_tokens")

# Upper bound on getters we call per target. A large ABI can have dozens of
# zero-arg address getters (registries, role holders); the token is invariably
# among the first few, and this keeps one alert from fanning out into an
# unbounded number of eth_calls.
MAX_GETTER_CALLS = 8

# Marker used when the target is itself the token, so callers can render the
# provenance ("self" vs "jane()") without a separate flag.
SELF_GETTER = "self"

# Per-process cache: (chain_id, target_lower) -> resolved tokens.
_cache: dict[tuple[int, str], list["RelatedToken"]] = {}


@dataclass(frozen=True)
class RelatedToken:
    """An ERC20 associated with a contract, and how it was discovered."""

    getter: str
    address: str
    symbol: str
    decimals: int

    @property
    def source(self) -> str:
        """Human-readable provenance, e.g. ``jane()`` or ``the target itself``."""
        return "the target itself" if self.getter == SELF_GETTER else f"{self.getter}()"


def _address_getter_names(abi_entries: list[dict]) -> list[str]:
    """Names of zero-arg view/pure functions returning exactly one address."""
    names: list[str] = []
    for entry in abi_entries:
        if entry.get("type") != "function" or entry.get("stateMutability") not in ("view", "pure"):
            continue
        if entry.get("inputs"):
            continue
        outputs = entry.get("outputs") or []
        if len(outputs) == 1 and outputs[0].get("type") == "address" and entry.get("name"):
            names.append(str(entry["name"]))
    return names


def _call_address_getters(chain_id: int, target: str, names: list[str]) -> dict[str, str]:
    """Call each getter and return ``{name: returned_address}`` for the ones that succeed.

    All getters go out in a single batch request; a batch failure falls back to
    returning nothing rather than retrying call-by-call, since this is
    enrichment and the alert must not stall on it.
    """
    if not names:
        return {}
    abi = [
        {
            "name": name,
            "type": "function",
            "stateMutability": "view",
            "inputs": [],
            "outputs": [{"name": "", "type": "address"}],
        }
        for name in names
    ]
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    contract = client.get_contract(to_checksum_address(target), abi)
    with client.batch_requests() as batch:
        for name in names:
            batch.add(contract.functions[name]())
        results = client.execute_batch(batch)
    return {name: str(value) for name, value in zip(names, results) if isinstance(value, str)}


def resolve_related_tokens(chain_id: int, target: str) -> list[RelatedToken]:
    """ERC20 tokens associated with ``target``, discovered from its own getters.

    Args:
        chain_id: Chain the contract lives on.
        target: Contract address to inspect.

    Returns:
        Resolved tokens (empty when the contract is unverified, exposes no
        token getter, or any lookup fails). The target being itself an ERC20
        is reported first, with ``getter == SELF_GETTER``.
    """
    if not target:
        return []
    cache_key = (chain_id, target.lower())
    if cache_key in _cache:
        return _cache[cache_key]

    tokens: list[RelatedToken] = []
    try:
        own_meta = fetch_erc20_metadata(chain_id, target)
        if own_meta:
            tokens.append(
                RelatedToken(
                    getter=SELF_GETTER,
                    address=to_checksum_address(target),
                    symbol=own_meta.symbol,
                    decimals=own_meta.decimals,
                )
            )

        abi_entries = fetch_abi_entries(chain_id, target)
        if abi_entries:
            names = _address_getter_names(abi_entries)[:MAX_GETTER_CALLS]
            seen = {t.address.lower() for t in tokens}
            for name, address in _call_address_getters(chain_id, target, names).items():
                if not address or address.lower() in seen or int(address, 16) == 0:
                    continue
                seen.add(address.lower())
                meta = fetch_erc20_metadata(chain_id, address)
                if meta:
                    tokens.append(
                        RelatedToken(
                            getter=name,
                            address=to_checksum_address(address),
                            symbol=meta.symbol,
                            decimals=meta.decimals,
                        )
                    )
    except Exception as e:  # noqa: BLE001 - enrichment only; never block an alert
        logger.info("Related-token resolution failed for %s on chain %s: %s", target, chain_id, e)

    _cache[cache_key] = tokens
    return tokens


def format_related_tokens_block(
    per_target: list[tuple[str, list[RelatedToken]]],
    labels: dict[str, str] | None = None,
) -> str:
    """Render the ``Related Tokens`` prompt section. Returns "" when nothing resolved."""
    lines: list[str] = []
    for target, tokens in per_target:
        if not tokens:
            continue
        label = (labels or {}).get(to_checksum_address(target)) if target.startswith("0x") else None
        header = f"{target} ({label}):" if label else f"{target}:"
        lines.append(header)
        lines.extend(f"  {t.source} -> {t.address} ({t.symbol}, {t.decimals} decimals)" for t in tokens)
    return "\n".join(lines)
