from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.schemas.planner import Intent


class ResponseMessage(BaseModel):
    role: str = "assistant"
    content: str


class ChatMetadata(BaseModel):
    intent: Intent
    tools_used: list[str] = Field(default_factory=list)
    data_used: bool = False
    calculation_used: bool = False
    needs_clarification: bool = False
    # The window the answer was computed over, and the phrase it came from.
    # Every figure in a financial answer is only meaningful next to its
    # period, and a caller should not have to parse prose to find out which
    # one was used -- or to notice that the wrong one was.
    period_start: date | None = None
    period_end: date | None = None
    period_phrase: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    message: ResponseMessage
    metadata: ChatMetadata


class ErrorResponse(BaseModel):
    """
    Stable public error shape. Never includes stack traces, internal
    exception args, or secrets — only a stable `code`, a safe human message,
    and a request_id for support/tracing correlation.
    """

    code: str
    message: str
    request_id: str | None = None
