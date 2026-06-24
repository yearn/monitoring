from unittest.mock import patch

from protocols.maple import collateral


def test_proof_of_reserves_alerts_when_syrup_globals_exceeds_por() -> None:
    with (
        patch.object(collateral, "fetch_proof_of_reserves", return_value=998.0),
        patch.object(collateral, "fetch_syrup_globals", return_value={"collateralValue": 1000.0}),
        patch.object(collateral, "send_alert") as send_alert,
    ):
        collateral.check_proof_of_reserves()

    send_alert.assert_called_once()
    alert = send_alert.call_args.args[0]
    assert "syrupGlobals exceeds PoR by: 0.20%" in alert.message


def test_proof_of_reserves_does_not_alert_when_por_exceeds_syrup_globals() -> None:
    with (
        patch.object(collateral, "fetch_proof_of_reserves", return_value=1002.0),
        patch.object(collateral, "fetch_syrup_globals", return_value={"collateralValue": 1000.0}),
        patch.object(collateral, "send_alert") as send_alert,
    ):
        collateral.check_proof_of_reserves()

    send_alert.assert_not_called()


def test_proof_of_reserves_does_not_alert_below_directional_threshold() -> None:
    with (
        patch.object(collateral, "fetch_proof_of_reserves", return_value=999.5),
        patch.object(collateral, "fetch_syrup_globals", return_value={"collateralValue": 1000.0}),
        patch.object(collateral, "send_alert") as send_alert,
    ):
        collateral.check_proof_of_reserves()

    send_alert.assert_not_called()
