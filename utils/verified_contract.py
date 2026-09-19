"""Structured record of an Etherscan-verified contract.

Etherscan returns a *bundle*: the deployed contract plus every base, library and
interface it imports, as a standard-json ``sources`` map. Flattening that bundle
into one string (the old cache shape) throws away the only thing that makes the
sources attributable — which file, and which contract inside it, was actually
deployed. Semantic consumers (ABI surface diffs, body diffs) therefore take a
:class:`VerifiedContract` and work against its resolved compilation target;
:meth:`VerifiedContract.concatenated_source` remains available, but only as an
explicitly named best-effort *search* helper for natspec lookups.

The record is JSON-round-trippable so it can live in the on-disk source cache.
``CACHE_SCHEMA_VERSION`` is part of the cache namespace: positive source entries
never expire, so any change to this shape needs a namespace bump to avoid
reading stale records written by an older version.
"""

import json
from dataclasses import dataclass, field

from utils.logger import get_logger
from utils.solidity_text import declares_contract

logger = get_logger("utils.verified_contract")

CACHE_SCHEMA_VERSION = 2

_NOT_VERIFIED_ABI = "Contract source code not verified"


@dataclass(frozen=True)
class VerifiedContract:
    """One verified contract as Etherscan reports it, with provenance preserved."""

    contract_name: str
    compiler_version: str
    language: str
    sources: dict[str, str]  # file path -> file content, as compiled
    settings: dict = field(default_factory=dict)  # standard-json settings (remappings, optimizer, …)
    abi: list[dict] = field(default_factory=list)
    contract_file: str | None = None  # resolved compilation target, None if ambiguous

    @property
    def compilation_target(self) -> tuple[str, str] | None:
        """(file, contract_name) when the deployed contract's file is resolved."""
        return (self.contract_file, self.contract_name) if self.contract_file else None

    @property
    def target_source(self) -> str:
        """Source of the file that defines the deployed contract ("" if unresolved)."""
        return self.sources.get(self.contract_file, "") if self.contract_file else ""

    def concatenated_source(self) -> str:
        """Every file in the bundle joined together — for text SEARCH only.

        Never use this for a semantic claim about the deployed contract: it mixes
        in bases, libraries and interfaces with no way to tell them apart.
        """
        return "\n\n".join(self.sources.values())

    def to_cache_dict(self) -> dict:
        return {
            "schema": CACHE_SCHEMA_VERSION,
            "contract_name": self.contract_name,
            "compiler_version": self.compiler_version,
            "language": self.language,
            "sources": self.sources,
            "settings": self.settings,
            "abi": self.abi,
            "contract_file": self.contract_file,
        }

    @classmethod
    def from_cache_dict(cls, data: object) -> "VerifiedContract | None":
        """Rebuild from a cached dict, or None if the entry is from another schema."""
        if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA_VERSION:
            return None
        sources = data.get("sources")
        if not isinstance(sources, dict):
            return None
        settings = data.get("settings")
        abi = data.get("abi")
        return cls(
            contract_name=str(data.get("contract_name") or ""),
            compiler_version=str(data.get("compiler_version") or ""),
            language=str(data.get("language") or "Solidity"),
            sources={str(k): str(v) for k, v in sources.items()},
            settings=settings if isinstance(settings, dict) else {},
            abi=abi if isinstance(abi, list) else [],
            contract_file=data.get("contract_file") or None,
        )


def parse_etherscan_entry(entry: dict) -> VerifiedContract | None:
    """Build a :class:`VerifiedContract` from one Etherscan ``getsourcecode`` result.

    Returns None when the entry carries no source (unverified contract).
    """
    raw_source = entry.get("SourceCode") or ""
    if not raw_source:
        return None

    contract_name = entry.get("ContractName") or ""
    language, sources, settings = _parse_source_bundle(raw_source, contract_name)
    return VerifiedContract(
        contract_name=contract_name,
        compiler_version=entry.get("CompilerVersion") or "",
        language=language,
        sources=sources,
        settings=settings,
        abi=parse_abi(entry.get("ABI") or "") or [],
        contract_file=resolve_contract_file(contract_name, sources, settings),
    )


def parse_abi(abi_json: str) -> list[dict] | None:
    """Parse Etherscan's ABI string. Returns None for unverified/malformed."""
    if not abi_json or abi_json == _NOT_VERIFIED_ABI:
        return None
    try:
        parsed = json.loads(abi_json)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def resolve_contract_file(contract_name: str, sources: dict[str, str], settings: dict) -> str | None:
    """Find the file that defines the deployed contract, or None if ambiguous.

    Resolution order:
      1. ``settings.compilationTarget`` (standard-json input, when Etherscan kept it);
      2. the unique file declaring ``contract_name``.

    Returns None rather than guessing when several files declare the name — a
    wrong target would attribute another contract's code to this address.
    """
    if not contract_name:
        return None

    target = settings.get("compilationTarget") if isinstance(settings, dict) else None
    if isinstance(target, dict):
        for path, name in target.items():
            if name == contract_name and isinstance(path, str):
                return path

    declaring = [path for path, content in sources.items() if declares_contract(content, contract_name)]
    if len(declaring) == 1:
        return declaring[0]
    if len(declaring) > 1:
        logger.debug("ambiguous compilation target for %s: %s", contract_name, declaring)
    return None


def _parse_source_bundle(raw_source: str, contract_name: str) -> tuple[str, dict[str, str], dict]:
    """Split Etherscan's ``SourceCode`` field into (language, sources, settings).

    Three shapes are returned by the API: a plain source string, a JSON map of
    ``path -> {"content": …}``, and a full standard-json input wrapped in an
    extra pair of braces.
    """
    stripped = raw_source.strip()
    fallback_path = f"{contract_name or 'Contract'}.sol"
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return "Solidity", {fallback_path: raw_source}, {}

    payload = stripped[1:-1] if stripped.startswith("{{") else stripped
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return "Solidity", {fallback_path: raw_source}, {}
    if not isinstance(parsed, dict):
        return "Solidity", {fallback_path: raw_source}, {}

    raw_sources = parsed.get("sources") if isinstance(parsed.get("sources"), dict) else parsed
    sources = _extract_file_contents(raw_sources)
    if not sources:
        return "Solidity", {fallback_path: raw_source}, {}

    language = parsed.get("language")
    settings = parsed.get("settings")
    return (
        language if isinstance(language, str) else "Solidity",
        sources,
        settings if isinstance(settings, dict) else {},
    )


def _extract_file_contents(raw_sources: object) -> dict[str, str]:
    """Map ``path -> content`` from a standard-json ``sources`` object."""
    if not isinstance(raw_sources, dict):
        return {}
    out: dict[str, str] = {}
    for path, entry in raw_sources.items():
        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
            out[str(path)] = entry["content"]
    return out
