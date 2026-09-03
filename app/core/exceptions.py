"""
Application exception hierarchy.

Every exception carries an HTTP status code and a stable machine-readable
`code` so the API layer can translate it into a structured error response
without ever leaking stack traces, secrets, or internal details to the
client. Route handlers never construct HTTPException directly for domain
errors — they let these propagate to the central handler in main.py.
"""

from __future__ import annotations


class FinancialAIError(Exception):
    """Base class for all application-raised errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthenticationError(FinancialAIError):
    status_code = 401
    code = "authentication_error"


class AuthorizationError(FinancialAIError):
    status_code = 403
    code = "authorization_error"


class UserContextError(FinancialAIError):
    """Raised when a trustworthy UserContext cannot be established."""

    status_code = 401
    code = "user_context_error"


class CrossUserDataError(FinancialAIError):
    """
    Raised when data reaching this service does not belong to the requesting
    user. This is a containment breach, not a routine error: it means an
    upstream data source returned another user's records. The request is
    aborted rather than answered — never degrade to a partial answer here.
    """

    status_code = 500
    code = "cross_user_data_blocked"


class ConversationNotFoundError(FinancialAIError):
    status_code = 404
    code = "conversation_not_found"


class ConversationError(FinancialAIError):
    status_code = 409
    code = "conversation_error"


class BackendServiceError(FinancialAIError):
    status_code = 502
    code = "backend_service_error"


class BackendTimeoutError(FinancialAIError):
    status_code = 504
    code = "backend_timeout"


class FinancialDataUnavailableError(FinancialAIError):
    status_code = 503
    code = "financial_data_unavailable"


class ToolExecutionError(FinancialAIError):
    status_code = 500
    code = "tool_execution_error"


class LLMProviderError(FinancialAIError):
    status_code = 502
    code = "llm_provider_error"


class LLMTimeoutError(FinancialAIError):
    status_code = 504
    code = "llm_timeout"


class InvalidModelResponseError(FinancialAIError):
    status_code = 502
    code = "invalid_model_response"


class RequestTooLargeError(FinancialAIError):
    status_code = 413
    code = "request_too_large"


class RateLimitedError(FinancialAIError):
    status_code = 429
    code = "rate_limited"


class ValidationFailedError(FinancialAIError):
    status_code = 422
    code = "validation_failed"
