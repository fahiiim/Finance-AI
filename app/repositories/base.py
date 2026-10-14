"""
Base repository for HTTP-backed data access.

Concrete backend integrations (see services/backend_client.py) compose this
rather than talking to httpx directly, so timeout handling and error
translation into our exception hierarchy happens in exactly one place.
"""

from __future__ import annotations

import asyncio

import httpx

from app.core.exceptions import BackendServiceError, BackendTimeoutError
from app.core.logging import get_logger, log_with_fields

logger = get_logger(__name__)


# A dropped connection or a gateway hiccup is retried; a real answer (4xx, or a
# 5xx that is not a gateway error) is not. Only reads are retried: repeating a
# write could apply it twice.
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_RETRY_DELAYS = (0.4, 1.0)


class HttpRepository:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers=headers or {},
            transport=transport,
        )

    async def get(self, path: str, *, params: dict | None = None) -> dict | list:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json: dict | None = None) -> dict | list:
        return await self._request("POST", path, json=json)

    async def delete(self, path: str, *, params: dict | None = None) -> dict | list:
        return await self._request("DELETE", path, params=params)

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request, retried on transient transport/gateway failures (GET only)."""
        attempts = 1 + (len(_RETRY_DELAYS) if method == "GET" else 0)
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                # TimeoutException is a TransportError too: a slow endpoint is
                # not retried, it would only double the wait.
                if last or isinstance(exc, httpx.TimeoutException):
                    raise
                log_with_fields(logger, 30, "backend_request_retry", path=path, error_type=type(exc).__name__)
            else:
                if last or response.status_code not in _RETRYABLE_STATUS:
                    return response
                log_with_fields(logger, 30, "backend_request_retry", path=path, status=response.status_code)
            await asyncio.sleep(_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            response = await self._send(method, path, **kwargs)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                # 204/empty is a real answer ("no summary yet", "deleted"),
                # not a malformed one. .json() would raise on it.
                return {}
            return response.json()
        except httpx.TimeoutException as exc:
            log_with_fields(logger, 30, "backend_request_timeout", path=path)
            raise BackendTimeoutError("The backend financial data service timed out.") from exc
        except httpx.HTTPStatusError as exc:
            # 404 is logged at a lower level than the rest: "this session does
            # not exist yet" is a routine answer to a routine question, not a
            # backend fault, and alerting on it would train people to ignore
            # the alert. The status code travels in `details` so callers can
            # tell the two apart -- the repository stays generic and does not
            # need to know what a conversation is.
            not_found = exc.response.status_code == 404
            log_with_fields(
                logger, 20 if not_found else 40,
                "backend_request_failed", path=path, status=exc.response.status_code,
            )
            raise BackendServiceError(
                "The backend financial data service returned an error.",
                details={"status_code": exc.response.status_code},
            ) from exc
        except httpx.HTTPError as exc:
            log_with_fields(logger, 40, "backend_request_error", path=path)
            raise BackendServiceError("Could not reach the backend financial data service.") from exc

    async def aclose(self) -> None:
        """
        Release the connection pool. Never raises — see the note in
        OpenAIProvider.aclose(): failing to tidy up must not become a failure
        to shut down.
        """
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            log_with_fields(logger, 30, "backend_client_close_failed", error_type=type(exc).__name__)
