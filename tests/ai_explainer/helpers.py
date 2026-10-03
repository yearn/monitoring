"""Small builders shared by the explainer tests."""

from unittest.mock import MagicMock

from utils.calldata.decoder import DecodedCall

PAUSE = DecodedCall(function_name="pause", signature="pause()")
UNKNOWN_DATA = "0xdeadbeef"
PAUSE_DATA = "0x8456cb59"


def make_provider(response: str, model_name: str = "test") -> MagicMock:
    """Build a text-only provider with a fixed completion."""
    provider = MagicMock()
    provider.supports_structured_output = False
    provider.complete.return_value = response
    provider.model_name = model_name
    return provider


def make_address(i: int) -> str:
    """Build a deterministic address from an integer."""
    return "0x" + f"{i:040x}"


def make_set_cap(key: str, cap: int = 1) -> DecodedCall:
    """Build a decoded cap setter for an address key."""
    return DecodedCall(
        function_name="setCap",
        signature="setCap(address,uint256)",
        params=[("address", key), ("uint256", cap)],
    )
