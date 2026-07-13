from protocols.yearn import check_endorsed
from utils.chains import Chain


def test_alerted_cache_key_includes_chain_and_lowercase_address() -> None:
    key = check_endorsed.alerted_cache_key(Chain.MAINNET, "0xABCDEF")

    assert key == "yearn_endorsed_alerted_1_0xabcdef"


def test_filter_new_unendorsed_removes_previously_alerted_addresses(monkeypatch) -> None:
    cached = {
        check_endorsed.alerted_cache_key(Chain.MAINNET, "0xaaa"),
        check_endorsed.alerted_cache_key(Chain.BASE, "0xccc"),
    }

    def read_cache(_filename: str, key: str) -> int:
        return 1 if key in cached else 0

    monkeypatch.setattr(check_endorsed, "get_last_value_for_key_from_file", read_cache)

    result = check_endorsed.filter_new_unendorsed(
        {
            Chain.MAINNET: ["0xaaa", "0xbbb"],
            Chain.BASE: ["0xccc"],
        }
    )

    assert result == {Chain.MAINNET: ["0xbbb"]}


def test_mark_alerted_errors_persists_each_address(monkeypatch) -> None:
    writes = []

    def write_cache(_filename: str, key: str, value: int) -> None:
        writes.append((key, value))

    monkeypatch.setattr(check_endorsed, "write_last_value_to_file", write_cache)

    check_endorsed.mark_alerted_errors(
        {
            Chain.MAINNET: ["0xaaa", "0xbbb"],
            Chain.BASE: ["0xccc"],
        }
    )

    assert writes == [
        ("yearn_endorsed_alerted_1_0xaaa", 1),
        ("yearn_endorsed_alerted_1_0xbbb", 1),
        ("yearn_endorsed_alerted_8453_0xccc", 1),
    ]
