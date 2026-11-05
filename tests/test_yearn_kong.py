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

    def fake_request_with_retry(method: str, url: str, *, json: dict, timeout: int) -> FakeResponse:
        calls.append((method, url, json, timeout))
        return FakeResponse(_vault_payload())

    monkeypatch.setattr(kong, "request_with_retry", fake_request_with_retry)

    vaults = kong.fetch_kong_vaults(Chain.MAINNET)

    assert calls[0][0] == "post"
    assert calls[0][1] == kong.KONG_GQL_URL
    assert calls[0][2]["variables"] == {"chainId": Chain.MAINNET.chain_id}
    assert calls[0][3] == 30
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
        kong,
        "request_with_retry",
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
    monkeypatch.setattr(kong, "request_with_retry", lambda *_args, **_kwargs: FakeResponse(payload))

    with pytest.raises(kong.KongRequestError):
        kong.fetch_kong_vaults(Chain.MAINNET)


def test_fetch_kong_parent_vaults_filters_retired_and_malformed_vaults(monkeypatch) -> None:
    payload = {
        "data": {
            "vaults": [
                {
                    "address": "0xParent",
                    "name": "USDC yVault",
                    "symbol": "yvUSDC",
                    "decimals": "6",
                    "vaultType": 1,
                    "asset": {"address": "0xAsset", "symbol": "USDC", "decimals": 6},
                    "meta": {"isRetired": False},
                },
                {
                    "address": "0xRetired",
                    "name": "Old vault",
                    "symbol": "yvOLD",
                    "decimals": "18",
                    "vaultType": 1,
                    "asset": {"address": "0xOldAsset", "symbol": "OLD", "decimals": 18},
                    "meta": {"isRetired": True},
                },
                {
                    "address": "0xHidden",
                    "name": "Hidden vault",
                    "symbol": "yvHIDDEN",
                    "decimals": "18",
                    "vaultType": 1,
                    "asset": {"address": "0xHiddenAsset", "symbol": "HIDDEN", "decimals": 18},
                    "meta": {"isRetired": False, "isHidden": True},
                },
                {
                    "address": "0xNoAsset",
                    "name": "Broken vault",
                    "symbol": "yvBROKEN",
                    "decimals": "18",
                    "vaultType": 1,
                    "asset": None,
                    "meta": {"isRetired": False},
                },
                {
                    "address": "0xNoDecimals",
                    "name": "Broken decimals vault",
                    "symbol": "yvNODEC",
                    "decimals": "18",
                    "vaultType": 1,
                    "asset": {"address": "0xAsset2", "symbol": "NODEC", "decimals": None},
                    "meta": {"isRetired": False},
                },
            ]
        }
    }
    calls = []

    def fake_request_with_retry(method: str, url: str, *, json: dict, timeout: int) -> FakeResponse:
        calls.append((method, url, json, timeout))
        return FakeResponse(payload)

    monkeypatch.setattr(kong, "request_with_retry", fake_request_with_retry)

    vaults = kong.fetch_kong_parent_vaults(Chain.MAINNET)

    assert "vaultType: 1" in calls[0][2]["query"]
    assert calls[0][2]["variables"] == {"chainId": 1}
    assert vaults == [
        {
            "address": "0xParent",
            "name": "USDC yVault",
            "symbol": "yvUSDC",
            "decimals": 6,
            "asset_address": "0xAsset",
            "asset_symbol": "USDC",
            "asset_decimals": 6,
        },
        {
            "address": "0xHidden",
            "name": "Hidden vault",
            "symbol": "yvHIDDEN",
            "decimals": 18,
            "asset_address": "0xHiddenAsset",
            "asset_symbol": "HIDDEN",
            "asset_decimals": 18,
        },
    ]


def test_fetch_kong_v2_strategies_excludes_retired_vaults_v3_and_other_strategies(monkeypatch) -> None:
    payload = {
        "data": {
            "vaults": [
                {
                    "chainId": 1,
                    "apiVersion": "0.4.6",
                    "strategies": ["0xAAA", "0xCCC", "0xBAD"],
                    "meta": {"isRetired": False, "isHidden": True},
                },
                {"chainId": 10, "apiVersion": "0.4.6", "strategies": ["0xAAA"]},
                {"chainId": 1, "apiVersion": "0.4.3", "strategies": ["0xBBB"], "meta": {"isRetired": True}},
                {"chainId": 1, "apiVersion": "3.0.4", "strategies": ["0xDDD"]},
                None,
            ],
            "strategies": [
                {"chainId": 1, "address": "0xAaA", "name": "StrategyCurve", "apiVersion": "0.4.6"},
                {"chainId": 10, "address": "0xAAA", "name": "StrategyVelo", "apiVersion": "0.4.6"},
                {"chainId": 8453, "address": "0xAAA", "name": "Other chain", "apiVersion": "0.4.6"},
                {"chainId": 1, "address": "0xBBB", "name": "Retired vault", "apiVersion": "0.4.3"},
                {"chainId": 1, "address": "0xCCC", "name": "V3 strategy", "apiVersion": "3.0.4"},
                {"chainId": 1, "address": "0xDDD", "name": "V3 vault", "apiVersion": "0.4.6"},
                {"chainId": 1, "address": "0xEEE", "name": "Unrelated", "apiVersion": "0.4.6"},
                {"chainId": 1, "address": "0xBAD", "name": None, "apiVersion": "0.4.6"},
                None,
            ],
        }
    }

    def request(method, url, **kwargs):
        assert "vaults(v3: false, yearn: true, limit: $limit, offset: $offset)" in kwargs["json"]["query"]
        return FakeResponse(payload)

    monkeypatch.setattr(kong, "request_with_retry", request)
    assert kong.fetch_kong_v2_strategies() == [
        {"chain_id": 1, "address": "0xAaA", "name": "StrategyCurve"},
        {"chain_id": 10, "address": "0xAAA", "name": "StrategyVelo"},
    ]


@pytest.mark.parametrize("data", [{}, {"vaults": []}, {"strategies": []}])
def test_fetch_kong_v2_strategies_raises_without_required_lists(monkeypatch, data) -> None:
    monkeypatch.setattr(kong, "request_with_retry", lambda *args, **kwargs: FakeResponse({"data": data}))
    with pytest.raises(kong.KongRequestError, match="missing (vaults|strategies) list"):
        kong.fetch_kong_v2_strategies()


def test_fetch_kong_v2_strategies_accepts_empty_lists(monkeypatch) -> None:
    payload = {"data": {"vaults": [], "strategies": []}}
    monkeypatch.setattr(kong, "request_with_retry", lambda *args, **kwargs: FakeResponse(payload))
    assert kong.fetch_kong_v2_strategies() == []


def test_fetch_kong_v2_strategies_paginates_vaults_and_fetches_strategies_once(monkeypatch) -> None:
    calls = []
    vault = {"chainId": 10, "apiVersion": "0.4.6", "strategies": ["0xAAA"]}
    strategy = {"chainId": 10, "apiVersion": "0.4.6", "address": "0xAAA", "name": "StrategyVelo"}

    def request(method, url, **kwargs):
        variables = kwargs["json"]["variables"]
        calls.append(variables)
        if variables["offset"] == 0:
            return FakeResponse({"data": {"vaults": [None] * 1000, "strategies": [strategy]}})
        return FakeResponse({"data": {"vaults": [vault]}})

    monkeypatch.setattr(kong, "request_with_retry", request)
    assert kong.fetch_kong_v2_strategies() == [{"chain_id": 10, "address": "0xAAA", "name": "StrategyVelo"}]
    assert calls == [
        {"limit": 1000, "offset": 0, "includeStrategies": True},
        {"limit": 1000, "offset": 1000, "includeStrategies": False},
    ]
