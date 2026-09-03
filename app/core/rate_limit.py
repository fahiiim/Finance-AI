"""
Per-user request rate limiting.

WHY THIS EXISTS
    A single chat turn costs 2-3 OpenAI calls. Without a cap, one user — or
    one buggy client in a retry loop — can run up an unbounded bill and
    saturate the provider's rate limit for everyone else. `settings`
    has carried `rate_limit_requests_per_minute` since the beginning; nothing
    read it, so the limit existed only on paper.

KEYED BY user_id, NOT BY IP
    Requests arrive server-to-server from the backend, so every caller shares
    the backend's IP. Limiting by IP would throttle the whole product the
    moment one user misbehaves. The trusted `user_id` from the verified token
    is the only meaningful key here — and it cannot be spoofed, because it
    comes from the same UserContext everything else is scoped by.

SCOPE AND LIMITS
    This is an in-process limiter: each service instance enforces its own
    window, so N instances allow roughly N times the configured rate. That is
    an acceptable, well-understood approximation for protecting an LLM budget
    — it turns "unbounded" into "bounded by a known factor". A globally exact
    limit needs shared state (Redis) and belongs with the same work that
    moves conversation storage out of memory.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque

from app.core.config import settings
from app.core.exceptions import RateLimitedError
from app.core.security import UserContext

WINDOW_SECONDS = 60.0

#: Cap on tracked users, so an attacker cycling through user ids cannot grow
#: this dict without bound. Least-recently-seen entries are evicted first.
MAX_TRACKED_USERS = 10_000


class SlidingWindowRateLimiter:
    """Fixed-memory sliding window: one timestamp deque per active user."""

    def __init__(self, *, limit_per_minute: int | None = None) -> None:
        self._limit = limit_per_minute
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def limit(self) -> int:
        # Read through to settings each call so the limit can be changed by
        # configuration without rebuilding the limiter.
        return self._limit if self._limit is not None else settings.rate_limit_requests_per_minute

    def check(self, user_id: str, *, now: float | None = None) -> None:
        """Record a request, or raise RateLimitedError if over the limit."""
        limit = self.limit
        if limit <= 0:  # 0 or negative disables limiting entirely
            return

        now = time.monotonic() if now is None else now
        cutoff = now - WINDOW_SECONDS

        timestamps = self._hits.get(user_id)
        if timestamps is None:
            timestamps = deque()
            self._hits[user_id] = timestamps
        self._hits.move_to_end(user_id)

        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= limit:
            retry_after = max(1, int(timestamps[0] + WINDOW_SECONDS - now) + 1)
            raise RateLimitedError(
                "Too many requests. Please wait a moment and try again.",
                details={"retry_after_seconds": retry_after},
            )

        timestamps.append(now)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while len(self._hits) > MAX_TRACKED_USERS:
            self._hits.popitem(last=False)

    def reset(self) -> None:
        self._hits.clear()


_limiter = SlidingWindowRateLimiter()


def get_rate_limiter() -> SlidingWindowRateLimiter:
    return _limiter


def enforce_rate_limit(context: UserContext) -> None:
    """Raise RateLimitedError when this user has exceeded their allowance."""
    _limiter.check(context.user_id)
