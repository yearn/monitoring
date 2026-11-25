"""Fetch verified source bundles from Sourcify's contract lookup API."""

import re

from eth_utils import is_address
from requests import HTTPError, RequestException

from utils.http_client import request_with_retry
from utils.logger import get_logger
from utils.verified_contract import ContractLookup, VerifiedContract

logger = get_logger("utils.sourcify_contract")

SOURCIFY_V2_API_URL = "https://sourcify.dev/server/v2/contract"
_USER_AGENT = "yearn-monitoring (+https://github.com/yearn/monitoring)"
_LOOKUP_TIMEOUT = 5.0
_MATCHES = ("match", "exact_match")
_FIELDS = "sources,abi,compilation,creationBytecode.transformationValues,runtimeBytecode.transformationValues"


def fetch_sourcify_contract(chain_id: int, address: str, *, timeout: float | None) -> ContractLookup:
    """Return a verified bundle, False for an explicit miss, or None on lookup failure."""
    if not is_address(address):
        return False
    url = f"{SOURCIFY_V2_API_URL}/{chain_id}/{address.lower()}"
    try:
        response = request_with_retry(
            "get",
            url,
            params={"fields": _FIELDS},
            headers={"User-Agent": _USER_AGENT},
            timeout=_LOOKUP_TIMEOUT if timeout is None else min(_LOOKUP_TIMEOUT, timeout),
            retries=0,
            log_errors=False,
        )
        payload = response.json()
    except HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            return False
        logger.debug("Sourcify lookup failed for %s on chain %s: %s", address, chain_id, error)
        return None
    except (RequestException, ValueError) as error:
        logger.debug("Sourcify lookup failed for %s on chain %s: %s", address, chain_id, error)
        return None
    if not isinstance(payload, dict):
        return None
    if "match" in payload and payload["match"] is None:
        return False
    return _parse_contract(payload)


def _parse_contract(payload: dict) -> VerifiedContract | None:
    """Map a runtime-verified Sourcify bundle onto the shared contract record."""
    match = payload.get("runtimeMatch")
    raw_sources = payload.get("sources")
    compilation = payload.get("compilation")
    abi = payload.get("abi")
    if match not in _MATCHES or not isinstance(raw_sources, dict) or not isinstance(compilation, dict):
        return None
    if not isinstance(abi, list) or not all(isinstance(entry, dict) for entry in abi):
        return None
    sources: dict[str, str] = {}
    for path, source in raw_sources.items():
        if not isinstance(source, dict) or not isinstance(source.get("content"), str):
            return None
        sources[path] = source["content"]

    language = compilation.get("language")
    compiler = compilation.get("compilerVersion")
    settings = compilation.get("compilerSettings")
    identifier = compilation.get("fullyQualifiedName")
    if not all(isinstance(value, str) and value for value in (language, compiler, identifier)):
        return None
    if not isinstance(settings, dict):
        return None
    path, _, name = identifier.rpartition(":")
    if not name or not sources.get(path):
        return None
    return VerifiedContract(
        contract_name=name,
        compiler_version=f"v{compiler}" if language == "Solidity" and not compiler.startswith("v") else compiler,
        language=language,
        sources=sources,
        settings=_with_linked_libraries(settings, payload),
        abi=abi,
        contract_file=path,
        constructor_arguments=_constructor_arguments(payload, abi),
        provider="Sourcify",
        match_quality=match,
    )


def _constructor_arguments(payload: dict, abi: list[dict]) -> str | None:
    """Read deployment arguments only when Sourcify matched the creation bytecode."""
    if payload.get("creationMatch") not in _MATCHES:
        return None
    args = _transformation_values(payload, "creationBytecode").get("constructorArguments")
    if isinstance(args, str):
        normalized = args.lower().removeprefix("0x")
        return normalized if re.fullmatch(r"(?:[0-9a-f]{2})*", normalized) else None
    # Sourcify omits this transformation when the constructor takes no arguments.
    return None if any(entry.get("type") == "constructor" and entry.get("inputs") for entry in abi) else ""


def _with_linked_libraries(settings: dict, payload: dict) -> dict:
    """Add observed deployment library addresses to the standard JSON settings."""
    configured = settings.get("libraries")
    libraries = (
        {path: entries.copy() for path, entries in configured.items() if isinstance(entries, dict)}
        if isinstance(configured, dict)
        else {}
    )
    for field in ("creationBytecode", "runtimeBytecode"):
        linked = _transformation_values(payload, field).get("libraries")
        if isinstance(linked, dict):
            for identifier, address in linked.items():
                path, _, name = identifier.rpartition(":")
                if path and name and isinstance(address, str) and is_address(address):
                    libraries.setdefault(path, {})[name] = address.lower()
    return {**settings, "libraries": libraries} if libraries else settings


def _transformation_values(payload: dict, field: str) -> dict:
    """Read the transformation values for creation or runtime bytecode."""
    bytecode = payload.get(field)
    values = bytecode.get("transformationValues") if isinstance(bytecode, dict) else None
    return values if isinstance(values, dict) else {}
