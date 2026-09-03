from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

MAX_MESSAGE_LENGTH = 2000


class ChatRequest(BaseModel):
    """
    Inbound chat request.

    Deliberately has NO user_id field. Identity comes exclusively from the
    authenticated UserContext dependency — see core/security.py and
    docs/security.md. Accepting a user_id here would let a caller impersonate
    another user simply by editing the JSON body.
    """

    session_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("message")
    @classmethod
    def _strip_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        return v
