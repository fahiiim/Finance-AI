"""
Rate limiting tests.

The limit protects an LLM budget: each chat turn costs 2-3 OpenAI calls, so
an unthrottled client in a retry loop is an unbounded bill. It was configured
but unenforced until now.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import RateLimitedError
from app.core.rate_limit import WINDOW_SECONDS, SlidingWindowRateLimiter


def test_requests_under_the_limit_are_allowed():
    limiter = SlidingWindowRateLimiter(limit_per_minute=5)
    for _ in range(5):
        limiter.check("user-A", now=100.0)


def test_exceeding_the_limit_raises():
    limiter = SlidingWindowRateLimiter(limit_per_minute=3)
    for _ in range(3):
        limiter.check("user-A", now=100.0)

    with pytest.raises(RateLimitedError) as exc_info:
        limiter.check("user-A", now=100.0)

    assert exc_info.value.status_code == 429
    assert exc_info.value.details["retry_after_seconds"] > 0


def test_one_users_traffic_does_not_throttle_another():
    """
    The headline property. Requests arrive server-to-server so every caller
    shares the backend's IP — limiting by anything other than the verified
    user_id would let one user throttle the entire product.
    """
    limiter = SlidingWindowRateLimiter(limit_per_minute=2)
    limiter.check("noisy-user", now=100.0)
    limiter.check("noisy-user", now=100.0)

    with pytest.raises(RateLimitedError):
        limiter.check("noisy-user", now=100.0)

    limiter.check("quiet-user", now=100.0)  # must not raise


def test_window_slides_so_the_limit_recovers():
    limiter = SlidingWindowRateLimiter(limit_per_minute=2)
    limiter.check("user-A", now=100.0)
    limiter.check("user-A", now=100.0)

    with pytest.raises(RateLimitedError):
        limiter.check("user-A", now=100.0)

    # Once the original requests age out of the window, capacity returns.
    limiter.check("user-A", now=100.0 + WINDOW_SECONDS + 1)


def test_zero_limit_disables_throttling():
    limiter = SlidingWindowRateLimiter(limit_per_minute=0)
    for _ in range(500):
        limiter.check("user-A", now=100.0)


def test_tracked_user_table_stays_bounded():
    """
    An attacker cycling through user ids must not grow this dict without
    bound — the limiter is a defence, not a new memory-exhaustion vector.
    """
    from app.core import rate_limit

    limiter = SlidingWindowRateLimiter(limit_per_minute=10)
    for i in range(rate_limit.MAX_TRACKED_USERS + 500):
        limiter.check(f"user-{i}", now=100.0)

    assert len(limiter._hits) <= rate_limit.MAX_TRACKED_USERS
