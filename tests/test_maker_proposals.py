from unittest.mock import patch

from maker.proposals import get_proposals


def test_maker_alerts_scheduled_uncast_executives_and_escapes_markdown():
    proposals = [
        {
            "key": "scheduled",
            "title": "Update PSM_USDC_A *rates*",
            "proposalBlurb": "This touches TYPE_1 params and [links].",
            "date": "2026-05-01T00:00:00.000Z",
            "spellData": {"hasBeenScheduled": True, "hasBeenCast": False, "eta": "2026-05-02T00:00:00.000Z"},
        },
        {
            "key": "cast",
            "title": "Already cast",
            "date": "2026-05-02T00:00:00.000Z",
            "spellData": {"hasBeenScheduled": True, "hasBeenCast": True},
        },
        {
            "key": "unscheduled",
            "title": "Not scheduled",
            "date": "2026-05-03T00:00:00.000Z",
            "spellData": {"hasBeenScheduled": False, "hasBeenCast": False},
        },
    ]

    with (
        patch("maker.proposals.fetch_executive_proposals", return_value=proposals),
        patch("maker.proposals.get_last_queued_id_from_file", return_value=0),
        patch("maker.proposals.send_telegram_message") as mock_send,
        patch("maker.proposals.write_last_queued_id_to_file") as mock_write,
    ):
        get_proposals()

    mock_send.assert_called_once()
    message, protocol = mock_send.call_args.args
    assert protocol == "maker"
    assert "Update PSM\\_USDC\\_A \\*rates\\*" in message
    assert "TYPE\\_1 params and \\[links]." in message
    assert "Already cast" not in message
    assert "Not scheduled" not in message
    mock_write.assert_called_once_with("maker", 1777593600)


def test_maker_does_not_cache_when_only_new_executives_are_not_actionable():
    proposals = [
        {
            "key": "unscheduled",
            "title": "Not scheduled",
            "date": "2026-05-03T00:00:00.000Z",
            "spellData": {"hasBeenScheduled": False, "hasBeenCast": False},
        }
    ]

    with (
        patch("maker.proposals.fetch_executive_proposals", return_value=proposals),
        patch("maker.proposals.get_last_queued_id_from_file", return_value=0),
        patch("maker.proposals.send_telegram_message") as mock_send,
        patch("maker.proposals.write_last_queued_id_to_file") as mock_write,
    ):
        get_proposals()

    mock_send.assert_not_called()
    mock_write.assert_not_called()
