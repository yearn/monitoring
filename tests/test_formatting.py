"""Tests for utils.formatting."""

from utils.formatting import redact_urls


def test_redact_urls_keeps_only_scheme_and_host() -> None:
    text = "Failed on https://eth-mainnet.g.alchemy.com/v2/SECRET?key=ALSO_SECRET: 429"

    assert redact_urls(text) == "Failed on https://eth-mainnet.g.alchemy.com/<redacted>: 429"


def test_redact_urls_covers_the_schemeless_path_in_urllib3_errors() -> None:
    text = "HTTPSConnectionPool(host='rpc.example', port=443): Max retries exceeded with url: /v2/SECRET (Caused by X)"

    assert redact_urls(text) == (
        "HTTPSConnectionPool(host='rpc.example', port=443): Max retries exceeded with url: /<redacted> (Caused by X)"
    )


def test_redact_urls_drops_userinfo_and_handles_several_urls() -> None:
    text = "a: wss://user:pass@node.example:8546/ws, b: http://10.0.0.1:8545"

    redacted = redact_urls(text)

    assert "user" not in redacted
    assert "pass" not in redacted
    assert redacted == "a: wss://node.example:8546/<redacted>, b: http://10.0.0.1:8545/<redacted>"
