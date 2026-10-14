from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import AuthenticationError, UserContextError
from app.core.security import get_user_context
from tests.conftest import make_service_token


@pytest.mark.asyncio
async def test_valid_token_establishes_user_context():
    token = make_service_token("user-42", "session-1")
    context = await get_user_context(authorization=f"Bearer {token}", x_session_id=None)
    assert context.user_id == "user-42"
    assert context.session_id == "session-1"


@pytest.mark.asyncio
async def test_missing_authorization_header_rejected():
    with pytest.raises(AuthenticationError):
        await get_user_context(authorization=None, x_session_id=None)


@pytest.mark.asyncio
async def test_malformed_authorization_header_rejected():
    with pytest.raises(AuthenticationError):
        await get_user_context(authorization="NotBearer abc", x_session_id=None)


@pytest.mark.asyncio
async def test_tampered_token_rejected():
    token = make_service_token("user-42", "session-1", secret="wrong-secret")
    with pytest.raises(AuthenticationError):
        await get_user_context(authorization=f"Bearer {token}", x_session_id=None)


@pytest.mark.asyncio
async def test_token_missing_session_id_rejected():
    """
    A token that is otherwise VALID (signed, unexpired) but carries no
    session_id is a UserContextError, not an AuthenticationError — the caller
    authenticated fine, they just cannot be given a usable context. The token
    must include `exp` here or it would fail authentication first and this
    test would pass for the wrong reason.
    """
    import jwt

    from app.core.config import settings

    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": "user-42", "iat": now, "exp": now + timedelta(seconds=300)},
        settings.service_jwt_secret,
        algorithm=settings.service_jwt_algorithm,
    )
    with pytest.raises(UserContextError):
        await get_user_context(authorization=f"Bearer {token}", x_session_id=None)


@pytest.mark.asyncio
async def test_a_message_body_cannot_impersonate_another_user():
    """
    Shows the security invariant directly: even if a malicious body
    contained something like {"user_id": "victim"}, ChatRequest has no such
    field (see test_user_isolation.py), and identity here comes only from
    the token — a forged body value has literally nowhere to plug in.
    """
    token = make_service_token("attacker", "session-1")
    context = await get_user_context(authorization=f"Bearer {token}", x_session_id=None)
    assert context.user_id == "attacker"  # the token's identity, not anything from a body
