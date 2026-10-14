"""
Centralized application configuration.

All environment-driven values live here. Nothing else in the application
should read `os.environ` directly — import `settings` instead. This is what
lets the OpenAI model, backend integration mode, and security settings change
via configuration only, with zero code changes elsewhere.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ConversationStorage(str, Enum):
    MEMORY = "memory"
    BACKEND_API = "backend_api"


class BackendIntegrationMode(str, Enum):
    """Which strategy the backend client uses to reach financial data."""

    BACKEND_API = "backend_api"
    READ_ONLY_DB = "read_only_db"


class Environment(str, Enum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App ----
    app_name: str = "financial-ai-service"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    request_body_max_bytes: int = 16_384

    # ---- OpenAI ----
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = "gpt-4.1"
    openai_timeout_seconds: float = 20.0
    openai_max_retries: int = 2
    openai_base_url: str | None = None

    # ---- Backend integration ----
    backend_integration_mode: BackendIntegrationMode = BackendIntegrationMode.BACKEND_API
    backend_api_base_url: str | None = None
    # 10s was too tight for the real payloads. A period comparison fetches two
    # transaction windows and a merchant question can reach back six months --
    # tens to hundreds of kilobytes per call for an active user. A timeout here
    # costs the user their answer, so the ceiling is set above the slow case
    # rather than at the typical one.
    backend_api_timeout_seconds: float = 30.0
    backend_api_service_token: str = Field(default="", repr=False)

    read_only_db_dsn: str | None = Field(default=None, repr=False)
    read_only_db_timeout_seconds: float = 10.0

    # ---- Security ----
    service_jwt_secret: str = Field(default="", repr=False)
    service_jwt_algorithm: str = "HS256"
    # Optional but strongly recommended: if the issuing backend sets `aud`/`iss`,
    # configure them here so a token minted for a DIFFERENT service cannot be
    # replayed against this one. Left unset, those claims are not verified.
    service_jwt_audience: str | None = None
    service_jwt_issuer: str | None = None
    # Tolerance for clock skew between this service and the issuing backend.
    service_jwt_leeway_seconds: int = 30
    # Defence in depth: reject a validly-signed token whose lifetime is longer
    # than a service-to-service assertion has any business being. Requires the
    # issuer to set `iat`; ignored when it does not. 0 disables the check.
    service_jwt_max_lifetime_seconds: int = 900
    # NoDecode: pydantic-settings JSON-decodes list fields BEFORE validators run,
    # so a plain comma-separated value — or an empty one, as .env.example ships —
    # would raise a JSONDecodeError at import time and the service would never
    # start. NoDecode hands the raw string to _split_origins below instead.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    rate_limit_requests_per_minute: int = 60
    # When true, financial records that do not declare `owner_user_id` are
    # REJECTED rather than accepted unverified (see app/core/data_isolation.py).
    # Turn this on once the backend emits ownership on every record — it turns
    # an unverifiable blind spot into a hard failure.
    strict_data_ownership: bool = False
    # In-memory conversation storage is per-process: with more than one
    # instance, a user's history depends on which pod they land on, and a
    # restart drops it entirely. Production must opt in consciously.
    allow_in_memory_conversations: bool = False

    # ---- Conversation memory ----
    # Where chat history lives. "memory" is process-local: lost on restart and
    # not shared between instances, so behind a load balancer a user's second
    # turn can reach a process that has never heard of them. "backend_api"
    # persists through the backend's /internal/conversations/* endpoints
    # (docs/handover-backend.md section 8).
    conversation_storage: ConversationStorage = ConversationStorage.MEMORY
    conversation_recent_message_limit: int = 12
    conversation_summary_trigger_length: int = 24

    # ---- Logging ----
    log_level: str = "INFO"
    json_logs: bool = True

    @field_validator(
        "service_jwt_audience",
        "service_jwt_issuer",
        "openai_base_url",
        "backend_api_base_url",
        "read_only_db_dsn",
        mode="before",
    )
    @classmethod
    def _empty_string_is_unset(cls, v: object) -> object:
        """
        Treat `KEY=` in a .env file as "not configured", not as the empty string.

        These fields switch behaviour on `is None`. Without this, a blank line
        copied from .env.example sets them to "" — which is NOT None — and, for
        example, turns on JWT issuer validation against an expected issuer of "",
        rejecting every otherwise-valid token.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept `a.com,b.com`, an empty string, or an already-parsed list."""
        if v is None:
            return []
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    def validate_runtime_safety(self) -> None:
        """
        Fail fast on configurations that must never reach production.

        Called once at startup (see app/main.py lifespan). Deliberately raises
        rather than logging a warning: a misconfigured auth secret
        in production is not something to discover from a log.
        """
        if self.service_jwt_algorithm.lower() == "none":
            raise RuntimeError("SERVICE_JWT_ALGORITHM='none' disables signature verification.")

        # Checked in EVERY environment, not just production. An empty token is
        # never valid once backend_api is selected -- it still builds a
        # well-formed client and sends "Authorization: Bearer " on every call,
        # so the backend 401s every request and the assistant answers "your
        # figures are temporarily unavailable" to everything.
        #
        # This first shipped inside the production-only block below, and the
        # team hit exactly that failure while integrating with ENVIRONMENT=local
        # against a real backend: the guardrail existed and never ran. The
        # environment says how much to TRUST a deployment, not whether its
        # credentials are structurally usable.
        if self.backend_integration_mode is BackendIntegrationMode.BACKEND_API:
            missing = []
            if not self.backend_api_base_url:
                missing.append("BACKEND_API_BASE_URL is not set")
            if not self.backend_api_service_token:
                missing.append(
                    "BACKEND_API_SERVICE_TOKEN is empty (every backend call would be sent "
                    "unauthenticated, and the backend will reject all of them)"
                )
            if missing:
                raise RuntimeError(
                    "BACKEND_INTEGRATION_MODE=backend_api is misconfigured: " + "; ".join(missing)
                )

        if not self.is_production:
            return

        problems: list[str] = []
        if not self.service_jwt_secret:
            problems.append("SERVICE_JWT_SECRET is empty")
        elif self.service_jwt_algorithm.upper().startswith("HS") and len(self.service_jwt_secret) < 32:
            # RFC 7518 3.2: an HMAC key should be at least as long as the digest.
            problems.append("SERVICE_JWT_SECRET is shorter than the 32 bytes required for HS256")
        if not self.openai_api_key:
            problems.append("OPENAI_API_KEY is empty")
        if self.debug:
            problems.append("DEBUG=true")
        if (
            self.conversation_storage is ConversationStorage.MEMORY
            and not self.allow_in_memory_conversations
        ):
            problems.append(
                "conversation storage is in-memory (history is lost on restart and is not shared "
                "across instances); set CONVERSATION_STORAGE=backend_api to persist it through the "
                "backend, or ALLOW_IN_MEMORY_CONVERSATIONS=true to accept this deliberately"
            )
        if (
            self.conversation_storage is ConversationStorage.BACKEND_API
            and not self.backend_api_base_url
        ):
            problems.append(
                "CONVERSATION_STORAGE=backend_api requires BACKEND_API_BASE_URL"
            )

        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))

    def warn_on_weak_production_settings(self) -> None:
        """
        Surface production settings that are risky but not our call to refuse.

        Distinct from validate_runtime_safety, which raises: these depend on
        what the BACKEND emits, so this service cannot know whether they are
        wrong. Refusing to start over one would block a deploy for a condition
        that may be perfectly correct -- but shipping silently would leave the
        cross-user barrier switched off with nothing said.
        """
        from app.core.logging import get_logger, log_with_fields

        if not self.is_production:
            return

        if not self.strict_data_ownership:
            log_with_fields(
                get_logger(__name__),
                30,
                "strict_data_ownership_disabled",
                detail=(
                    "Records that do not declare owner_user_id are NOT verified against the "
                    "requesting user. The cross-user containment check is a no-op for them. "
                    "Once the backend emits owner_user_id on every record, set "
                    "STRICT_DATA_OWNERSHIP=true so unlabeled data becomes an error."
                ),
            )


@lru_cache
def get_settings() -> Settings:
    """Settings are cached — construct once per process."""
    return Settings()


settings = get_settings()
