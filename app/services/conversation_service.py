"""
Conversation storage abstraction + service.

CRITICAL: every read/write is scoped by BOTH user_id and session_id (see
financial-ai-data-security skill) — a session_id alone is never sufficient,
since it could otherwise be guessed/replayed to read another user's history.

If the existing backend already owns chat_sessions/chat_messages tables
(common — see spec section 11), implement ConversationStore against the
backend API instead of the in-memory default and wire it in via
get_conversation_service(). No orchestrator code needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from app.core.exceptions import ConversationError, ConversationNotFoundError
from app.core.security import UserContext
from app.schemas.conversation import ConversationMessage, ConversationSummary, MessageRole


class ConversationStore(ABC):
    @abstractmethod
    async def create_session(self, context: UserContext) -> None: ...

    @abstractmethod
    async def get_messages(self, context: UserContext) -> list[ConversationMessage]: ...

    @abstractmethod
    async def append_message(self, context: UserContext, message: ConversationMessage) -> None: ...

    @abstractmethod
    async def get_summary(self, context: UserContext) -> ConversationSummary | None: ...

    @abstractmethod
    async def set_summary(self, context: UserContext, summary: ConversationSummary) -> None: ...

    @abstractmethod
    async def delete_session(self, context: UserContext) -> None: ...

    @abstractmethod
    async def session_exists(self, context: UserContext) -> bool: ...


class InMemoryConversationStore(ConversationStore):
    """
    Process-local store, intended for local dev/tests only (it will not work
    across multiple service instances). Keys are always (user_id, session_id)
    tuples — never session_id alone — enforcing the isolation invariant above
    structurally rather than by convention.
    """

    def __init__(self) -> None:
        self._messages: dict[tuple[str, str], list[ConversationMessage]] = {}
        self._summaries: dict[tuple[str, str], ConversationSummary] = {}

    def _key(self, context: UserContext) -> tuple[str, str]:
        return (context.user_id, context.session_id)

    async def create_session(self, context: UserContext) -> None:
        key = self._key(context)
        if key in self._messages:
            raise ConversationError("A conversation with this session_id already exists.")
        self._messages[key] = []

    async def session_exists(self, context: UserContext) -> bool:
        return self._key(context) in self._messages

    async def get_messages(self, context: UserContext) -> list[ConversationMessage]:
        key = self._key(context)
        if key not in self._messages:
            raise ConversationNotFoundError("No conversation found for this session.")
        return list(self._messages[key])

    async def append_message(self, context: UserContext, message: ConversationMessage) -> None:
        key = self._key(context)
        self._messages.setdefault(key, [])
        if message.created_at is None:
            message = message.model_copy(update={"created_at": datetime.now(UTC)})
        self._messages[key].append(message)

    async def get_summary(self, context: UserContext) -> ConversationSummary | None:
        return self._summaries.get(self._key(context))

    async def set_summary(self, context: UserContext, summary: ConversationSummary) -> None:
        self._summaries[self._key(context)] = summary

    async def delete_session(self, context: UserContext) -> None:
        key = self._key(context)
        self._messages.pop(key, None)
        self._summaries.pop(key, None)


_default_store = InMemoryConversationStore()


def get_conversation_store() -> ConversationStore:
    """
    The configured store.

    Imported lazily so this module stays free of HTTP and config concerns for
    tests and local runs, which never leave the in-memory path.
    """
    from app.core.config import ConversationStorage, settings

    if settings.conversation_storage is ConversationStorage.BACKEND_API:
        from app.repositories.base import HttpRepository
        from app.services.api_conversation_store import BackendAPIConversationStore

        if not settings.backend_api_base_url:
            raise RuntimeError(
                "CONVERSATION_STORAGE=backend_api requires BACKEND_API_BASE_URL to be set."
            )
        return BackendAPIConversationStore(
            HttpRepository(
                base_url=settings.backend_api_base_url,
                timeout_seconds=settings.backend_api_timeout_seconds,
                headers={"Authorization": f"Bearer {settings.backend_api_service_token}"},
            )
        )

    return _default_store


class ConversationService:
    """Business logic layer over ConversationStore: creation, history, deletion."""

    def __init__(self, store: ConversationStore | None = None) -> None:
        self._store = store or get_conversation_store()

    async def ensure_session(self, context: UserContext) -> None:
        if not await self._store.session_exists(context):
            await self._store.create_session(context)

    async def add_user_message(self, context: UserContext, content: str) -> None:
        await self._store.append_message(context, ConversationMessage(role=MessageRole.USER, content=content))

    async def add_assistant_message(
        self,
        context: UserContext,
        content: str,
        *,
        intent: str | None = None,
        is_clarification: bool = False,
        date_phrase: str | None = None,
    ) -> None:
        await self._store.append_message(
            context,
            ConversationMessage(
                role=MessageRole.ASSISTANT,
                content=content,
                intent=intent,
                is_clarification=is_clarification,
                date_phrase=date_phrase,
            ),
        )

    async def get_history(self, context: UserContext) -> list[ConversationMessage]:
        return await self._store.get_messages(context)

    async def get_summary(self, context: UserContext) -> ConversationSummary | None:
        return await self._store.get_summary(context)

    async def set_summary(self, context: UserContext, summary: ConversationSummary) -> None:
        await self._store.set_summary(context, summary)

    async def delete(self, context: UserContext) -> None:
        await self._store.delete_session(context)

    async def exists(self, context: UserContext) -> bool:
        return await self._store.session_exists(context)
