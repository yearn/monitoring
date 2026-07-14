import pytest

from protocols.yearn import kong
from utils.chains import Chain


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


def _vault_payload() -> dict:
    return {
        "data": {
            "vaults": [
                {
                    "address": "0xABC",
                    "symbol": "yvTEST",
                    "decimals": "6",
                    "strategies": ["0xAAA", "0xBBB"],
                    "get_default_queue": ["0xBBB"],
                    "meta": {"isRetired": False},
                },
                {
                    "address": "0xDEF",
                    "symbol": "yvOLD",
                    "decimals": "18",
                    "strategies": ["0xCCC"],
                    "get_default_queue": ["0xCCC"],
                    "meta": {"isRetired": True},
                },
            ]
        }
    }


def test_fetch_kong_vaults_uses_all_strategies_by_default(monkeypatch) -> None:
    calls = []

    def fake_post(url: str, json: dict, timeout: int) -> FakeResponse:
        calls.append((url, json, timeout))
        return FakeResponse(_vault_payload())

    monkeypatch.setattr(kong.requests, "post", fake_post)

    vaults = kong.fetch_kong_vaults(Chain.MAINNET)

    assert calls[0][0] == kong.KONG_GQL_URL
    assert calls[0][1]["variables"] == {"chainId": Chain.MAINNET.chain_id}
    assert calls[0][2] == 30
    assert vaults == [
        {
            "address": "0xabc",
            "symbol": "yvTEST",
            "decimals": 6,
            "strategies": [{"address": "0xaaa"}, {"address": "0xbbb"}],
            "known_strategies": ["0xaaa", "0xbbb"],
        }
    ]


def test_fetch_kong_vaults_can_use_default_queue(monkeypatch) -> None:
    monkeypatch.setattr(
        kong.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(_vault_payload()),
    )

    vaults = kong.fetch_kong_vaults(
        Chain.MAINNET,
        strategy_source=kong.STRATEGY_SOURCE_DEFAULT_QUEUE,
    )

    assert vaults[0]["strategies"] == [{"address": "0xbbb"}]
    assert vaults[0]["known_strategies"] == ["0xbbb"]


def test_fetch_kong_vaults_raises_on_graphql_errors(monkeypatch) -> None:
    payload = {"errors": [{"message": "bad query"}]}
    monkeypatch.setattr(kong.requests, "post", lambda *_args, **_kwargs: FakeResponse(payload))

    with pytest.raises(kong.KongRequestError):
        kong.fetch_kong_vaults(Chain.MAINNET)
