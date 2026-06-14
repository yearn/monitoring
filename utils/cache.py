import os
from typing import Union

from dotenv import load_dotenv

from utils import paths, store

load_dotenv()

CACHE_DIR = paths.CACHE_DIR
cache_path = paths.cache_path


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
    if os.getenv("CACHE_BACKEND", "sqlite") == "file":
        return _get_last_value_from_legacy_file(filename, wanted_key)

    namespace = os.path.basename(filename)
    value = store.state_get(namespace, wanted_key)
    if value is not None:
        return value

    legacy_value = _get_last_value_from_legacy_file(filename, wanted_key)
    if legacy_value != 0:
        store.state_set(namespace, wanted_key, str(legacy_value))
    return legacy_value


def write_last_value_to_file(filename: str, write_key: str, write_value: Union[int, str, float]) -> None:
    if os.getenv("CACHE_BACKEND", "sqlite") == "file":
        _write_last_value_to_legacy_file(filename, write_key, write_value)
        return

    store.state_set(os.path.basename(filename), write_key, str(write_value))
    if os.getenv("CACHE_DUAL_WRITE_LEGACY") == "1":
        _write_last_value_to_legacy_file(filename, write_key, write_value)


def _get_last_value_from_legacy_file(filename: str, wanted_key: str) -> Union[str, int]:
    if not os.path.exists(filename):
        return 0
    with open(filename, "r") as f:
        # read line by line in format "key:value"
        lines = f.readlines()
        for line in lines:
            key, value = line.strip().split(":", 1)
            if key == wanted_key:
                return value
    return 0


def _write_last_value_to_legacy_file(filename: str, write_key: str, write_value: Union[int, str, float]) -> None:
    # check if the proposal ud is already in the file, then update the id else append
    if os.path.exists(filename):
        with open(filename, "r") as f:
            lines = f.readlines()
            for i, line in enumerate(lines):
                key, _ = line.strip().split(":", 1)
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
