"""Resolve ``bytes32`` role identifiers to the role names they hash from.

Access-control calls (``grantRole``, ``revokeRole``, ``setRoleAdmin``, …) carry
their role as an opaque ``bytes32`` that is almost always
``keccak256("SOME_ROLE_NAME")``. The hash is one-way, so a decoded call on its
own can only ever show the digest — which is why role grants reach the LLM as
unidentified parameters and get their severity understated.

Two sources recover the pre-image, cheapest first:

- **Static table** — OpenZeppelin's ``AccessControl``/``TimelockController``
  roles plus the role names that recur across protocols. Hashed once at import.
- **Verified source** — protocols declare their roles as
  ``bytes32 constant NAME = keccak256("NAME")``. ``fetch_source`` already
  returns every file of a multi-file verified contract concatenated, and it is
  memoized and disk-cached, so harvesting these costs no extra HTTP when source
  context was already pulled for the same target.

``DEFAULT_ADMIN_ROLE`` is the one role that is not a pre-image: OpenZeppelin
defines it as ``bytes32(0)``, so it is mapped explicitly.
"""

import re

from eth_utils import keccak

from utils.logger import get_logger

logger = get_logger("utils.calldata.role_names")

# `bytes32 [internal|public|private] constant NAME = keccak256("PREIMAGE");`
# The declared constant name and the hashed string are captured separately —
# they usually match, but nothing enforces it, and the pre-image is what the
# hash actually commits to.
_ROLE_CONSTANT_RE = re.compile(
    r'bytes32\s+(?:internal\s+|public\s+|private\s+)?constant\s+(\w+)\s*=\s*keccak256\(\s*"([^"]+)"\s*\)'
)

# OpenZeppelin's AccessControl treats bytes32(0) as the admin of every other
# role, so it is worth naming even though it has no pre-image.
DEFAULT_ADMIN_ROLE = "0x" + "00" * 32

_COMMON_ROLE_NAMES = (
    # OpenZeppelin TimelockController
    "TIMELOCK_ADMIN_ROLE",
    "PROPOSER_ROLE",
    "EXECUTOR_ROLE",
    "CANCELLER_ROLE",
    # Recurring across ERC20/ERC721 presets and protocol access control
    "ADMIN_ROLE",
    "BURNER_ROLE",
    "GOVERNOR_ROLE",
    "GUARDIAN_ROLE",
    "KEEPER_ROLE",
    "MANAGER_ROLE",
    "MINTER_ROLE",
    "OPERATOR_ROLE",
    "ORACLE_ROLE",
    "PAUSER_ROLE",
    "RELAYER_ROLE",
    "SNAPSHOT_ROLE",
    "UPGRADER_ROLE",
)


def _hash_of(name: str) -> str:
    return "0x" + keccak(text=name).hex()


_STATIC_ROLES: dict[str, str] = {DEFAULT_ADMIN_ROLE: "DEFAULT_ADMIN_ROLE"}
_STATIC_ROLES.update({_hash_of(name): name for name in _COMMON_ROLE_NAMES})


def normalize_role_hash(value: str) -> str:
    """Return ``value`` as a lowercase 0x-prefixed 32-byte hex string, or ``""``.

    Decoded ``bytes32`` params arrive already formatted as ``0x``-prefixed hex,
    but callers may pass an unprefixed digest or mixed case.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if len(candidate) != 64:
        return ""
    try:
        int(candidate, 16)
    except ValueError:
        return ""
    return f"0x{candidate}"


def harvest_role_names(source: str) -> dict[str, str]:
    """Map role hash → declared name for every ``keccak256`` role constant in ``source``.

    Args:
        source: Solidity source text, typically the concatenated files of a
            verified contract.

    Returns:
        Mapping of lowercase 0x-prefixed role hash to the constant's name. The
        declared name is preferred for display; when it differs from the hashed
        string, both are reported so the discrepancy stays visible.
    """
    resolved: dict[str, str] = {}
    for declared_name, preimage in _ROLE_CONSTANT_RE.findall(source or ""):
        label = declared_name if declared_name == preimage else f'{declared_name} (keccak256("{preimage}"))'
        resolved[_hash_of(preimage)] = label
    return resolved


def resolve_role_names(
    role_hashes: list[str], chain_id: int | None = None, target: str | None = None
) -> dict[str, str]:
    """Resolve role hashes to names, consulting the static table then ``target``'s source.

    Args:
        role_hashes: Candidate ``bytes32`` values, in any casing.
        chain_id: Chain to look ``target`` up on. Source harvesting is skipped
            when either this or ``target`` is missing.
        target: Contract whose verified source declares the roles.

    Returns:
        Mapping of normalized role hash to role name, containing only the
        hashes that resolved. Never raises — an unresolvable role simply stays
        absent so the caller can report it as unidentified.
    """
    wanted = {normalized for h in role_hashes if (normalized := normalize_role_hash(h))}
    if not wanted:
        return {}

    resolved = {h: name for h, name in _STATIC_ROLES.items() if h in wanted}

    unresolved = wanted - resolved.keys()
    if not unresolved or not chain_id or not target:
        return resolved

    try:
        # Imported lazily: source_context pulls in the Etherscan/web3 stack, and
        # the static table alone is enough for callers that never hit a chain.
        from utils.source_context import fetch_source

        record = fetch_source(chain_id, target)
    except Exception as e:  # noqa: BLE001 - enrichment only; unresolved roles are reported as such
        logger.info("Role-name source lookup failed for %s on chain %s: %s", target, chain_id, e)
        return resolved

    if record is None:
        return resolved

    for role_hash, name in harvest_role_names(record[1]).items():
        if role_hash in unresolved:
            resolved[role_hash] = name
    return resolved
