from __future__ import annotations

import os

os.environ.setdefault("SERVICE_JWT_SECRET", "test-secret-do-not-use-in-production-32bytes")
os.environ.setdefault("BACKEND_INTEGRATION_MODE", "backend_api")
os.environ.setdefault("BACKEND_API_BASE_URL", "https://backend.test.invalid/MyFi/api")
os.environ.setdefault("BACKEND_API_SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
# Tests must not depend on a developer's local .env: strict ownership is a
# deployment setting, and the tests that need it switch it on themselves.
os.environ["STRICT_DATA_OWNERSHIP"] = "false"

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings, settings
from app.core.security import UserContext
from app.llm.base import LLMProvider
from app.schemas.planner import RawExecutionPlan
from tests.support.in_memory_backend import InMemoryBackendClient


@pytest.fixture(autouse=True, scope="session")
def _clear_settings_cache():
    get_settings.cache_clear()


def make_user_context(user_id: str = "user-1", session_id: str = "session-1", timezone: str = "UTC") -> UserContext:
    return UserContext(user_id=user_id, session_id=session_id, timezone=timezone, currency="USD")


def make_service_token(
    user_id: str,
    session_id: str,
    *,
    secret: str | None = None,
    expires_in: int | None = 300,
    issued_at: datetime | None = None,
    extra_claims: dict | None = None,
) -> str:
    """
    Mint a service token shaped like the real backend's.

    `exp` is included by default because the service now REQUIRES it; pass
    expires_in=None to build a non-expiring token for negative tests, or a
    negative value for an already-expired one.
    """
    now = issued_at or datetime.now(UTC)
    payload: dict = {
        "sub": user_id,
        "session_id": session_id,
        "timezone": "UTC",
        "currency": "USD",
        "iat": now,
    }
    if expires_in is not None:
        payload["exp"] = now + timedelta(seconds=expires_in)
    payload.update(extra_claims or {})
    return jwt.encode(payload, secret or settings.service_jwt_secret, algorithm=settings.service_jwt_algorithm)


@pytest.fixture
def in_memory_backend():
    return InMemoryBackendClient()


class FakeLLMProvider(LLMProvider):
    """
    Deterministic test double. `structured_generate` returns a canned
    RawExecutionPlan (or a queued sequence of them); `generate` returns a
    canned string echoing what it was given, so tests can assert on prompt
    content without hitting the real OpenAI API.
    """

    def __init__(self, plans: list[RawExecutionPlan] | None = None, response_text: str = "Here's your answer.") -> None:
        self._plans = list(plans or [])
        self._response_text = response_text
        self.last_structured_messages: list[dict] | None = None
        self.last_generate_messages: list[dict] | None = None

    async def generate(self, *, system_prompt, messages, temperature=0.3, max_tokens=700) -> str:
        self.last_generate_messages = messages
        return self._response_text

    async def structured_generate(self, *, system_prompt, messages, response_schema, temperature=0.0, max_tokens=500):
        self.last_structured_messages = messages
        if not self._plans:
            raise AssertionError("FakeLLMProvider.structured_generate called with no queued plan.")
        return self._plans.pop(0)


@pytest.fixture
def fake_llm():
    return FakeLLMProvider()
