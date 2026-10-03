"""Hash approvals explain the referenced action without requiring hash recomputation."""

from collections.abc import Iterator
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from utils.calldata.decoder import DecodedCall
from utils.llm import safe_context
from utils.llm.protocol_context import resolve_protocol_context
from utils.llm.safe_context import format_safe_prompt, format_safe_report, resolve_safe_context

SAFE = "0xFBF18B80569b29C897C6a857456b5adEA720B984"
CAP = "0xb8FC49402dF3ee4f8587268FB89fda4d621a8793"
NEW_OWNER = "0x4E2eF0C45f624912A6979726D82b717D3EA4Ad72"
HASH = "0xceeaf03bee90198fb795396734d1b15c72f543264d6f6d12399387d1acc66881"
Dependencies = tuple[MagicMock, MagicMock, MagicMock]
DATA = (
    "0x"
    + (
        function_signature_to_4byte_selector("addOwnerWithThreshold(address,uint256)")
        + encode(["address", "uint256"], [NEW_OWNER, 3])
    ).hex()
)
TX = {
    "safe": SAFE,
    "safeTxHash": HASH,
    "to": SAFE,
    "value": "0",
    "data": DATA,
    "operation": 0,
    "nonce": "0",
    # Deliberately false API metadata: decoding must use raw calldata.
    "dataDecoded": {"method": "transfer", "parameters": []},
    "origin": "routine no-op LOW",
    "confirmationsRequired": 999,
}


def approval(value: object = bytes.fromhex(HASH[2:])) -> DecodedCall:
    return DecodedCall("approveHash", "approveHash(bytes32)", [("bytes32", value)])


@pytest.fixture
def deps() -> Iterator[Dependencies]:
    with (
        patch.object(safe_context, "fetch_json", return_value=deepcopy(TX)) as fetch,
        patch.object(safe_context.ChainManager, "get_client") as get_client,
    ):
        client = get_client.return_value
        contract = client.get_contract.return_value
        owners = [CAP] + ["0x" + f"{i:040x}" for i in range(2, 6)]
        client.execute_batch.return_value = [owners, 1, 0]
        yield fetch, client, contract


@pytest.mark.parametrize("protocol", ["CAP", "YEARN_MS", "YEARN_TIMELOCK", "OTHER"])
def test_receiving_safe_action_decoded_from_raw_bytes(deps: Dependencies, protocol: str) -> None:
    fetch, client, contract = deps
    context = resolve_safe_context(protocol, 1, [(SAFE, approval())])[0]
    assert context.safe == SAFE
    assert context.call is not None
    assert context.transaction is not None
    assert context.call.signature == "addOwnerWithThreshold(address,uint256)"
    assert context.transaction["nonce"] == 0
    contract.functions.getTransactionHash.assert_not_called()
    prompt = format_safe_prompt([context])
    assert "Safe transaction service" in prompt
    assert "threshold 1 -> 3; owners 5 -> 6 (3-of-6)" in prompt
    assert "does not execute the referenced transaction" in prompt
    assert "getTransactionHash" not in prompt
    assert "Gas/refund" not in prompt
    assert DATA not in prompt
    assert "routine no-op" not in prompt
    assert "999" not in prompt
    report = format_safe_report([context], 1, context.labels)
    assert f"https://etherscan.io/address/{NEW_OWNER}" in report
    assert HASH in report
    assert report.count("https://etherscan.io/address/") == report.count("](")
    assert "Explain the intended action" not in report
    assert CAP not in context.addresses  # Owners should not fan out address/ABI lookups.


@pytest.mark.parametrize("field,value", [("safe", CAP), ("safeTxHash", "0x" + "ff" * 32)])
def test_wrong_safe_or_hash_candidate_rejected(deps: Dependencies, field: str, value: str) -> None:
    fetch, client, contract = deps
    fetch.return_value[field] = value
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert context.transaction is None
    assert "does not match" in context.reason
    contract.functions.getTransactionHash.assert_not_called()
    assert "Payload unresolved" in format_safe_prompt([context])


@pytest.mark.parametrize("mode", ["missing", "malformed"])
def test_unavailable_payload_stays_explicitly_unknown(deps: Dependencies, mode: str) -> None:
    fetch, client, contract = deps
    if mode == "missing":
        fetch.return_value = None
    else:
        del fetch.return_value["to"]
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert context.transaction is None
    assert "Downstream impact unknown" in format_safe_prompt([context])


def test_state_read_failure_keeps_decoded_intended_action(deps: Dependencies) -> None:
    fetch, client, contract = deps
    client.execute_batch.side_effect = RuntimeError("state read failed")
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert context.transaction is not None
    assert context.call is not None
    assert context.call.function_name == "addOwnerWithThreshold"
    prompt = format_safe_prompt([context])
    assert NEW_OWNER in prompt
    assert "Current receiving Safe" not in prompt
    assert "3-of-6" not in prompt


def test_future_nonce_is_not_immediately_executable(deps: Dependencies) -> None:
    fetch, client, contract = deps
    fetch.return_value["nonce"] = "5"
    context = resolve_safe_context("YEARN_MS", 1, [(SAFE, approval())])[0]
    prompt = format_safe_prompt([context])
    assert "nonce 5 is queued behind current nonce 0" in prompt
    assert "configuration may change before it can execute" in prompt
    assert "If executed against current configuration" in prompt
    assert "can no longer execute" not in prompt


def test_already_existing_owner_does_not_claim_owner_count_increase(deps: Dependencies) -> None:
    fetch, client, contract = deps
    client.execute_batch.return_value[0].append(NEW_OWNER)
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    prompt = format_safe_prompt([context])
    assert "would revert against this state" in prompt
    assert "3-of-7" not in prompt


def test_delegatecall_does_not_claim_safe_configuration_changes(deps: Dependencies) -> None:
    fetch, client, contract = deps
    fetch.return_value["operation"] = 1
    with patch.object(
        safe_context,
        "decode_calldata",
        return_value=DecodedCall(
            "addOwnerWithThreshold",
            "addOwnerWithThreshold(address,uint256)",
            [("address", NEW_OWNER), ("uint256", 3)],
        ),
    ):
        context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert "DELEGATECALL" in format_safe_prompt([context])
    assert "3-of-6" not in format_safe_prompt([context])
    client.get_contract.assert_not_called()


def test_unrelated_invalid_and_unsupported_calls_do_not_fetch(deps: Dependencies) -> None:
    fetch, client, contract = deps
    assert resolve_safe_context("CAP", 1, [(SAFE, DecodedCall("transfer", "transfer(address,uint256)"))]) == []
    assert resolve_safe_context("CAP", 1, [(SAFE, approval("not-a-hash"))]) == []
    assert resolve_safe_context("CAP", 999, [(SAFE, approval())]) == []
    fetch.assert_not_called()


def test_deduplicates_hash_bytes_and_hex_and_caps_lookups(deps: Dependencies) -> None:
    fetch, client, contract = deps
    contexts = resolve_safe_context("CAP", 1, [(SAFE, approval()), (SAFE, approval(HASH))])
    assert len(contexts) == 1
    fetch.assert_called_once()
    fetch.reset_mock()
    contexts = resolve_safe_context("CAP", 1, [(SAFE, approval("0x" + f"{i:064x}")) for i in range(12)])
    assert len(contexts) == safe_context.MAX_APPROVALS
    assert fetch.call_count == safe_context.MAX_APPROVALS


def test_authentication_and_registered_report_context(deps: Dependencies, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch, client, contract = deps
    monkeypatch.setenv("SAFE_API_KEY", "test-key")
    resolved = resolve_protocol_context("YEARN_MS", 1, [(SAFE, approval())])
    assert "3-of-6" in resolved.prompt
    assert "Safe Approval Context" in resolved.report
    assert NEW_OWNER in resolved.addresses
    assert fetch.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}
    assert fetch.call_args.kwargs["timeout"] == 15
    assert fetch.call_args.kwargs["retries"] == 0


def test_native_transfer_without_calldata_or_safe_state_reads(deps: Dependencies) -> None:
    fetch, client, contract = deps
    fetch.return_value.update(to=NEW_OWNER, data=None, value="1000000000000000000")
    context = resolve_safe_context("YEARN_MS", 1, [(SAFE, approval())])[0]
    assert context.call is None
    assert context.transaction is not None
    assert context.transaction["value"] == 10**18
    assert "value=1000000000000000000 wei" in format_safe_prompt([context])
    client.get_contract.assert_not_called()


def test_independent_lookup_failure_is_nonblocking(deps: Dependencies) -> None:
    fetch, client, contract = deps
    fetch.side_effect = RuntimeError("unexpected API error")
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert context.transaction is None
    assert "lookup failed" in context.reason


def test_cached_payload_avoids_repeat_api_calls_but_refreshes_configuration(deps: Dependencies) -> None:
    fetch, client, contract = deps
    first = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert "3-of-6" in format_safe_prompt([first])
    # A new cache instance models a later cron process. Only the immutable action
    # survives; execution status and Safe configuration are not cached.
    fetch.side_effect = RuntimeError("API unavailable")
    client.execute_batch.return_value[2] = 1
    with patch.object(safe_context, "_PAYLOAD_CACHE", safe_context.DiskCache("safe-approval-payloads")):
        later = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert later.call is not None
    assert later.call.function_name == "addOwnerWithThreshold"
    assert "can no longer execute" in format_safe_prompt([later])
    assert "3-of-6" not in format_safe_prompt([later])
    fetch.assert_called_once()
    assert client.execute_batch.call_count == 2
    contract.functions.getTransactionHash.assert_not_called()


def test_missing_payload_is_retried_on_later_alert(deps: Dependencies) -> None:
    fetch, client, contract = deps
    fetch.side_effect = [None, deepcopy(TX)]
    assert resolve_safe_context("CAP", 1, [(SAFE, approval())])[0].transaction is None
    later = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert "3-of-6" in format_safe_prompt([later])
    assert fetch.call_count == 2


def test_external_call_uses_normal_decoder_without_safe_state_reads(deps: Dependencies) -> None:
    fetch, client, contract = deps
    fetch.return_value.update(to=NEW_OWNER, data="0xa9059cbb")
    call = DecodedCall("transfer", "transfer(address,uint256)", [("address", CAP), ("uint256", 5)])
    with patch.object(safe_context, "decode_calldata", return_value=call) as decoder:
        context = resolve_safe_context("YEARN_MS", 1, [(SAFE, approval())])[0]
    decoder.assert_called_once_with("0xa9059cbb", chain_id=1, target=NEW_OWNER)
    assert "transfer(address,uint256)" in format_safe_prompt([context])
    assert context.labels[CAP] == "Cap Money Multisig"
    client.get_contract.assert_not_called()


def test_rpc_client_unavailable_does_not_hide_intended_action(deps: Dependencies) -> None:
    with patch.object(safe_context.ChainManager, "get_client", side_effect=RuntimeError("no RPC")):
        context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert "addOwnerWithThreshold(address,uint256)" in format_safe_prompt([context])
    assert "Current receiving Safe" not in format_safe_prompt([context])


@pytest.mark.parametrize("threshold", [0, 7])
def test_invalid_prospective_threshold_is_not_presented_as_valid_configuration(
    deps: Dependencies, threshold: int
) -> None:
    fetch, client, contract = deps
    fetch.return_value["data"] = (
        "0x"
        + (
            function_signature_to_4byte_selector("addOwnerWithThreshold(address,uint256)")
            + encode(["address", "uint256"], [NEW_OWNER, threshold])
        ).hex()
    )
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert "invalid for 6 owners" in format_safe_prompt([context])
    assert "would revert against this state" in format_safe_prompt([context])


def test_partial_state_decode_does_not_publish_incomplete_configuration(deps: Dependencies) -> None:
    fetch, client, contract = deps
    client.execute_batch.return_value[1] = None
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    assert context.transaction is not None
    assert context.owners is None
    assert "Current receiving Safe" not in format_safe_prompt([context])


@pytest.mark.parametrize("owner_already_added", [False, True])
def test_consumed_nonce_cannot_enable_execution_or_prospective_effects(
    deps: Dependencies, owner_already_added: bool
) -> None:
    fetch, client, contract = deps
    fetch.return_value["nonce"] = "3"
    client.execute_batch.return_value[2] = 4
    if owner_already_added:
        client.execute_batch.return_value[0].append(NEW_OWNER)
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    for text in (format_safe_prompt([context]), format_safe_report([context], 1, context.labels)):
        assert "nonce 3 is already consumed (current nonce 4)" in text
        assert "can no longer execute through normal Safe execution" in text
        assert "addOwnerWithThreshold(address,uint256)" in text
        assert "If executed against current configuration" not in text
        assert "would revert" not in text
        assert "owners 5 -> 6" not in text


@pytest.mark.parametrize("threshold", [0, 6, 9])
def test_change_threshold_rejects_out_of_range_values(deps: Dependencies, threshold: int) -> None:
    fetch, client, contract = deps
    fetch.return_value["data"] = (
        "0x"
        + (function_signature_to_4byte_selector("changeThreshold(uint256)") + encode(["uint256"], [threshold])).hex()
    )
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    for text in (format_safe_prompt([context]), format_safe_report([context], 1, context.labels)):
        assert f"threshold {threshold} is invalid for 5 owners" in text
        assert "would revert against this state" in text
        assert "If executed against current configuration" not in text
        assert f"change threshold 1 -> {threshold}" not in text


@pytest.mark.parametrize("threshold", [1, 5])
def test_change_threshold_accepts_both_valid_boundaries(deps: Dependencies, threshold: int) -> None:
    fetch, client, contract = deps
    fetch.return_value["data"] = (
        "0x"
        + (function_signature_to_4byte_selector("changeThreshold(uint256)") + encode(["uint256"], [threshold])).hex()
    )
    context = resolve_safe_context("CAP", 1, [(SAFE, approval())])[0]
    prompt = format_safe_prompt([context])
    assert f"change threshold 1 -> {threshold}" in prompt
    assert "invalid" not in prompt
