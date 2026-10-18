"""Keeping to a rate, so a small or busy server isn't overloaded.

RateLimit is the budget: at most `per_second` requests a second, with up
to `burst` at once after a quiet spell. Limited is a Transport that waits
for the budget before each request it sends, so everything a client sends
counts: pages, retries and token requests alike. One RateLimit shared by
several transports gives them one budget between them.

    limit = RateLimit(2)  # two requests a second
    client = Client(url, transport=Limited(Urllib3Transport(), limit))
"""

import threading
import time
from collections.abc import Callable

from .http import Request, Response, Timeout, Transport


class RateLimit:
    """At most `per_second` requests a second, `burst` of them at once.

    Safe across threads: each caller reserves the next free moment under
    a lock and waits outside it, so waiting never holds others up longer
    than the rate does. `clock` and `sleep` are for tests.
    """

    def __init__(
        self,
        per_second: float,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_second <= 0:
            raise ValueError(f"per_second must be more than 0, got {per_second}")
        if burst < 1:
            raise ValueError(f"burst must be at least 1, got {burst}")
        self.per_second = per_second
        self.burst = burst
        self._interval = 1.0 / per_second
        self._clock = clock
        self._sleep = sleep
        self._next = float("-inf")  # the next free moment
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"RateLimit({self.per_second:g}, burst={self.burst})"

    def wait(self) -> float:
        """Wait for the next free moment, and return how long that took."""
        with self._lock:
            now = self._clock()
            # Unused moments add up while quiet, but only to `burst` of them.
            self._next = max(self._next, now - (self.burst - 1) * self._interval)
            delay = max(0.0, self._next - now)
            self._next += self._interval
        if delay:
            self._sleep(delay)
        return delay


class Limited:
    """A Transport that keeps another one to a RateLimit.

    Each send waits for `limit`, then goes through `transport`. close()
    closes `transport`.
    """

    def __init__(self, transport: Transport, limit: RateLimit) -> None:
        self.transport = transport
        self.limit = limit

    def __repr__(self) -> str:
        return f"Limited({self.transport!r}, {self.limit!r})"

    def send(self, request: Request, timeout: Timeout) -> Response:
        """Wait for the rate limit, then send `request`."""
        self.limit.wait()
        return self.transport.send(request, timeout)

    def close(self) -> None:
        """Close the transport it wraps."""
        self.transport.close()
