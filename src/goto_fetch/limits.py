"""Keeping to a rate, so a small or busy server isn't overloaded.

RateLimit is the budget: at most `per_second` requests a second, with up
to `burst` at once after a quiet spell. A Client given one waits for it
before every request it sends, so everything counts: pages, retries and
token requests alike. One RateLimit shared by several clients gives them
one budget between them.

    client = Client(url, rate_limit=RateLimit(2))  # two requests a second
"""

import threading
import time
from collections.abc import Callable


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
