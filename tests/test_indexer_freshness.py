"""Tests for the Envio indexer freshness monitor."""

import pytest

from protocols.yearn import check_indexer_freshness as freshness
from protocols.yearn.check_indexer_freshness import ChainFreshness, IndexerUnavailableError

NOW = 1_800_000_000
HOUR = 3600


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def _rows() -> list[dict]:
    return [
        {"chain_id": 8453, "latest_processed_block": 49220190, "block_height": 49220390},
        {"chain_id": 1, "latest_processed_block": 24150245, "block_height": 25626800},
    ]


@pytest.fixture
def envio_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "https://indexer.example/v1/graphql"
    monkeypatch.setattr(freshness, "ENVIO_GRAPHQL_URL", url)
    return url


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every message the monitor routes to the errors channel."""
    messages: list[str] = []
    monkeypatch.setattr(freshness, "send_error_message", lambda msg, protocol, **kwargs: messages.append(msg))
    return messages


def test_collect_freshness_computes_lag_and_sorts_by_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = {1: NOW - 5 * HOUR, 8453: NOW - 120}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain_id, block: timestamps[chain_id])

    result = freshness.collect_freshness(_rows(), NOW)

    assert [c.chain_id for c in result] == [1, 8453]
    assert result[0].lag_seconds == 5 * HOUR
    assert result[0].name == "Ethereum"
    assert result[1].lag_seconds == 120


def test_collect_freshness_skips_chains_without_a_processed_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain_id, block: NOW)
    rows = [{"chain_id": 100, "latest_processed_block": 0, "block_height": 0}]

    assert freshness.collect_freshness(rows, NOW) == []


def test_collect_freshness_marks_lag_unknown_when_rpc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain_id, block: None)

    result = freshness.collect_freshness(_rows(), NOW)

    assert all(chain.lag_seconds is None for chain in result)
    # Unknown lag must never fire an alert — a broken RPC is not a stale indexer.
    assert not any(chain.is_stale(HOUR) for chain in result)


def test_is_stale_uses_threshold() -> None:
    chain = ChainFreshness(chain_id=1, latest_processed_block=100, lag_seconds=HOUR + 1)

    assert chain.is_stale(HOUR)
    assert not chain.is_stale(2 * HOUR)


def test_build_stale_message_lists_every_lagging_chain() -> None:
    stale = [
        ChainFreshness(chain_id=1, latest_processed_block=24150245, lag_seconds=205 * 86400),
        ChainFreshness(chain_id=747474, latest_processed_block=38465232, lag_seconds=2 * HOUR + 900),
    ]

    message = freshness.build_stale_message(stale, HOUR)

    assert "Ethereum (chain 1): 205d behind, last block 24150245" in message
    assert "Katana (chain 747474): 2h 15m behind, last block 38465232" in message
    assert "Threshold: 1h" in message
    assert freshness.DASHBOARD_URL in message


def test_fetch_chain_metadata_returns_rows(monkeypatch: pytest.MonkeyPatch, envio_url: str) -> None:
    captured: dict = {}

    def fake_request(method: str, url: str, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return FakeResponse({"data": {"chain_metadata": _rows()}})

    monkeypatch.setattr(freshness, "request_with_retry", fake_request)

    assert freshness.fetch_chain_metadata() == _rows()
    assert captured["method"] == "post"
    assert captured["url"] == envio_url


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "boom"}]},
        {"data": {"chain_metadata": []}},
        {"data": None},
    ],
)
def test_fetch_chain_metadata_raises_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, payload: dict
) -> None:
    monkeypatch.setattr(freshness, "request_with_retry", lambda *a, **kw: FakeResponse(payload))

    with pytest.raises(IndexerUnavailableError):
        freshness.fetch_chain_metadata()


def test_fetch_chain_metadata_raises_when_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "ENVIO_GRAPHQL_URL", None)

    with pytest.raises(IndexerUnavailableError, match="ENVIO_GRAPHQL_URL"):
        freshness.fetch_chain_metadata()


def test_chains_to_alert_respects_cooldown() -> None:
    stale = [ChainFreshness(chain_id=1, latest_processed_block=1, lag_seconds=2 * HOUR)]

    assert freshness.chains_to_alert(stale, NOW, 6 * HOUR) == stale

    freshness._set_last_alert_timestamp(1, NOW)
    assert freshness.chains_to_alert(stale, NOW + HOUR, 6 * HOUR) == []
    assert freshness.chains_to_alert(stale, NOW + 6 * HOUR, 6 * HOUR) == stale


def test_main_alerts_once_per_cooldown_then_reports_recovery(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str]
) -> None:
    monkeypatch.setattr(
        freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"data": {"chain_metadata": _rows()}})
    )
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])

    stale_timestamps = {1: NOW - 5 * HOUR, 8453: NOW - 120}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain_id, block: stale_timestamps[chain_id])
    monkeypatch.setattr(freshness.time, "time", lambda: NOW)

    freshness.main()
    assert len(sent) == 1
    assert "Ethereum" in sent[0]
    assert "Base" not in sent[0]

    # Second run inside the cooldown window stays quiet.
    freshness.main()
    assert len(sent) == 1

    # Once mainnet catches up, the recovery note fires and clears the state.
    stale_timestamps[1] = NOW - 60
    freshness.main()
    assert len(sent) == 2
    assert "caught up" in sent[1]

    freshness.main()
    assert len(sent) == 2


def test_main_alerts_when_indexer_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])
    monkeypatch.setattr(freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"errors": ["down"]}))

    freshness.main()

    assert len(sent) == 1
    assert "Envio indexer unavailable" in sent[0]
