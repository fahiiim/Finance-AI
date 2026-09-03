from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class UserContextSchema(BaseModel):
    """Read-only view of UserContext, safe to embed in logs/responses if needed."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    timezone: str
    currency: str
