"""
Structured logging configuration.

Logs are emitted as JSON so they're easy to ship to any log aggregator.
A logging filter strips known-sensitive field names before a record is
emitted, as defense-in-depth on top of "just don't log that" discipline
elsewhere in the codebase.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from app.core.config import settings

_REDACTED_KEYS = {
    "authorization",
    "api_key",
    "openai_api_key",
    "password",
    "token",
    "service_jwt_secret",
    "backend_api_service_token",
    "secret",
}


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if any(bad in key.lower() for bad in _REDACTED_KEYS):
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = _redact_dict(value)
        else:
            redacted[key] = value
    return redacted


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(_redact_dict(extra))
        if record.exc_info:
            # Intentionally omit traceback text from the emitted payload in
            # production-facing logs to avoid leaking internals; a local
            # dev environment can still see it via debug-level console logs.
            payload["error_type"] = str(record.exc_info[0].__name__) if record.exc_info[0] else None
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """
    Human-readable logs that still carry their structured fields.

    The plain formatter used to print only the message, so
    `backend_request_failed` reached the console with no path and no status
    code -- everything needed to tell "endpoint missing" from "token wrong"
    was in `extra_fields` and silently dropped. A team integrating with
    JSON_LOGS=false was debugging blind, which is when readable logs matter
    most. Redaction is applied here exactly as it is for JSON.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict) and extra:
            fields = " ".join(f"{k}={v}" for k, v in _redact_dict(extra).items())
            line = f"{line} | {fields}"
        return line


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    if settings.json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(PlainFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with_fields(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log a message with structured extra fields, redaction applied."""
    logger.log(level, message, extra={"extra_fields": fields})
