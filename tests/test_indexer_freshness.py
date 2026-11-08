"""Tests for the Envio indexer freshness monitor."""

import re

import pytest
import requests

from protocols.yearn import check_indexer_freshness as freshness
from protocols.yearn.check_indexer_freshness import (
    BlockTimestampUnavailableError,
    ChainFreshness,
    IndexerUnavailableError,
)
from utils.chains import Chain

NOW = 1_800_000_000
HOUR = 3600

# Last processed block per expected chain, roughly as the live indexer reports them.
INDEXED_BLOCKS: dict[Chain, int] = {
    Chain.BASE: 49220190,
    Chain.MAINNET: 24150245,
    Chain.KATANA: 38465232,
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


def _unavailable(chain: Chain, block: int) -> int:
    raise BlockTimestampUnavailableError("block not found (RPC returned a null result)")


def test_collect_freshness_marks_lag_unknown_when_rpc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "fetch_block_timestamp", _unavailable)

    result = freshness.collect_freshness(_rows(), NOW)

    assert all(entry.lag_seconds is None for entry in result)
    assert all("null result" in (entry.unavailable_reason or "") for entry in result)
    # Unknown lag must never fire an alert — a broken RPC is not a stale indexer.
    assert not any(entry.is_stale(HOUR) for entry in result)
    # Nor is it missing: the indexer did report sync state.
    assert freshness.missing_chains(result) == []


def test_collect_freshness_keeps_other_chains_when_one_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(chain: Chain, block: int) -> int:
        if chain == Chain.KATANA:
            return _unavailable(chain, block)
        return NOW - 120

    monkeypatch.setattr(freshness, "fetch_block_timestamp", lookup)

    result = {entry.chain: entry for entry in freshness.collect_freshness(_rows(), NOW)}

    assert result[Chain.KATANA].lag_seconds is None
    assert all(result[chain].lag_seconds == 120 for chain in result if chain != Chain.KATANA)


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
    assert "envio-monitoring" not in message


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


def test_main_unavailable_alert_notifies(monkeypatch: pytest.MonkeyPatch, envio_url: str) -> None:
    """An unreachable indexer blinds every Envio monitor, so the alert must not be silent."""
    calls: list[dict] = []
    monkeypatch.setattr(freshness, "send_envio_error_message", lambda msg, protocol, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])
    monkeypatch.setattr(freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"errors": ["down"]}))

    freshness.main()

    assert len(calls) == 1
    assert calls[0]["disable_notification"] is False
    assert calls[0]["alert_protocol"] == "yearn-internal"


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


# --- Block timestamp resolution -------------------------------------------------


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ({"jsonrpc": "2.0", "id": 1, "result": None}, "null result"),
        ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "header not found"}}, "-32000"),
        ({"jsonrpc": "2.0", "id": 1, "error": "rate limited"}, "rate limited"),
        ({"jsonrpc": "2.0", "id": 1, "result": {"number": "0x1"}}, "malformed block timestamp None"),
        ({"jsonrpc": "2.0", "id": 1, "result": {"timestamp": "zz"}}, "malformed block timestamp 'zz'"),
        ({"jsonrpc": "2.0", "id": 1, "result": "0x1"}, "malformed block (str result)"),
        (None, "malformed RPC response (NoneType)"),
    ],
)
def test_parse_block_timestamp_rejects_unusable_responses(response: object, reason: str) -> None:
    with pytest.raises(BlockTimestampUnavailableError, match=re.escape(reason)):
        freshness.parse_block_timestamp(response)


class FakeClient:
    """Stands in for Web3Client, replaying one scripted response per request."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests = 0
        self.rotations = 0

    def make_request(self, method: str, params: list) -> object:
        response = self.responses[self.requests]
        self.requests += 1
        if isinstance(response, Exception):
            raise response
        return response

    def rotate_provider(self) -> None:
        self.rotations += 1


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness, "BLOCK_TIMESTAMP_RETRY_DELAY_SECONDS", 0)


def _use_client(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(freshness.ChainManager, "get_client", classmethod(lambda cls, chain: client))


def test_fetch_block_timestamp_retries_null_result_on_another_provider(
    monkeypatch: pytest.MonkeyPatch, no_retry_delay: None
) -> None:
    """The observed Katana failure: a provider that hasn't seen the block yet answers null."""
    client = FakeClient([{"result": None}, {"result": {"timestamp": hex(NOW)}}])
    _use_client(monkeypatch, client)

    assert freshness.fetch_block_timestamp(Chain.KATANA, 47317489) == NOW
    assert client.requests == 2
    assert client.rotations == 1


def test_fetch_block_timestamp_gives_up_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch, no_retry_delay: None
) -> None:
    client = FakeClient([{"result": None}] * 10)
    _use_client(monkeypatch, client)

    with pytest.raises(BlockTimestampUnavailableError, match=r"null result\) \(after 3 attempts\)"):
        freshness.fetch_block_timestamp(Chain.KATANA, 47317489)
    assert client.requests == freshness.BLOCK_TIMESTAMP_ATTEMPTS


def test_fetch_block_timestamp_keeps_transport_error_text_out_of_the_reason(
    monkeypatch: pytest.MonkeyPatch, no_retry_delay: None
) -> None:
    """make_request already rotated every provider; don't retry again, and never leak the key.

    The reason is forwarded to Telegram, and urllib3 reports the request path without a scheme.
    """
    error = ConnectionError(
        "HTTPSConnectionPool(host='rpc.example.com', port=443): Max retries exceeded with url: /v2/SECRET_KEY "
        "(Caused by NewConnectionError('Failed to establish a new connection'))"
    )
    client = FakeClient([error])
    _use_client(monkeypatch, client)

    with pytest.raises(BlockTimestampUnavailableError) as excinfo:
        freshness.fetch_block_timestamp(Chain.KATANA, 47317489)

    assert "SECRET_KEY" not in str(excinfo.value)
    assert str(excinfo.value) == "RPC request failed (ConnectionError), see monitor logs"
    assert client.requests == 1


def test_fetch_block_timestamp_reports_missing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(cls, chain: Chain) -> None:
        raise ValueError(f"No providers found for chain {chain.name}")

    monkeypatch.setattr(freshness.ChainManager, "get_client", classmethod(no_client))

    with pytest.raises(BlockTimestampUnavailableError, match="No providers found"):
        freshness.fetch_block_timestamp(Chain.KATANA, 1)


# --- Unknown coverage: notification and recovery ----------------------------------


@pytest.fixture
def indexer_rows(monkeypatch: pytest.MonkeyPatch, envio_url: str) -> None:
    monkeypatch.setattr(
        freshness, "request_with_retry", lambda *a, **kw: FakeResponse({"data": {"chain_metadata": _rows()}})
    )
    monkeypatch.setattr("sys.argv", ["check_indexer_freshness.py"])
    monkeypatch.setattr(freshness.time, "time", lambda: NOW)


def _katana_lookup(state: dict[str, int | None]):
    """Katana resolves to `state["katana"]` (None = unavailable); every other chain is fresh."""

    def lookup(chain: Chain, block: int) -> int:
        if chain == Chain.KATANA:
            timestamp = state["katana"]
            if timestamp is None:
                return _unavailable(chain, block)
            return timestamp
        return NOW - 60

    return lookup


def test_main_unknown_lag_alerts_only_once_it_persists_then_reports_recovery(
    monkeypatch: pytest.MonkeyPatch, indexer_rows: None, sent: list[str]
) -> None:
    state: dict[str, int | None] = {"katana": None}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", _katana_lookup(state))

    # A single unmeasurable run is a blip: logged, not alerted.
    freshness.main()
    assert sent == []

    # The second consecutive one is an hour blind: alert as unknown, not stale.
    freshness.main()
    assert len(sent) == 1
    assert "freshness unknown on 1 chain(s)" in sent[0]
    assert "neither confirmed fresh nor stale" in sent[0]
    assert "Katana (chain 747474): unknown for 2 consecutive check(s), last indexed block 38465232" in sent[0]
    assert "null result" in sent[0]

    # Still unknown inside the cooldown: quiet.
    freshness.main()
    assert len(sent) == 1

    # Measurable again: one recovery note, then quiet.
    state["katana"] = NOW - 60
    freshness.main()
    assert len(sent) == 2
    assert sent[1] == "Envio indexer lag measurable again: Katana (1m behind)"
    freshness.main()
    assert len(sent) == 2


def test_main_isolated_unknown_run_never_alerts(
    monkeypatch: pytest.MonkeyPatch, indexer_rows: None, sent: list[str]
) -> None:
    """The 2026-11-07 Katana case: one failed lookup, fine next run — the streak resets."""
    state: dict[str, int | None] = {"katana": None}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", _katana_lookup(state))

    freshness.main()
    state["katana"] = NOW - 60
    freshness.main()
    state["katana"] = None
    freshness.main()

    assert sent == []


def test_main_unknown_lag_does_not_clear_stale_alert_state(
    monkeypatch: pytest.MonkeyPatch, indexer_rows: None, sent: list[str]
) -> None:
    """A stale chain that becomes unmeasurable has not recovered; it must not claim to have caught up."""
    state: dict[str, int | None] = {"katana": NOW - 5 * HOUR}
    monkeypatch.setattr(freshness, "fetch_block_timestamp", _katana_lookup(state))

    freshness.main()
    assert len(sent) == 1
    assert "Katana (chain 747474): 5h behind" in sent[0]

    state["katana"] = None
    freshness.main()
    assert len(sent) == 1
    assert freshness._last_alert_timestamp(Chain.KATANA.chain_id) == NOW

    # Back and caught up: now the stale recovery fires.
    state["katana"] = NOW - 60
    freshness.main()
    assert len(sent) == 2
    assert "caught up: Katana" in sent[1]
