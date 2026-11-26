"""Request guards for the alerts API: per-client rate limiting and a concurrency cap.

Caddy is the first line of defence (TLS, bearer tokens, timeouts), but the stdlib
server behind it spawns one thread per connection with no limits of its own. These
guards bound what any single client, or a token holder gone rogue, can make it do.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class RateLimiter:
    """Token-bucket rate limiter keyed by client.

    Each client may burst up to ``max_requests`` and then refills at
    ``max_requests / window_seconds`` per second. Tracks at most ``max_clients``
    buckets; the least recently seen client is evicted first, so memory stays
    bounded no matter how many distinct clients appear.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a limiter.

        Args:
            max_requests: Requests allowed per window (also the burst size). Must be positive.
            window_seconds: Window length in seconds. Must be positive.
            max_clients: Most client buckets kept in memory.
            clock: Monotonic time source, injectable for tests.

        Raises:
            ValueError: If ``max_requests`` or ``window_seconds`` is not positive.
        """
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be positive")
        self._capacity = float(max_requests)
        self._refill_per_second = max_requests / window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float:
        """Spend one request for ``key``.

        Returns:
            0.0 when the request is allowed, else the seconds until it would be.
        """
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
            retry_after = 0.0
            if tokens >= 1:
                tokens -= 1
            else:
                retry_after = (1 - tokens) / self._refill_per_second
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_clients:
                self._buckets.popitem(last=False)
            return retry_after


class ConcurrencyGate:
    """Caps in-flight requests; callers over the cap are turned away, not queued."""

    def __init__(self, max_in_flight: int) -> None:
        """Create a gate admitting at most ``max_in_flight`` concurrent requests.

        Raises:
            ValueError: If ``max_in_flight`` is not positive.
        """
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        self._semaphore = threading.BoundedSemaphore(max_in_flight)

    def try_enter(self) -> bool:
        """Claim a slot without blocking. Returns False when the gate is full."""
        return self._semaphore.acquire(blocking=False)

    def leave(self) -> None:
        """Release a slot claimed by :meth:`try_enter`."""
        self._semaphore.release()


def client_key(forwarded_for: str | None, peer_host: str) -> str:
    """Identify the client a request is rate limited as.

    The API only listens on loopback behind Caddy. Caddy discards any client-sent
    ``X-Forwarded-For`` (no ``trusted_proxies`` configured) and sets it to the real
    peer address, so its rightmost entry is the client. Direct local callers send no
    header and are keyed by their socket address.

    Args:
        forwarded_for: The request's ``X-Forwarded-For`` header, if any.
        peer_host: Host of the TCP peer (Caddy, or a local caller).
    """
    if forwarded_for:
        last = forwarded_for.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return peer_host
