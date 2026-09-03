"""
Conversation context-window strategy.

We never send unlimited history to OpenAI (see spec section 11). Instead:
  - the most recent `conversation_recent_message_limit` messages go in full, and
  - anything older is folded into a compact rolling summary once history
    exceeds `conversation_summary_trigger_length`.

The summary itself is produced by the LLM provider (a cheap, focused call),
never assumed or hand-written.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import settings
from app.core.security import UserContext
from app.llm.base import LLMProvider
from app.schemas.conversation import ConversationMessage, ConversationSummary
from app.services.conversation_service import ConversationService

_SUMMARY_SYSTEM_PROMPT = """\
Summarize the older portion of this financial-assistant conversation in 2-4 \
sentences, preserving any figures, time periods, or categories the user \
asked about that later messages might reference. Do not add commentary.
"""


class MemoryService:
    def __init__(self, conversation_service: ConversationService, llm_provider: LLMProvider) -> None:
        self._conversations = conversation_service
        self._llm = llm_provider

    async def build_context_window(
        self, context: UserContext
    ) -> tuple[list[ConversationMessage], ConversationSummary | None]:
        """Returns (recent_messages, summary) ready to hand to the response prompt."""
        history = await self._conversations.get_history(context)
        recent = history[-settings.conversation_recent_message_limit :]
        summary = await self._conversations.get_summary(context)
        return recent, summary

    async def maybe_update_summary(self, context: UserContext) -> None:
        history = await self._conversations.get_history(context)
        if len(history) < settings.conversation_summary_trigger_length:
            return

        older = history[: -settings.conversation_recent_message_limit]
        if not older:
            return

        existing_summary = await self._conversations.get_summary(context)
        already_covered = existing_summary.covers_message_count if existing_summary else 0
        if len(older) <= already_covered:
            return  # nothing new to fold in

        transcript = "\n".join(f"{m.role.value}: {m.content}" for m in older)
        prior = f"Previous summary: {existing_summary.summary_text}\n\n" if existing_summary else ""
        summary_text = await self._llm.generate(
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"{prior}Conversation so far:\n{transcript}"}],
            temperature=0.0,
            max_tokens=200,
        )

        await self._conversations.set_summary(
            context,
            ConversationSummary(
                session_id=context.session_id,
                summary_text=summary_text,
                covers_message_count=len(older),
                updated_at=datetime.now(UTC),
            ),
        )
