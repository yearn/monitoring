"""Identify the timelock operation an ``execute``/``executeBatch`` call releases.

A Safe executing a timelock batch carries the real actions as payloads, and
nothing in the calldata says they already sat in a public queue for the
timelock's delay. A report on Yearn's ``TimelockExecutor.executeBatch``
described four strategy changes as if the Safe were making them directly, never
mentioning that the operation was scheduled on the 7-day Yearn
TimelockController — and alerted on — a week earlier.

For OpenZeppelin ``execute``/``executeBatch`` calls this adapter finds the
timelock (the target itself, or the ``TIMELOCK()``/``timelock()`` a forwarding
executor points at), recomputes the operation ID exactly as
``hashOperation``/``hashOperationBatch`` does, and reads its status: not
scheduled (the call reverts), not ready yet, ready, or already executed.
"""

from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.eth_view import call_view
from utils.formatting import format_duration, format_utc
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.source_context import get_contract_label
from utils.web3_wrapper import ChainManager, Web3Client

logger = get_logger("utils.llm.timelock_execution_context")

_EXECUTE = "execute(address,uint256,bytes,bytes32,bytes32)"
_EXECUTE_BATCH = "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)"
_OPERATION_TYPES = {
    _EXECUTE: ["address", "uint256", "bytes", "bytes32", "bytes32"],
    _EXECUTE_BATCH: ["address[]", "uint256[]", "bytes[]", "bytes32", "bytes32"],
}
# Getters a forwarding executor uses to name the timelock it calls.
_TIMELOCK_GETTERS = ("TIMELOCK()", "timelock()")
# OpenZeppelin marks an executed operation's timestamp as 1.
_DONE_TIMESTAMP = 1


@dataclass(frozen=True)
class TimelockExecutionContext:
    """The timelock operation one execute call releases, and its status."""

    target: str
    target_label: str
    timelock: str
    timelock_label: str
    signature: str
    operation_id: str
    call_count: int
    min_delay: int
    ready_at: int  # 0 = not scheduled, 1 = done, otherwise the ready timestamp
    now: int

    @property
    def addresses(self) -> list[str]:
        return [self.target, self.timelock]

    @property
    def labels(self) -> dict[str, str]:
        return {self.timelock: self.timelock_label} if self.timelock_label else {}

    @property
    def via_executor(self) -> bool:
        return self.target.lower() != self.timelock.lower()

    @property
    def ready(self) -> bool:
        """Scheduled, not yet executed, and past its ready time."""
        return _DONE_TIMESTAMP < self.ready_at <= self.now

    def status(self) -> str:
        """Operation status in plain text."""
        if self.ready_at == 0:
            return "NOT SCHEDULED on the timelock — this execute call reverts (or its calldata differs from what was scheduled)"
        if self.ready_at == _DONE_TIMESTAMP:
            return "ALREADY EXECUTED — this execute call reverts"
        ready = format_utc(self.ready_at)
        if self.ready_at > self.now:
            return f"scheduled, NOT READY until {ready} — executing earlier reverts"
        scheduled_by = format_utc(self.ready_at - self.min_delay)
        return f"scheduled and ready since {ready} (so scheduled no later than {scheduled_by})"

    def lines(self) -> list[str]:
        """Plain-text facts for the prompt."""
        timelock = f"{self.timelock} ({self.timelock_label})" if self.timelock_label else self.timelock
        target = f"{self.target} ({self.target_label})" if self.target_label else self.target
        route = f"{target} forwards to timelock {timelock}" if self.via_executor else f"timelock {timelock}"
        calls = "1 call" if self.call_count == 1 else f"{self.call_count} calls"
        lines = [
            f"{self.signature.split('(')[0]} via {route}: releases operation {self.operation_id} ({calls}); "
            f"timelock min delay {format_duration(self.min_delay)}.",
            f"Operation status: {self.status()}.",
        ]
        if self.ready:
            lines.append(
                "The inner calls are not new proposals: they sat publicly in the timelock queue for at least the "
                "delay, and a timelock alert from scheduling carries this operation ID. This call only executes them."
            )
        elif self.ready_at == 0:
            lines.append(
                "No operation with this ID is in the timelock queue, so these inner calls have NOT gone through "
                "the delay as encoded here."
            )
        elif self.ready_at != _DONE_TIMESTAMP:
            lines.append(
                "The delay has NOT elapsed: the inner calls are queued but cannot execute until the ready time."
            )
        return lines


def _find_timelock(client: Web3Client, target: str) -> tuple[str, int] | None:
    """(timelock, min delay) — the target itself, or the timelock a forwarding executor names."""
    delay = call_view(client, target, "getMinDelay()", "uint256")
    if isinstance(delay, int):
        return target, delay
    for getter in _TIMELOCK_GETTERS:
        found = call_view(client, target, getter, "address")
        if isinstance(found, str) and int(found, 16) != 0:
            timelock = to_checksum_address(found)
            delay = call_view(client, timelock, "getMinDelay()", "uint256")
            if isinstance(delay, int):
                return timelock, delay
    return None


def operation_id(call: DecodedCall) -> str:
    """``hashOperation``/``hashOperationBatch`` of an execute call: keccak of its abi-encoded arguments."""
    types = _OPERATION_TYPES[call.signature]
    values = [value for _, value in call.params]
    return "0x" + keccak(abi_encode(types, values)).hex()


def _resolve_one(chain_id: int, client: Web3Client, target: str, call: DecodedCall) -> TimelockExecutionContext | None:
    target = to_checksum_address(target)
    found = _find_timelock(client, target)
    if found is None:
        return None
    timelock, min_delay = found
    op_id = operation_id(call)
    ready_at = call_view(client, timelock, "getTimestamp(bytes32)", "uint256", (bytes.fromhex(op_id[2:]),))
    if not isinstance(ready_at, int):
        return None
    targets = call.params[0][1]
    return TimelockExecutionContext(
        target=target,
        target_label=get_contract_label(chain_id, target),
        timelock=timelock,
        timelock_label=get_contract_label(chain_id, timelock),
        signature=call.signature,
        operation_id=op_id,
        call_count=len(targets) if isinstance(targets, (list, tuple)) else 1,
        min_delay=min_delay,
        ready_at=ready_at,
        now=int(client.eth.get_block("latest")["timestamp"]),
    )


def resolve_timelock_execution_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[TimelockExecutionContext]:
    """Identify the timelock operation behind every OpenZeppelin execute call.

    ``protocol`` is unused: the timelock interface is the same whoever governs.
    """
    del protocol
    candidates = [(target, call) for target, call in targets_and_calls if call.signature in _OPERATION_TYPES]
    if not candidates:
        return []
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))

    contexts: list[TimelockExecutionContext] = []
    for target, call in candidates:
        try:
            context = _resolve_one(chain_id, client, target, call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Timelock execution context failed for %s: %s", target, error)
            continue
        if context is not None and context.operation_id not in {c.operation_id for c in contexts}:
            contexts.append(context)
    return contexts


def format_timelock_execution_prompt(contexts: list[TimelockExecutionContext]) -> str:
    """Render verified timelock-operation facts for the LLM prompt."""
    return "\n\n".join("\n".join(context.lines()) for context in contexts)


def format_timelock_execution_report(
    contexts: list[TimelockExecutionContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic timelock-operation section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        route = (
            f"{address_link(context.target, chain_id, labels)} → {address_link(context.timelock, chain_id, labels)}"
            if context.via_executor
            else address_link(context.timelock, chain_id, labels)
        )
        sections.append(
            "\n".join(
                [
                    f"**Timelock execution:** `{context.signature.split('(')[0]}` via {route}",
                    f"- **Operation ID:** `{context.operation_id}` ({context.call_count} call(s))",
                    f"- **Min delay:** {format_duration(context.min_delay)}",
                    f"- **Status:** {context.status()}",
                ]
            )
        )
    return "\n\n".join(sections)
