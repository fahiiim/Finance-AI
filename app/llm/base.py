"""
LLM provider interface.

FastAPI routes and the orchestrator never talk to OpenAI (or any provider)
directly — everything goes through this interface. Swapping models,
providers, or adding streaming later means implementing/extending this
class, not touching the orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 700,
    ) -> str:
        """Generate a free-text completion for user-facing responses."""

    @abstractmethod
    async def structured_generate(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        response_schema: type[SchemaT],
        temperature: float = 0.0,
        max_tokens: int = 500,
    ) -> SchemaT:
        """
        Generate a completion constrained to `response_schema` and return a
        validated instance of it. Used for intent detection / planning,
        where free-form text parsing would be fragile.
        """

    async def aclose(self) -> None:
        """
        Release any long-lived resources (connection pools) on shutdown.
        No-op by default so test doubles need not implement it.
        """
        return None
