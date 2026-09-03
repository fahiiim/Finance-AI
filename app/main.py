"""
Application entrypoint.

Route handlers stay thin (spec section 35): this file wires middleware,
routers, and the single centralized exception handler that translates our
exception hierarchy into a stable, secret-free ErrorResponse — it never lets
a raw traceback or exception message reach the client.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# Running this file directly (VS Code's Run button, `python app/main.py`) makes
# `app/` — not the project root — sys.path[0], so the absolute `app.*` imports
# below cannot resolve. Put the project root back on the path first. When the
# module is imported normally (`uvicorn app.main:app`) `__package__` is set and
# this is a no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import get_llm_provider
from app.api.routes import chat, conversations, health
from app.core.config import Environment, settings
from app.core.exceptions import FinancialAIError
from app.core.logging import configure_logging, get_logger, log_with_fields
from app.schemas.responses import ErrorResponse
from app.services.backend_client import get_backend_client

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on an unsafe configuration rather than starting and serving
    # fabricated data (or unauthenticated traffic) in production.
    settings.validate_runtime_safety()
    settings.warn_on_weak_production_settings()
    log_with_fields(
        logger,
        20,
        "service_starting",
        environment=settings.environment.value,
        backend_mode=settings.backend_integration_mode.value,
    )
    try:
        yield
    finally:
        # Both clients own httpx connection pools. Releasing them here is what
        # makes a rolling restart clean instead of leaving sockets in TIME_WAIT.
        await get_backend_client().aclose()
        await get_llm_provider().aclose()
        log_with_fields(logger, 20, "service_stopping")


app = FastAPI(
    title="Financial AI Service",
    description=(
        "AI orchestration layer for a personal finance application. Understands "
        "natural-language questions about a user's own verified financial data. "
        "This service does not own financial data — the existing backend remains "
        "the source of truth (see docs/architecture.md)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

if settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "X-Session-Id", "Content-Type"],
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(FinancialAIError)
async def financial_ai_error_handler(request: Request, exc: FinancialAIError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    log_with_fields(
        logger,
        40 if exc.status_code >= 500 else 30,
        "request_failed",
        code=exc.code,
        status_code=exc.status_code,
        request_id=request_id,
        path=str(request.url.path),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(code=exc.code, message=exc.message, request_id=request_id).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    log_with_fields(
        logger, 50, "unhandled_exception", request_id=request_id, path=str(request.url.path), error_type=type(exc).__name__
    )
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            code="internal_error", message="An unexpected error occurred.", request_id=request_id
        ).model_dump(),
    )


app.include_router(health.router)
app.include_router(chat.router)
app.include_router(conversations.router)


if __name__ == "__main__":
    # Convenience entrypoint for local development only. Production runs the
    # ASGI app under a process manager (see Dockerfile), never this block.
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=settings.environment is Environment.LOCAL,
    )
