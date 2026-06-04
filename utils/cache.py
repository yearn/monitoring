import os
from typing import Union

from dotenv import load_dotenv

load_dotenv()

# CACHE_DIR is the single knob for where all on-disk dedupe/cache state lives.
# Default "" → the current working directory, so local runs drop files in the
# repo as before. On the VPS the systemd unit sets CACHE_DIR=/srv/cache (the one
# writable path under the hardened service); see deploy/systemd/yearn-monitor.service.
CACHE_DIR: str = os.getenv("CACHE_DIR", "")


def cache_path(filename: str) -> str:
    """Resolve a cache ``filename`` against ``CACHE_DIR``.

    An absolute ``filename`` is returned unchanged (``os.path.join`` semantics), so an
    explicit override always wins over ``CACHE_DIR``.
    """
    return os.path.join(CACHE_DIR, filename)


# format of the data: "protocol:value"
cache_filename: str = cache_path(os.getenv("CACHE_FILENAME", "cache-id.txt"))
# format of the data: "address:nonce"
nonces_filename: str = cache_path(os.getenv("NONCE_FILENAME", "nonces.txt"))
# format of the data: "vault_address+market_id+type_value:cap_timestamp"
# Same default basename as cache_filename — hourly shares one file across alert
# dedupe and morpho rows; the daily profile overrides MORPHO_FILENAME to isolate.
morpho_filename: str = cache_path(os.getenv("MORPHO_FILENAME", "cache-id.txt"))


def get_last_queued_id_from_file(protocol: str) -> int:
    return int(get_last_value_for_key_from_file(cache_filename, protocol))


def write_last_queued_id_to_file(protocol: str, proposal_id: Union[int, str]) -> None:
    write_last_value_to_file(cache_filename, protocol, proposal_id)


def get_last_executed_nonce_from_file(safe_address: str) -> int:
    return int(get_last_value_for_key_from_file(nonces_filename, safe_address))


def write_last_executed_nonce_to_file(safe_address: str, nonce: int) -> None:
    write_last_value_to_file(nonces_filename, safe_address, nonce)


def get_last_executed_morpho_from_file(vault_address: str, market_id: str, value_type: str) -> int:
    return int(get_last_value_for_key_from_file(morpho_filename, morpho_key(vault_address, market_id, value_type)))


def write_last_executed_morpho_to_file(
    vault_address: str, market_id: str, value_type: str, value: Union[int, str]
) -> None:
    write_last_value_to_file(morpho_filename, morpho_key(vault_address, market_id, value_type), value)


def morpho_key(vault_address: str, market_id: str, value_type: str) -> str:
    return vault_address + "+" + market_id + "+" + value_type


def get_last_value_for_key_from_file(filename: str, wanted_key: str) -> Union[str, int]:
    if not os.path.exists(filename):
        return 0
    else:
        with open(filename, "r") as f:
            # read line by line in format "key:value"
            lines = f.readlines()
            for line in lines:
                key, value = line.strip().split(":")
                if key == wanted_key:
                    return value
    return 0


def write_last_value_to_file(filename: str, write_key: str, write_value: Union[int, str, float]) -> None:
    # check if the proposal ud is already in the file, then update the id else append
    if os.path.exists(filename):
        with open(filename, "r") as f:
            lines = f.readlines()
            for i, line in enumerate(lines):
                key, _ = line.strip().split(":")
                if key == write_key:
                    lines[i] = f"{write_key}:{write_value}\n"
                    break
            else:
                lines.append(f"{write_key}:{write_value}\n")
        with open(filename, "w") as f:
            f.writelines(lines)
    else:
        lines = [f"{write_key}:{write_value}\n"]
        with open(filename, "w") as f:
            f.writelines(lines)
