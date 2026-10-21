"""Small read-only MCP bridge for monitoring's configured chain data sources.

The agent supplies a chain, method, and public on-chain identifiers. It cannot
choose a URL, HTTP method, API key, or write-capable JSON-RPC method. This server
runs once per LLM completion and limits both calls and response size.
"""

import json
import os
import re
from typing import Any

import requests
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from utils.chains import PUBLIC_RPC_URLS, Chain

SERVER_NAME = "monitoring_chain"
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
MAX_CALLS = 8
MAX_RPC_BYTES = 64_000
MAX_ETHERSCAN_BYTES = 256_000
MAX_SOURCE_CHARS = 24_000
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_HEX = re.compile(r"0x(?:[0-9a-fA-F]{2})*\Z")
_QUANTITY = re.compile(r"0x[0-9a-fA-F]{1,64}\Z")
_ALLOWED_RPC = {
    "eth_call",
    "eth_getBalance",
    "eth_getCode",
    "eth_getStorageAt",
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
}

mcp = MCPServer(SERVER_NAME)
_calls = 0


def _use_budget() -> None:
    global _calls
    _calls += 1
    if _calls > MAX_CALLS:
        raise ValueError("Chain lookup budget exhausted for this summary")


def _chain(chain_id: int) -> Chain:
    try:
        return Chain.from_chain_id(chain_id)
    except ValueError as e:
        raise ValueError("Unsupported chain") from e


def _rpc_url(chain: Chain) -> str:
    name = f"PROVIDER_URL_{chain.name}"
    for key in (name, *(f"{name}_{i}" for i in range(1, 4))):
        if url := os.getenv(key):
            return url
    if url := PUBLIC_RPC_URLS.get(chain.chain_id):
        return url
    raise ValueError("No RPC provider configured for this chain")


def _block(value: Any) -> bool:
    return value in ("latest", "safe", "finalized") or (isinstance(value, str) and bool(_QUANTITY.fullmatch(value)))


def _validate_rpc(method: str, params: list[Any]) -> None:
    if method not in _ALLOWED_RPC:
        raise ValueError("RPC method is not allowed")
    if not isinstance(params, list):
        raise ValueError("RPC params must be a list")
    if method == "eth_call":
        if len(params) != 2 or not isinstance(params[0], dict) or not _block(params[1]):
            raise ValueError("eth_call requires a transaction and block")
        call = params[0]
        if set(call) != {"to", "data"} or not isinstance(call["to"], str) or not _ADDRESS.fullmatch(call["to"]):
            raise ValueError("eth_call permits only a contract address and calldata")
        if not isinstance(call["data"], str) or len(call["data"]) > 4098 or not _HEX.fullmatch(call["data"]):
            raise ValueError("Invalid eth_call calldata")
    elif method in ("eth_getBalance", "eth_getCode"):
        if (
            len(params) != 2
            or not isinstance(params[0], str)
            or not _ADDRESS.fullmatch(params[0])
            or not _block(params[1])
        ):
            raise ValueError("Expected address and block")
    elif method == "eth_getStorageAt":
        if (
            len(params) != 3
            or not isinstance(params[0], str)
            or not _ADDRESS.fullmatch(params[0])
            or not isinstance(params[1], str)
            or not _QUANTITY.fullmatch(params[1])
            or not _block(params[2])
        ):
            raise ValueError("Expected address, storage slot and block")
    elif len(params) != 1 or not isinstance(params[0], str) or not _HASH.fullmatch(params[0]):
        raise ValueError("Expected transaction hash")


def _get_json(
    url: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None, limit: int
) -> dict[str, Any]:
    """Fetch one bounded JSON response without redirects or ambient proxies."""
    try:
        with requests.Session() as session:
            session.trust_env = False
            request = session.post if payload is not None else session.get
            with request(
                url, params=params, json=payload, timeout=(3, 10), stream=True, allow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise ValueError("Chain data source returned an HTTP error")
                raw = bytearray()
                for chunk in response.iter_content(8192):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ValueError("Chain data response is too large")
        result = json.loads(raw)
    except requests.RequestException, json.JSONDecodeError:
        # Requests' exception strings can contain provider URLs and API keys.
        raise ValueError("Chain data request failed") from None
    if not isinstance(result, dict):
        raise ValueError("Chain data response was not an object")
    return result


def read_rpc(chain_id: int, method: str, params: list[Any]) -> dict[str, Any]:
    """Read public chain state through an operator-configured RPC endpoint."""
    chain = _chain(chain_id)
    _validate_rpc(method, params)
    _use_budget()
    data = _get_json(
        _rpc_url(chain),
        payload={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        limit=MAX_RPC_BYTES,
    )
    if data.get("jsonrpc") != "2.0" or data.get("id") != 1 or "result" not in data:
        raise ValueError("Malformed RPC response")
    if "error" in data:
        # Do not pass provider URLs or echoed credentials through RPC errors.
        raise ValueError("RPC returned an error")
    return {"chain_id": chain_id, "method": method, "result": data.get("result")}


def read_etherscan(chain_id: int, address: str, kind: str) -> dict[str, Any]:
    """Fetch a verified contract ABI or bounded source from Etherscan V2."""
    _chain(chain_id)
    if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
        raise ValueError("Invalid contract address")
    if kind not in ("abi", "source"):
        raise ValueError("Etherscan lookup must be abi or source")
    token = os.getenv("ETHERSCAN_TOKEN")
    if not token:
        raise ValueError("Etherscan API key is not configured")
    _use_budget()
    data = _get_json(
        ETHERSCAN_URL,
        params={
            "chainid": str(chain_id),
            "module": "contract",
            "action": "getabi" if kind == "abi" else "getsourcecode",
            "address": address,
            "apikey": token,
        },
        limit=MAX_ETHERSCAN_BYTES,
    )
    if data.get("status") != "1":
        raise ValueError("Etherscan did not return a verified contract")
    if kind == "abi":
        abi = data.get("result")
        if not isinstance(abi, str):
            raise ValueError("Invalid ABI response")
        return {
            "chain_id": chain_id,
            "address": address,
            "abi": abi[:MAX_SOURCE_CHARS],
            "truncated": len(abi) > MAX_SOURCE_CHARS,
        }
    entries = data.get("result")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise ValueError("Invalid source response")
    entry = entries[0]
    source = entry.get("SourceCode") or ""
    if not isinstance(source, str):
        raise ValueError("Invalid source code")
    return {
        "chain_id": chain_id,
        "address": address,
        "contract_name": str(entry.get("ContractName") or ""),
        "compiler_version": str(entry.get("CompilerVersion") or ""),
        "proxy": str(entry.get("Proxy") or ""),
        "implementation": str(entry.get("Implementation") or ""),
        "source": source[:MAX_SOURCE_CHARS],
        "truncated": len(source) > MAX_SOURCE_CHARS,
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def rpc_read(chain_id: int, method: str, params: list[Any]) -> dict[str, Any]:
    """Read public RPC data. Methods: eth_call, eth_getBalance, eth_getCode, eth_getStorageAt, eth_getTransactionByHash, eth_getTransactionReceipt. No writes or arbitrary URL."""
    return read_rpc(chain_id, method, params)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def etherscan_contract(chain_id: int, address: str, kind: str) -> dict[str, Any]:
    """Read a verified contract ABI or source with the configured Etherscan key. kind is abi or source. No arbitrary Etherscan API action."""
    return read_etherscan(chain_id, address, kind)


if __name__ == "__main__":
    mcp.run()
