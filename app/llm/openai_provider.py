"""
OpenAI implementation of LLMProvider.

- Model name is fully configurable via settings.openai_model — never hardcoded.
- Every call has an explicit timeout.
- Retries are limited, exponential-backoff, and only for transient errors.
- Structured output uses JSON-schema-constrained generation, then re-validates
  with Pydantic locally so a malformed/partial model response can never
  silently pass through as valid structured data.
"""

from __future__ import annotations

import asyncio
import json

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.logging import get_logger, log_with_fields
from app.llm.base import LLMProvider, SchemaT
from app.llm.exceptions import InvalidModelResponseError, LLMProviderError, LLMTimeoutError

logger = get_logger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        base_url: str | None = None,
    ) -> None:
        self._model = model or settings.openai_model
        self._max_retries = max_retries if max_retries is not None else settings.openai_max_retries
        self._timeout = timeout_seconds or settings.openai_timeout_seconds
        self._client = AsyncOpenAI(
            api_key=api_key or settings.openai_api_key,
            base_url=base_url or settings.openai_base_url,
            timeout=self._timeout,
        )

    async def _call_with_retries(self, coro_factory):
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await coro_factory()
            except APITimeoutError as exc:
                last_error = exc
                log_with_fields(logger, 30, "openai_timeout", attempt=attempt)
            except APIConnectionError as exc:
                last_error = exc
                log_with_fields(logger, 30, "openai_connection_error", attempt=attempt)
            except APIStatusError as exc:
                last_error = exc
                if exc.status_code not in _RETRYABLE_STATUS_CODES:
                    log_with_fields(logger, 40, "openai_non_retryable_error", status=exc.status_code)
                    raise LLMProviderError(
                        "The language model provider returned an error.",
                        details={"status_code": exc.status_code},
                    ) from exc
                log_with_fields(logger, 30, "openai_retryable_error", status=exc.status_code, attempt=attempt)

            if attempt < self._max_retries:
                await asyncio.sleep(min(2**attempt * 0.5, 4.0))

        if isinstance(last_error, APITimeoutError):
            raise LLMTimeoutError("The language model provider timed out.") from last_error
        raise LLMProviderError("The language model provider is currently unavailable.") from last_error

    async def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 700,
    ) -> str:
        async def _call():
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system_prompt}, *messages],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response

        response = await self._call_with_retries(_call)
        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice and choice.message else None
        if not content:
            raise InvalidModelResponseError("The language model returned an empty response.")
        return content.strip()

    async def structured_generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        response_schema: type[SchemaT],
        temperature: float = 0.0,
        max_tokens: int = 500,
    ) -> SchemaT:
        schema_dict = _pydantic_to_strict_json_schema(response_schema)

        async def _call():
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system_prompt}, *messages],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_schema.__name__,
                        "schema": schema_dict,
                        "strict": False,
                    },
                },
            )
            return response

        response = await self._call_with_retries(_call)
        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice and choice.message else None
        if not content:
            raise InvalidModelResponseError("The language model returned an empty structured response.")

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise InvalidModelResponseError("The language model returned malformed JSON.") from exc

        try:
            return response_schema.model_validate(data)
        except ValidationError as exc:
            log_with_fields(logger, 40, "structured_output_validation_failed", errors=str(exc))
            raise InvalidModelResponseError(
                "The language model returned a response that did not match the expected structure."
            ) from exc

    async def aclose(self) -> None:
        """
        Release the underlying HTTP connection pool on shutdown.

        Never raises. This runs in the lifespan's shutdown path, where an
        exception would interrupt the remaining cleanup and can stop the
        process terminating cleanly — a failure to tidy up must not become a
        failure to shut down. The pool is released by process exit regardless.
        """
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            log_with_fields(logger, 30, "llm_client_close_failed", error_type=type(exc).__name__)


def _pydantic_to_strict_json_schema(schema: type[BaseModel]) -> dict:
    """
    Produce a JSON schema suitable for OpenAI's structured-output mode.

    We intentionally do not force `additionalProperties: false` recursively
    here (Pydantic's generated schema already models required/optional
    fields correctly for our use cases) — we rely on local re-validation via
    `response_schema.model_validate` as the real safety net regardless of
    what the provider enforces.
    """
    return schema.model_json_schema()
