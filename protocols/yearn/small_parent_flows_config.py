"""Chain-scoped transaction initiator exclusions for small parent-vault flows."""

from utils.chains import Chain

# Match transactionFrom only, not a shared router or an event's recipient.
IGNORED_TRANSACTION_SENDERS: dict[int, frozenset[str]] = {
    Chain.KATANA.chain_id: frozenset({"0xe9312419edc29e7784ac2f5aa2fd2ac0f38ff2af"}),
}
