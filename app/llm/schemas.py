"""
Small schema helpers shared by LLM provider implementations.

Domain-level structured-output schemas (ExecutionPlan, etc.) live in
app/schemas/ since they're used well beyond the LLM layer. This module is
for provider-facing plumbing only.
"""

from __future__ import annotations

from typing import Literal, TypedDict


class ChatMessageDict(TypedDict):
    role: Literal["user", "assistant", "system"]
    content: str


def to_provider_messages(history: list[ChatMessageDict]) -> list[dict[str, str]]:
    """Normalize internal message dicts into the {role, content} shape providers expect."""
    return [{"role": m["role"], "content": m["content"]} for m in history]
