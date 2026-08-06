"""Tests for the Envio indexer freshness monitor."""

import pytest
import requests

from protocols.yearn import check_indexer_freshness as freshness
from protocols.yearn.check_indexer_freshness import ChainFreshness, IndexerUnavailableError
from utils.chains import Chain

NOW = 1_800_000_000
HOUR = 3600

# Last processed block per expected chain, roughly as the live indexer reports them.
INDEXED_BLOCKS: dict[Chain, int] = {
    Chain.BASE: 49220190,
    Chain.MAINNET: 24150245,
    Chain.KATANA: 38465232,
    Chain.OPTIMISM: 154815667,
    Chain.POLYGON: 91016489,
    Chain.ARBITRUM: 488554295,
}

# Chains the indexer covers that nothing in this repo reads from.
UNMONITORED_ROWS = [
    {"chain_id": 100, "latest_processed_block": 47431537, "block_height": 47431737},
    {"chain_id": 80094, "latest_processed_block": 24107494, "block_height": 24107694},
]


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def _rows(*, omit: tuple[Chain, ...] = (), zero_block: tuple[Chain, ...] = ()) -> list[dict]:
    """chain_metadata as the indexer returns it: unsorted, including chains we ignore.

    Args:
        omit: Expected chains to leave out entirely, as a dropped indexer config would.
        zero_block: Expected chains present but with no processed block.
    """
    rows = [
        {
            "chain_id": chain.chain_id,
            "latest_processed_block": 0 if chain in zero_block else block,
            "block_height": block + 200,
        }
        for chain, block in INDEXED_BLOCKS.items()
        if chain not in omit
    ]
    return rows + UNMONITORED_ROWS


@pytest.fixture
def envio_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "https://indexer.example/v1/graphql"
    monkeypatch.setattr(freshness, "ENVIO_GRAPHQL_URL", url)
    return url


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every message the monitor routes to the envio channel."""
    messages: list[str] = []
    monkeypatch.setattr(freshness, "send_envio_error_message", lambda msg, protocol, **kwargs: messages.append(msg))
    return messages


def test_collect_freshness_computes_lag_and_sorts_by_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = {Chain.MAINNET: NOW - 5 * HOUR}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: timestamps.get(chain, NOW - 120))

    result = freshness.collect_freshness(_rows(), NOW)

    # Gnosis (100) and Berachain (80094) are indexed but unused here, so they drop out.
    assert [entry.chain for entry in result] == [
        Chain.MAINNET,
        Chain.OPTIMISM,
        Chain.POLYGON,
        Chain.BASE,
        Chain.ARBITRUM,
        Chain.KATANA,
    ]
    assert result[0].lag_seconds == 5 * HOUR
    assert result[0].name == "Mainnet"
    assert result[1].lag_seconds == 120


def test_collect_freshness_skips_chains_without_a_processed_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: NOW)

    result = freshness.collect_freshness(_rows(zero_block=(Chain.KATANA,)), NOW)

    assert Chain.KATANA not in [entry.chain for entry in result]


def test_collect_freshness_marks_lag_unknown_when_rpc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: None)

    result = freshness.collect_freshness(_rows(), NOW)

    assert all(entry.lag_seconds is None for entry in result)
    # Unknown lag must never fire an alert — a broken RPC is not a stale indexer.
    assert not any(entry.is_stale(HOUR) for entry in result)


def test_is_stale_uses_threshold() -> None:
    entry = ChainFreshness(chain=Chain.MAINNET, latest_processed_block=100, lag_seconds=HOUR + 1)

    assert entry.is_stale(HOUR)
    assert not entry.is_stale(2 * HOUR)


def test_missing_chains_covers_absent_and_unsynced_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain dropped from chain_metadata and one with no processed block both count."""
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: NOW - 60)
    rows = _rows(omit=(Chain.KATANA,), zero_block=(Chain.POLYGON,))

    missing = freshness.missing_chains(freshness.collect_freshness(rows, NOW))

    assert missing == [Chain.POLYGON, Chain.KATANA]


def test_missing_chains_empty_when_every_expected_chain_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: NOW - 60)

    assert freshness.missing_chains(freshness.collect_freshness(_rows(), NOW)) == []


def test_build_alert_message_lists_lagging_and_missing_chains() -> None:
    stale = [
        ChainFreshness(chain=Chain.MAINNET, latest_processed_block=24150245, lag_seconds=205 * 86400),
        ChainFreshness(chain=Chain.KATANA, latest_processed_block=38465232, lag_seconds=2 * HOUR + 900),
    ]

    message = freshness.build_alert_message(stale, [Chain.BASE], HOUR)

    assert "problem on 3 chain(s)" in message
    assert "Mainnet (chain 1): 205d behind, last block 24150245" in message
    assert "Katana (chain 747474): 2h 15m behind, last block 38465232" in message
    assert "Base (chain 8453): no sync state reported by the indexer" in message
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
    stale = [ChainFreshness(chain=Chain.MAINNET, latest_processed_block=1, lag_seconds=2 * HOUR)]
    missing = [Chain.KATANA]

    assert freshness.chains_to_alert(stale, missing, NOW, 6 * HOUR) == (stale, missing)

    freshness._set_last_alert_timestamp(Chain.MAINNET.chain_id, NOW)
    freshness._set_last_alert_timestamp(Chain.KATANA.chain_id, NOW)
    assert freshness.chains_to_alert(stale, missing, NOW + HOUR, 6 * HOUR) == ([], [])
    assert freshness.chains_to_alert(stale, missing, NOW + 6 * HOUR, 6 * HOUR) == (stale, missing)


def test_chains_to_alert_keeps_chains_independent() -> None:
    """One chain inside its cooldown must not suppress another's first alert."""
    stale = [ChainFreshness(chain=Chain.MAINNET, latest_processed_block=1, lag_seconds=2 * HOUR)]
    freshness._set_last_alert_timestamp(Chain.MAINNET.chain_id, NOW)

    assert freshness.chains_to_alert(stale, [Chain.BASE], NOW, 6 * HOUR) == ([], [Chain.BASE])


def test_main_alerts_once_per_cooldown_then_reports_recovery(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str]
) -> None:
    monkeypatch.setattr(
        freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"data": {"chain_metadata": _rows()}})
    )
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])

    timestamps = {Chain.MAINNET: NOW - 5 * HOUR}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: timestamps.get(chain, NOW - 120))
    monkeypatch.setattr(freshness.time, "time", lambda: NOW)

    freshness.main()
    assert len(sent) == 1
    assert "Mainnet" in sent[0]
    assert "Base" not in sent[0]

    # Second run inside the cooldown window stays quiet.
    freshness.main()
    assert len(sent) == 1

    # Once mainnet catches up, the recovery note fires and clears the state.
    timestamps[Chain.MAINNET] = NOW - 60
    freshness.main()
    assert len(sent) == 2
    assert "caught up" in sent[1]

    freshness.main()
    assert len(sent) == 2


def test_main_alerts_when_an_expected_chain_is_absent(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str]
) -> None:
    """Rows present but an expected chain missing must not read as "all fresh"."""
    rows = _rows(omit=(Chain.KATANA,))
    monkeypatch.setattr(
        freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"data": {"chain_metadata": rows}})
    )
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])
    # Every chain the indexer *does* report is perfectly fresh.
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: NOW - 60)
    monkeypatch.setattr(freshness.time, "time", lambda: NOW)

    freshness.main()

    assert len(sent) == 1
    assert "Katana (chain 747474): no sync state reported by the indexer" in sent[0]


def test_main_stays_quiet_when_every_expected_chain_is_fresh(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str]
) -> None:
    monkeypatch.setattr(
        freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"data": {"chain_metadata": _rows()}})
    )
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])
    monkeypatch.setattr(freshness, "fetch_block_timestamp", lambda chain, block: NOW - 60)
    monkeypatch.setattr(freshness.time, "time", lambda: NOW)

    freshness.main()

    assert sent == []


def test_main_alerts_when_indexer_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])
    monkeypatch.setattr(freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"errors": ["down"]}))

    freshness.main()

    assert len(sent) == 1
    assert "Envio indexer unavailable" in sent[0]


@pytest.mark.parametrize(
    "error",
    [
        # request_with_retry exhausts its retries on 5xx, then raises HTTPError.
        requests.HTTPError("502 Server Error: Bad Gateway"),
        requests.ConnectionError("connection refused"),
        requests.Timeout("read timeout"),
        # Hasura down behind a proxy answers 200 with an HTML error page.
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ],
)
def test_main_alerts_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch, envio_url: str, sent: list[str], error: Exception
) -> None:
    """Every way the endpoint can fail still produces a Telegram alert."""
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(freshness, "request_with_retry", fail)

    freshness.main()

    assert len(sent) == 1
    assert "Envio indexer unavailable" in sent[0]
