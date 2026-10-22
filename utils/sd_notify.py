"""Tiny systemd ``sd_notify`` client for ``Type=notify`` services with a watchdog.

Sends datagrams to ``$NOTIFY_SOCKET`` (``READY=1``, ``WATCHDOG=1``, ``STATUS=...``).
Outside systemd the variable is unset and every call is a no-op.
"""

import os
import socket


def notify(*states: str) -> bool:
    """Send state lines to systemd. Returns False when not running under systemd."""
    address = os.getenv("NOTIFY_SOCKET")
    if not address or not states:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]  # abstract namespace socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall("\n".join(states).encode())
    return True
