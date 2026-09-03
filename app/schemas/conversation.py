from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationMessage(BaseModel):
    role: MessageRole
    content: str
    created_at: datetime | None = None
    intent: str | None = None
    # True when this assistant turn was a clarifying QUESTION rather than an
    # answer. Without this the planner cannot tell that the user's next
    # message ("yes", "the first one") is answering something, and would ask
    # the same question forever.
    is_clarification: bool = False
    # The period this assistant turn was about ("last month"), when it had one.
    # The conversation's memory of which window it is discussing: a follow-up
    # that names no period inherits it, so the assistant never has to ask
    # about a period it has already answered for.
    date_phrase: str | None = None


class ConversationSummary(BaseModel):
    """Compact rolling summary used once history exceeds the recent-message window."""

    session_id: str
    summary_text: str
    covers_message_count: int
    updated_at: datetime


class ConversationCreateRequest(BaseModel):
    pass  # user_id comes from UserContext, never the body


class ConversationResponse(BaseModel):
    session_id: str
    user_id: str
    messages: list[ConversationMessage] = Field(default_factory=list)
    summary: ConversationSummary | None = None
