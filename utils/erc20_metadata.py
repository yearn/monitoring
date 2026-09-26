"""Fetch ERC20 token metadata (symbol, decimals) for LLM prompt enrichment.

Without this, the LLM sees raw amounts like ``transfer(0xUSDC, 1000000000)``
and can't tell if that's $1 (6 decimals) or $1e-12 (18 decimals). Looking
up ``symbol()`` and ``decimals()`` once per unique address lets us annotate
the prompt with ``(USDC, 6 dec)`` so the model can compute concrete values.

This is best-effort: any non-ERC20 address fails the eth_call cleanly and
returns ``None``, treated as "no metadata available".
"""

from dataclasses import dataclass

from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from utils.abi import load_abi
from utils.chains import Chain
from utils.logger import get_logger
from utils.proxy import get_current_implementation, minimal_proxy_implementation
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.erc20_metadata")

_ERC20_ABI = None  # loaded lazily on first call

# Per-process cache: (chain_id, address_lower) -> ERC20Metadata or None for miss.
_cache: dict[tuple[int, str], "ERC20Metadata | None"] = {}

# Selectors an ERC20 must dispatch. We only ever call symbol()/decimals() when
# the contract bytecode actually contains both — never a blind eth_call we expect
# to fail. Stored as bare lowercase hex (no 0x) to substring-match raw bytecode.
_SYMBOL_SELECTOR = function_signature_to_4byte_selector("symbol()").hex()  # 95d89b41
_DECIMALS_SELECTOR = function_signature_to_4byte_selector("decimals()").hex()  # 313ce567
_NAME_SELECTOR = function_signature_to_4byte_selector("name()").hex()  # 06fdde03


@dataclass(frozen=True)
class ERC20Metadata:
    """Decoded ERC20 token metadata."""

    symbol: str
    decimals: int
    # On-chain ``name()``, when the token dispatches it and it decodes as a
    # string. Optional: a bytes32 name (MKR) or a missing getter leaves it None
    # without failing the symbol/decimals lookup.
    name: str | None = None


def fetch_erc20_metadata(chain_id: int, address: str) -> ERC20Metadata | None:
    """Return token metadata for an address, or None if it isn't ERC20-compatible.

    Both ``symbol()`` and ``decimals()`` must succeed — partial responses are
    treated as a miss. ``name()`` is read in the same batch when the bytecode
    dispatches it; if that batch fails, the pair is retried without it so a
    non-standard name never costs the token its symbol and decimals.
    """
    if not address or len(address) != 42 or not address.startswith("0x"):
        return None

    cache_key = (chain_id, address.lower())
    if cache_key in _cache:
        return _cache[cache_key]

    global _ERC20_ABI
    if _ERC20_ABI is None:
        _ERC20_ABI = load_abi("common-abi/ERC20.json")

    try:
        chain = Chain.from_chain_id(chain_id)
        client = ChainManager.get_client(chain)
        checksum = to_checksum_address(address)

        # Gate: only call symbol()/decimals() when the bytecode proves the
        # contract dispatches them. EOAs and non-token contracts are skipped
        # without a blind eth_call.
        token_code = _token_code(chain_id, client, checksum)
        if token_code is None:
            _cache[cache_key] = None
            return None

        token = client.get_contract(checksum, _ERC20_ABI)
        meta = None
        if _NAME_SELECTOR in token_code:
            try:
                with client.batch_requests() as batch:
                    batch.add(token.functions.symbol())
                    batch.add(token.functions.decimals())
                    batch.add(token.functions.name())
                    symbol, decimals, name = client.execute_batch(batch)
                meta = ERC20Metadata(symbol=str(symbol), decimals=int(decimals), name=str(name) or None)
            except Exception as e:  # noqa: BLE001 - retried below without name()
                logger.debug("ERC20 name() read failed for %s on chain %s: %s", address, chain_id, e)
        if meta is None:
            with client.batch_requests() as batch:
                batch.add(token.functions.symbol())
                batch.add(token.functions.decimals())
                symbol, decimals = client.execute_batch(batch)
            meta = ERC20Metadata(symbol=str(symbol), decimals=int(decimals))
    except Exception as e:  # noqa: BLE001 - any eth_call failure -> not an ERC20
        logger.debug("ERC20 metadata fetch failed for %s on chain %s: %s", address, chain_id, e)
        _cache[cache_key] = None
        return None

    _cache[cache_key] = meta
    return meta


def _code_hex(client, address: str) -> str:
    """Return deployed bytecode at ``address`` as bare lowercase hex ("" if none)."""
    raw = client.eth.get_code(to_checksum_address(address))
    code = raw.hex()
    if code.startswith("0x"):
        code = code[2:]
    return code.lower()


def _has_token_selectors(code: str) -> bool:
    """True if bytecode dispatches both symbol() and decimals()."""
    return bool(code) and _SYMBOL_SELECTOR in code and _DECIMALS_SELECTOR in code


def _token_code(chain_id: int, client, checksum: str) -> str | None:
    """Positive-evidence ERC20 check via bytecode inspection.

    Returns the bytecode that dispatches both symbol() and decimals() — the
    contract's own, or for proxies its implementation's — or None. EOAs and
    non-token contracts return None so we never blind-call functions they
    can't serve.

    Proxy stubs delegate through a fallback and carry none of the impl's
    selectors, so a bare scan would false-negative proxy tokens (e.g. USDC).
    EIP-1167 clones (every Yearn V3 vault) embed the implementation in their
    bytecode; other proxies are resolved through ``get_current_implementation``.
    """
    code = _code_hex(client, checksum)
    if not code:
        return None  # EOA / no deployed code
    if _has_token_selectors(code):
        return code
    impl = minimal_proxy_implementation(code) or get_current_implementation(checksum, chain_id)
    if impl:
        impl_code = _code_hex(client, impl)
        return impl_code if _has_token_selectors(impl_code) else None
    return None


def reset_cache() -> None:
    """Reset the in-memory metadata cache. Useful for tests."""
    _cache.clear()
