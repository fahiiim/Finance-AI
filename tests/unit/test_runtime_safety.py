"""
Tests for startup configuration guardrails and client lifecycle.

These cover failure modes that only bite in production — a test data source
serving real users, or a connection pool leaked per request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import BackendIntegrationMode, Environment, Settings


def _settings(**overrides) -> Settings:
    base = {
        "environment": Environment.PRODUCTION,
        "backend_integration_mode": BackendIntegrationMode.BACKEND_API,
        "service_jwt_secret": "a" * 40,
        "openai_api_key": "sk-test",
        "debug": False,
        # Accepted deliberately in these fixtures; the guardrail itself is
        # covered by test_production_rejects_in_memory_conversations.
        "allow_in_memory_conversations": True,
        # backend_api mode is unusable without both of these; the guardrails
        # are covered by the two tests below.
        "backend_api_base_url": "https://backend.internal",
        "backend_api_service_token": "service-token",
    }
    base.update(overrides)
    return Settings(**base)


def test_production_rejects_empty_jwt_secret():
    with pytest.raises(RuntimeError, match="SERVICE_JWT_SECRET is empty"):
        _settings(service_jwt_secret="").validate_runtime_safety()


def test_production_rejects_short_hmac_secret():
    """RFC 7518 3.2 — an HS256 key must be at least 32 bytes."""
    with pytest.raises(RuntimeError, match="32 bytes"):
        _settings(service_jwt_secret="tooshort").validate_runtime_safety()


def test_production_rejects_in_memory_conversations():
    """
    Per-process conversation storage means history depends on which instance
    a user lands on, and vanishes on restart. Production must opt in to that
    explicitly rather than discover it under load.
    """
    with pytest.raises(RuntimeError, match="in-memory"):
        _settings(allow_in_memory_conversations=False).validate_runtime_safety()


def test_production_rejects_debug_mode():
    with pytest.raises(RuntimeError, match="DEBUG=true"):
        _settings(debug=True).validate_runtime_safety()


def test_alg_none_is_rejected_in_every_environment():
    with pytest.raises(RuntimeError, match="disables signature verification"):
        _settings(environment=Environment.LOCAL, service_jwt_algorithm="none").validate_runtime_safety()


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.LOCAL])
def test_an_empty_backend_service_token_is_rejected_in_EVERY_environment(environment):
    """
    An empty token still builds a working client and sends "Authorization:
    Bearer " on every call, so the backend rejects every request and the
    assistant answers "your figures are temporarily unavailable" to everything.

    This check first shipped inside the production-only block, and the
    integrating team hit exactly that failure with ENVIRONMENT=local pointed at
    a real backend: the guardrail existed and never ran. The environment says
    how much to TRUST a deployment, not whether its credentials are usable.
    """
    with pytest.raises(RuntimeError, match="BACKEND_API_SERVICE_TOKEN is empty"):
        _settings(environment=environment, backend_api_service_token="").validate_runtime_safety()


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.LOCAL])
def test_a_missing_backend_base_url_is_rejected_in_EVERY_environment(environment):
    with pytest.raises(RuntimeError, match="BACKEND_API_BASE_URL is not set"):
        _settings(environment=environment, backend_api_base_url=None).validate_runtime_safety()


def test_a_local_backend_api_setup_with_credentials_starts_fine():
    """The guard must not block a correctly-configured integration run."""
    _settings(
        environment=Environment.LOCAL,
        backend_api_base_url="https://host/MyFi/api",
        backend_api_service_token="a-token",
    ).validate_runtime_safety()


def test_those_checks_do_not_fire_for_other_integration_modes():
    """A token is meaningless without an HTTP backend; do not demand one."""
    _settings(
        environment=Environment.LOCAL,
        backend_integration_mode=BackendIntegrationMode.READ_ONLY_DB,
        backend_api_service_token="",
        backend_api_base_url=None,
    ).validate_runtime_safety()


def test_an_unknown_integration_mode_is_rejected_rather_than_silently_ignored():
    """A stale or mistyped .env value must fail loudly at startup, not fall back."""
    with pytest.raises(ValueError):
        _settings(backend_integration_mode="not_a_mode")


def test_weak_settings_warn_without_blocking_startup(caplog):
    """
    Whether owner_user_id is emitted is the BACKEND's behaviour, so this
    service cannot know if the setting is wrong -- refusing to start would
    block a deploy over a condition that may be correct. It must still be
    impossible to ship without it having been said.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _settings(strict_data_ownership=False).warn_on_weak_production_settings()
    assert "strict_data_ownership_disabled" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _settings(strict_data_ownership=True).warn_on_weak_production_settings()
    assert "strict_data_ownership_disabled" not in caplog.text


def test_valid_production_config_passes():
    _settings().validate_runtime_safety()  # must not raise


def test_local_config_is_allowed_without_production_secrets():
    """Local dev must stay frictionless — the production guardrails do not apply."""
    Settings(
        environment=Environment.LOCAL,
        backend_integration_mode=BackendIntegrationMode.BACKEND_API,
        backend_api_base_url="https://host/MyFi/api",
        backend_api_service_token="a-token",
        service_jwt_secret="",
    ).validate_runtime_safety()


def test_backend_client_is_a_single_shared_instance():
    """
    One connection pool for the process, not one per request. Before this was
    cached, every /chat call built a new httpx.AsyncClient that was never closed.
    """
    from app.services.backend_client import get_backend_client

    get_backend_client.cache_clear()
    try:
        assert get_backend_client() is get_backend_client()
    finally:
        get_backend_client.cache_clear()


@pytest.mark.asyncio
async def test_backend_client_exposes_aclose():
    """Shutdown must be able to release the pool regardless of active mode."""
    from tests.support.in_memory_backend import InMemoryBackendClient

    await InMemoryBackendClient().aclose()  # no-op, but must exist on the interface


def test_shutdown_releases_client_connection_pools():
    """
    A rolling restart must not leave sockets behind. TestClient used as a
    context manager drives the real lifespan, so this exercises the actual
    startup/shutdown path rather than calling aclose() directly.
    """
    from fastapi.testclient import TestClient

    from app.api.deps import get_llm_provider
    from app.main import app
    from app.services.backend_client import get_backend_client

    closed: list[str] = []
    for obj, name in ((get_backend_client(), "backend"), (get_llm_provider(), "llm")):
        original = obj.aclose

        async def _record(_original=original, _name=name):
            closed.append(_name)
            await _original()

        obj.aclose = _record

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert sorted(closed) == ["backend", "llm"]


def test_empty_cors_origins_does_not_crash_startup():
    """
    `.env.example` ships `CORS_ALLOWED_ORIGINS=`. pydantic-settings decodes list
    fields as JSON BEFORE validators run, so without NoDecode an empty value
    raised JSONDecodeError at import time and the service could not boot at all.
    """
    assert Settings(cors_allowed_origins="").cors_allowed_origins == []


def test_cors_origins_accepts_comma_separated_string():
    parsed = Settings(cors_allowed_origins="https://a.com, https://b.com").cors_allowed_origins
    assert parsed == ["https://a.com", "https://b.com"]


def test_cors_origins_accepts_a_real_list():
    assert Settings(cors_allowed_origins=["https://a.com"]).cors_allowed_origins == ["https://a.com"]


def test_blank_env_values_mean_unset_not_empty_string():
    """
    `.env.example` ships `SERVICE_JWT_AUDIENCE=` and `SERVICE_JWT_ISSUER=`.
    These fields switch behaviour on `is None`, so a blank value arriving as ""
    silently enabled issuer/audience validation against an expected value of "",
    rejecting every otherwise-valid token. Blank must mean unset.
    """
    s = Settings(
        service_jwt_audience="",
        service_jwt_issuer="   ",
        openai_base_url="",
        backend_api_base_url="",
    )
    assert s.service_jwt_audience is None
    assert s.service_jwt_issuer is None
    assert s.openai_base_url is None
    assert s.backend_api_base_url is None


def test_configured_audience_and_issuer_are_preserved():
    s = Settings(service_jwt_audience="financial-ai", service_jwt_issuer="myfi-backend")
    assert s.service_jwt_audience == "financial-ai"
    assert s.service_jwt_issuer == "myfi-backend"


def test_a_token_verifies_against_a_blank_configured_issuer():
    """End-to-end guard: the exact .env.example shape must still authenticate."""
    import jwt as pyjwt

    from app.core import security

    now = datetime.now(UTC)
    secret = "d" * 40
    token = pyjwt.encode(
        {"sub": "42", "session_id": "s1", "iat": now, "exp": now + timedelta(seconds=300)},
        secret,
        algorithm="HS256",
    )
    blank = Settings(service_jwt_secret=secret, service_jwt_audience="", service_jwt_issuer="")
    original = security.settings
    security.settings = blank
    try:
        assert security._decode_service_token(token)["sub"] == "42"
    finally:
        security.settings = original


def test_plain_logs_carry_their_structured_fields(caplog):
    """
    With JSON_LOGS=false the formatter printed only the message, so
    `backend_request_failed` arrived with no path and no status code -- the two
    fields that separate "endpoint missing" from "token wrong". A team
    integrating locally was debugging blind, which is when readable logs matter
    most.
    """
    import logging

    from app.core.logging import PlainFormatter

    formatter = PlainFormatter("%(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("contract-test")

    record = logging.LogRecord("contract-test", logging.ERROR, __file__, 1, "backend_request_failed", None, None)
    record.extra_fields = {"path": "/internal/transactions", "status": 401}
    line = formatter.format(record)
    assert "path=/internal/transactions" in line
    assert "status=401" in line

    record.extra_fields = {"backend_api_service_token": "super-secret"}
    assert "super-secret" not in formatter.format(record), "secrets must stay redacted"

    # A message with no extras must not gain a trailing separator.
    plain = logging.LogRecord("contract-test", logging.INFO, __file__, 1, "service_starting", None, None)
    assert formatter.format(plain).endswith("service_starting")
    assert logger is not None
