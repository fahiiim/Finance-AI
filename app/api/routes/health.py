"""
Liveness and readiness.

The distinction matters to whatever is watching this process:

  /health  is LIVENESS -- the process is up and serving. A failure here means
           restart me. It must stay cheap and dependency-free, or a slow
           backend triggers a restart loop that fixes nothing.

  /ready   is READINESS -- this instance can actually answer a user. A failure
           means stop sending me traffic. It is allowed to look at
           dependencies, and it MUST, because the alternative was discovered
           the hard way: with an empty BACKEND_API_SERVICE_TOKEN this endpoint
           reported "ready" with a 200 while every single question a user
           asked came back "your figures are temporarily unavailable". Nothing
           was monitoring the one thing that was broken.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response

from app.core.config import BackendIntegrationMode, settings
from app.core.logging import get_logger, log_with_fields

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

#: How long a backend reachability result is reused. Readiness probes run
#: every few seconds per instance; without this, a fleet would generate steady
#: background load against the backend purely to ask whether it is up.
_PROBE_CACHE_SECONDS = 15.0

_probe_cache: tuple[float, bool, str] | None = None
_llm_probe_cache: tuple[float, bool, str] | None = None


@router.get(
    "/health",
    summary="Liveness check",
    description="Returns 200 if the process is up. Does not check downstream dependencies.",
    response_description="Service liveness status.",
)
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name}


def _configuration_problems() -> list[str]:
    """
    Settings that make this instance unable to answer, without any I/O.

    Cheap enough to run on every probe, and catches the failure that actually
    happened: credentials that are structurally missing rather than wrong.
    """
    problems: list[str] = []

    if not settings.openai_api_key:
        problems.append("OPENAI_API_KEY is not set")

    if settings.backend_integration_mode is BackendIntegrationMode.BACKEND_API:
        if not settings.backend_api_base_url:
            problems.append("BACKEND_API_BASE_URL is not set")
        if not settings.backend_api_service_token:
            problems.append(
                "BACKEND_API_SERVICE_TOKEN is empty -- every backend call is sent unauthenticated"
            )
    return problems


async def _backend_reachable() -> tuple[bool, str]:
    """
    Whether the backend answers at all, cached briefly.

    ANY HTTP response counts as reachable, including a 404: this proves DNS,
    TCP, TLS and routing all work, which is what readiness is asking about.
    Whether a particular endpoint exists is a contract question, and
    scripts/verify_backend_contract.py is where that belongs -- a readiness
    probe that failed on a 404 would take a whole fleet out of rotation over
    one unimplemented optional endpoint.
    """
    global _probe_cache

    if settings.backend_integration_mode is not BackendIntegrationMode.BACKEND_API:
        return True, "not applicable for this integration mode"
    if not settings.backend_api_base_url:
        return False, "no base URL configured"

    now = time.monotonic()
    if _probe_cache is not None and now - _probe_cache[0] < _PROBE_CACHE_SECONDS:
        return _probe_cache[1], _probe_cache[2]

    import httpx

    try:
        async with httpx.AsyncClient(timeout=min(settings.backend_api_timeout_seconds, 5.0)) as client:
            response = await client.get(settings.backend_api_base_url)
        ok, detail = True, f"responded with HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        ok, detail = False, f"unreachable ({type(exc).__name__})"
        log_with_fields(logger, 40, "backend_unreachable_on_readiness", error_type=type(exc).__name__)

    _probe_cache = (now, ok, detail)
    return ok, detail


async def _llm_key_valid() -> tuple[bool, str]:
    """
    Whether the OpenAI key is actually accepted, cached briefly.

    Checking only that the variable is NON-EMPTY is the same mistake that let
    an empty backend token through: a key that is present but revoked or
    expired passes that test and fails every single chat request. A revoked
    key is a completely ordinary production event -- rotation, a leak, an
    expired billing card -- so it deserves a real check rather than trust.

    models.list() is a free GET, and the result is cached, so a fleet of
    readiness probes costs nothing.
    """
    global _llm_probe_cache

    if not settings.openai_api_key:
        return False, "no API key configured"

    now = time.monotonic()
    if _llm_probe_cache is not None and now - _llm_probe_cache[0] < _PROBE_CACHE_SECONDS:
        return _llm_probe_cache[1], _llm_probe_cache[2]

    from openai import AsyncOpenAI

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=5.0, max_retries=0)
        try:
            await client.models.list()
            ok, detail = True, "key accepted"
        finally:
            await client.close()
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        ok, detail = False, f"key rejected or unreachable ({type(exc).__name__})"
        log_with_fields(logger, 40, "llm_unavailable_on_readiness", error_type=type(exc).__name__)

    _llm_probe_cache = (now, ok, detail)
    return ok, detail


@router.get(
    "/ready",
    summary="Readiness check",
    description=(
        "Returns 200 when this instance can actually answer a user, and 503 when it "
        "cannot. Checks configuration and — for backend_api mode — that the backend "
        "responds at all. Backend reachability is cached briefly so probes do not "
        "generate load."
    ),
    response_description="Service readiness status and the checks behind it.",
)
async def ready(response: Response) -> dict:
    problems = _configuration_problems()
    reachable, detail = await _backend_reachable()
    if not reachable:
        problems.append(f"backend {detail}")

    llm_ok, llm_detail = await _llm_key_valid()
    if not llm_ok:
        problems.append(f"OpenAI {llm_detail}")

    checks = {
        "openai_api_key_configured": bool(settings.openai_api_key),
        "openai_key_valid": llm_ok,
        "openai_detail": llm_detail,
        "backend_integration_mode": settings.backend_integration_mode.value,
        "backend_credentials_configured": not any("BACKEND_API" in p for p in _configuration_problems()),
        "backend_reachable": reachable,
        "backend_detail": detail,
    }

    if problems:
        # 503 so an orchestrator or load balancer actually acts on it. Reporting
        # 200 with a "not_ready" string in the body meant every probe passed and
        # a misconfigured instance kept serving errors to users.
        response.status_code = 503
        return {"status": "not_ready", "problems": problems, "checks": checks}

    return {"status": "ready", "checks": checks}
