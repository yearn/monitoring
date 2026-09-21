from __future__ import annotations

import pytest

import protocols.yearn.check_timelock_delay as timelock_delay
from utils.alert import Alert


def test_violation_alerts_go_to_public_topic_and_internal_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Alert] = []
    monkeypatch.setattr(timelock_delay, "send_alert", lambda alert, **kwargs: sent.append(alert))

    timelock_delay.send_violation_alerts("msg")

    assert [(a.protocol, a.channel) for a in sent] == [
        ("yearn", "YEARN_TIMELOCK"),
        ("YEARN_TIMELOCK_INTERNAL", ""),
    ]


def test_internal_mirror_still_sent_when_public_send_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Alert] = []

    def fake_send(alert: Alert, **kwargs: object) -> None:
        if alert.channel == "YEARN_TIMELOCK":
            raise RuntimeError("public down")
        sent.append(alert)

    monkeypatch.setattr(timelock_delay, "send_alert", fake_send)

    with pytest.raises(RuntimeError, match="public down"):
        timelock_delay.send_violation_alerts("msg")

    assert [a.protocol for a in sent] == ["YEARN_TIMELOCK_INTERNAL"]
