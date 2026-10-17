"""When to try a failed request again, and how long to wait first."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
"""Methods that can be repeated safely: doing them twice does what doing them once does."""

TEMPORARY_STATUSES = frozenset({408, 429, *range(500, 600)} - {501, 505})
"""Statuses that may pass on a later try: a timeout, a rate limit, or a
server error. 501 (not implemented) and 505 (HTTP version not supported)
won't change, so they aren't retried."""


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Return the seconds to wait before retry number `attempt` (0 is the first).

    The delay doubles with each retry up to `cap`, then gets ±20% jitter, so
    callers that failed together don't all retry at the same moment.
    """
    return min(base * 2**attempt, cap) * random.uniform(0.8, 1.2)


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Read a Retry-After header, which is either seconds or an HTTP date.

    Returns None when the header is absent or unreadable, and 0 for a date
    already past. `now` defaults to the current time.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


@dataclass(frozen=True)
class Retry:
    """When a failed request is tried again, and how long to wait between tries.

    - `attempts`: tries in all, the first included. At least 1.
    - `statuses`: HTTP statuses worth another try. Network errors and
      timeouts are always worth one.
    - `methods`: methods retried unless a call says otherwise. Only
      idempotent ones by default, because repeating a POST can repeat what
      it did.
    - `backoff`, `max_backoff`: the first wait and the longest (see
      backoff_delay). A Retry-After header can ask for a longer wait, but
      not longer than `max_backoff`: then the request fails at once rather
      than block.
    """

    attempts: int = 3
    backoff: float = 1.0
    max_backoff: float = 30.0
    statuses: frozenset[int] = TEMPORARY_STATUSES
    methods: frozenset[str] = IDEMPOTENT_METHODS

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {self.attempts}")
        if self.backoff < 0 or self.max_backoff < 0:
            raise ValueError("backoff and max_backoff can't be negative")

    def delay(self, retry_number: int) -> float:
        """Seconds to wait before retry number `retry_number` (0 is the first)."""
        return backoff_delay(retry_number, self.backoff, self.max_backoff)
