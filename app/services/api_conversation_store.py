"""
Conversation storage backed by the client's backend API.

WHY THIS EXISTS
    The default store keeps chat history in process memory. That is right for
    local development and tests, and wrong for production in two ways that do
    not announce themselves:

      - history is lost on every restart and deploy;
      - it is not shared between instances, so behind a load balancer a user's
        second turn may land on a process that has never heard of them.

    The second is the dangerous one. Nothing errors. The assistant simply
    answers as though the conversation had just begun -- re-asking a question
    it was already answered, losing the period under discussion, and treating
    "yes" as a message with no subject. Every response is still a 200.

THE CONTRACT
    Implements the endpoints in docs/handover-backend.md section 8.2, option
    "you own it". The three extra `chat_messages` columns described in 8.1
    (`intent`, `is_clarification`, `date_phrase`) are not optional decoration:
    they are what lets a later turn read a bare "yes" against the question it
    answers, and what stops the assistant re-asking about a period it has
    already reported on. They are sent and read verbatim -- `date_phrase`
    especially is the USER's wording, resolved to real dates on every read, so
    normalising it at write time would freeze "this month" to whichever month
    the row was inserted in.

SCOPING
    Every call passes BOTH user_id and session_id, and the backend is expected
    to filter on both. A session id alone is not sufficient: it would
    otherwise be guessable or replayable across users, which is the one
    failure this whole service is built to make impossible.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.exceptions import BackendServiceError, ConversationError, ConversationNotFoundError
from app.core.security import UserContext
from app.schemas.conversation import ConversationMessage, ConversationSummary
from app.services.conversation_service import ConversationStore


class BackendAPIConversationStore(ConversationStore):
    """
    Persists conversations through the backend's `/internal/conversations/*`
    endpoints, so history survives restarts and is shared across instances.

    Wire it in by overriding get_conversation_store() (see
    app/api/deps.py) once the backend exposes the endpoints.
    """

    def __init__(self, repository) -> None:  # repository: HttpRepository
        self._repo = repository

    def _scope(self, context: UserContext) -> dict[str, str]:
        # Both, always. See the module docstring.
        return {"user_id": context.user_id, "session_id": context.session_id}

    async def create_session(self, context: UserContext) -> None:
        try:
            await self._repo.post("/internal/conversations", json=self._scope(context))
        except ConversationError:
            raise
        except Exception as exc:
            raise ConversationError("Could not create the conversation.") from exc

    async def session_exists(self, context: UserContext) -> bool:
        try:
            await self._repo.get("/internal/conversations", params=self._scope(context))
        except BackendServiceError as exc:
            if exc.details.get("status_code") == 404:
                return False
            # Anything else is a backend fault. Returning False here would
            # look like "no session yet", and the service would start a fresh
            # conversation on top of one that already exists -- losing the
            # user's history because the backend was briefly unwell.
            raise
        return True

    async def get_messages(self, context: UserContext) -> list[ConversationMessage]:
        data = await self._repo.get("/internal/conversations/messages", params=self._scope(context))
        if data is None:
            raise ConversationNotFoundError("No conversation found for this session.")
        return [ConversationMessage.model_validate(item) for item in data]

    async def append_message(self, context: UserContext, message: ConversationMessage) -> None:
        if message.created_at is None:
            message = message.model_copy(update={"created_at": datetime.now(UTC)})
        payload = self._scope(context) | {
            "role": message.role.value,
            "content": message.content,
            "created_at": message.created_at.isoformat() if message.created_at else None,
            # The three columns from section 8.1. Dropping any of them is a
            # silent memory failure, not an error, so they are always sent
            # even when empty rather than omitted when falsy.
            "intent": message.intent,
            "is_clarification": message.is_clarification,
            "date_phrase": message.date_phrase,
        }
        await self._repo.post("/internal/conversations/messages", json=payload)

    async def get_summary(self, context: UserContext) -> ConversationSummary | None:
        data = await self._repo.get("/internal/conversations/summary", params=self._scope(context))
        return ConversationSummary.model_validate(data) if data else None

    async def set_summary(self, context: UserContext, summary: ConversationSummary) -> None:
        await self._repo.post(
            "/internal/conversations/summary",
            json=self._scope(context) | summary.model_dump(mode="json"),
        )

    async def delete_session(self, context: UserContext) -> None:
        await self._repo.delete("/internal/conversations", params=self._scope(context))
