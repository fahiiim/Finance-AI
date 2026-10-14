"""
Authentication and trusted UserContext establishment.

CRITICAL INVARIANT (see docs/security.md):
    The AI service NEVER trusts a user_id supplied by the request body,
    the natural-language message, or the LLM. User identity is derived
    ONLY from a verified service-to-service JWT issued by the existing,
    authenticated backend.

The expected integration shape is: the client's existing backend already
authenticates the end user (session cookie, OAuth, etc.), then calls this
AI service server-to-server with a short-lived signed token asserting who
the user is. This module verifies that token and produces a `UserContext`
that every downstream tool/orchestrator component receives explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.exceptions import AuthenticationError, UserContextError


@dataclass(frozen=True, slots=True)
class UserContext:
    """
    Trusted, immutable identity + locale context for a single request.

    Every financial tool and backend call takes this explicitly rather than
    reading ambient state, so a user_id can never be silently substituted
    partway through a request.
    """

    user_id: str
    session_id: str
    timezone: str = "UTC"
    currency: str = "USD"

    def for_session(self, session_id: str) -> UserContext:
        """
        Same authenticated user, different session.

        Used by the conversation endpoints so a URL path can address one of
        the caller's OWN sessions. user_id is carried over unchanged and can
        never be substituted here — that is what keeps the (user_id,
        session_id) scoping invariant intact.
        """
        return replace(self, session_id=session_id)

    def __repr__(self) -> str:  # pragma: no cover - defensive redaction
        # user_id is a stable identifier, not a secret, but we still avoid
        # ever accidentally interpolating raw PII-adjacent values into logs
        # via a bare repr() call elsewhere in the codebase.
        return f"UserContext(user_id={self.user_id!r}, session_id={self.session_id!r})"


def _decode_service_token(token: str) -> dict:
    """
    Verify a service token's signature and time-based claims.

    `exp` is REQUIRED. PyJWT only validates expiry when the claim is present,
    so without `require=["exp"]` an issuer that forgets it mints tokens that
    are valid forever — a single leaked token would then be permanent
    impersonation of that user. Identity claim completeness (`sub`,
    `session_id`) is still checked separately by the caller, because
    "unauthenticated" and "authenticated but unusable" are different failures
    a caller should be able to tell apart.
    """
    if not settings.service_jwt_secret:
        raise AuthenticationError("Service authentication is not configured.")

    if settings.service_jwt_algorithm.lower() == "none":
        # Belt and braces; config.validate_runtime_safety() also rejects this
        # at startup. An attacker-supplied `alg: none` cannot get this far
        # regardless, since we pass an explicit algorithm allow-list below.
        raise AuthenticationError("Service authentication is not configured.")

    try:
        payload = jwt.decode(
            token,
            settings.service_jwt_secret,
            # Explicit allow-list — never derive the algorithm from the token
            # header, which is the classic algorithm-confusion attack.
            algorithms=[settings.service_jwt_algorithm],
            audience=settings.service_jwt_audience,
            issuer=settings.service_jwt_issuer,
            leeway=settings.service_jwt_leeway_seconds,
            options={
                "require": ["exp"],
                # PyJWT raises InvalidAudienceError when a token carries `aud`
                # but no expected audience was configured. Only opt in to
                # audience checking when the operator has actually set one.
                "verify_aud": settings.service_jwt_audience is not None,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("The service token has expired.") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise AuthenticationError("The service token is missing a required claim.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid or expired service token.") from exc

    _enforce_max_lifetime(payload)
    return payload


def _enforce_max_lifetime(payload: dict) -> None:
    """
    Reject validly-signed tokens with an implausibly long lifetime.

    A service-to-service assertion should live minutes, not months. If the
    issuer sets `iat` we can enforce that independently of trusting it to
    choose a sane `exp`. Skipped when `iat` is absent or the check is disabled.
    """
    max_lifetime = settings.service_jwt_max_lifetime_seconds
    if max_lifetime <= 0:
        return

    issued_at, expires_at = payload.get("iat"), payload.get("exp")
    if not isinstance(issued_at, (int, float)) or not isinstance(expires_at, (int, float)):
        return

    if expires_at - issued_at > max_lifetime + settings.service_jwt_leeway_seconds:
        raise AuthenticationError("The service token lifetime exceeds the permitted maximum.")


# Declares the bearer scheme to OpenAPI so Swagger UI shows an Authorize button.
# Swagger silently discards any header PARAMETER named "authorization", so
# without this the docs page could not authenticate at all. auto_error=False:
# the check itself stays in get_user_context below, with its own error type.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Service-signed JWT (HS256) asserting sub=<application_users.id>. Paste the token only; "
    "Swagger adds the 'Bearer ' prefix.",
)


async def get_user_context(
    # Still read directly so every client works as before; hidden from the
    # schema because Swagger would render a field that it never sends.
    authorization: str | None = Header(default=None, include_in_schema=False),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    _scheme: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> UserContext:
    """
    FastAPI dependency that establishes the trusted UserContext for a request.

    Expects:
        Authorization: Bearer <service-signed JWT with sub=user_id>
        X-Session-Id: <session id>  (falls back to the token's session_id claim)

    Raises AuthenticationError if identity cannot be established. Never
    accepts a user_id from anywhere else — see module docstring.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing or malformed Authorization header.")

    token = authorization.split(" ", 1)[1].strip()
    payload = _decode_service_token(token)

    user_id = payload.get("sub")
    session_id = x_session_id or payload.get("session_id")
    if not user_id or not session_id:
        raise UserContextError("Token did not establish a complete user identity.")

    timezone = payload.get("timezone") or "UTC"
    currency = payload.get("currency") or "USD"

    return UserContext(
        user_id=str(user_id),
        session_id=str(session_id),
        timezone=str(timezone),
        currency=str(currency),
    )


def redact(value: str, keep: int = 4) -> str:
    """Redact a secret-ish string for safe logging, keeping a short suffix."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"
