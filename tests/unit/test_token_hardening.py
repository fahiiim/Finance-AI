"""
Regression tests for service-token verification.

Each test here corresponds to a way a validly-signed token could otherwise be
abused. They are deliberately negative tests: the value is in proving the
door is shut, not that the happy path works (covered elsewhere).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import AuthenticationError
from app.core.security import _decode_service_token, get_user_context
from tests.conftest import make_service_token


def test_token_without_exp_is_rejected():
    """A token with no expiry would otherwise be valid forever."""
    token = make_service_token("user-A", "session-1", expires_in=None)
    with pytest.raises(AuthenticationError, match="missing a required claim"):
        _decode_service_token(token)


def test_expired_token_is_rejected():
    token = make_service_token("user-A", "session-1", expires_in=-60)
    with pytest.raises(AuthenticationError, match="expired"):
        _decode_service_token(token)


def test_token_signed_with_wrong_secret_is_rejected():
    token = make_service_token("user-A", "session-1", secret="an-entirely-different-secret-value-32b")
    with pytest.raises(AuthenticationError):
        _decode_service_token(token)


def test_absurdly_long_lived_token_is_rejected():
    """
    Defence in depth: even correctly signed, a year-long service assertion is
    not something this service should honour.
    """
    token = make_service_token("user-A", "session-1", expires_in=365 * 24 * 3600)
    with pytest.raises(AuthenticationError, match="lifetime"):
        _decode_service_token(token)


def test_token_within_max_lifetime_is_accepted():
    token = make_service_token("user-A", "session-1", expires_in=300)
    payload = _decode_service_token(token)
    assert payload["sub"] == "user-A"


def test_token_carrying_aud_still_works_when_no_audience_configured():
    """
    PyJWT rejects a token bearing `aud` unless an expected audience is given.
    Deployments whose issuer sets `aud` must not break just because this
    service has not configured one.
    """
    token = make_service_token("user-A", "session-1", extra_claims={"aud": "some-other-service"})
    assert _decode_service_token(token)["sub"] == "user-A"


@pytest.mark.asyncio
async def test_clock_skew_within_leeway_is_tolerated():
    """A token that expired 5s ago is accepted; the backend's clock may drift."""
    token = make_service_token(
        "user-A", "session-1", issued_at=datetime.now(UTC) - timedelta(seconds=305), expires_in=300
    )
    context = await get_user_context(authorization=f"Bearer {token}", x_session_id="session-1")
    assert context.user_id == "user-A"


@pytest.mark.asyncio
async def test_user_id_comes_from_token_not_headers():
    """The trusted identity is the `sub` claim — nothing else can override it."""
    token = make_service_token("real-user", "session-1")
    context = await get_user_context(authorization=f"Bearer {token}", x_session_id="session-1")
    assert context.user_id == "real-user"
