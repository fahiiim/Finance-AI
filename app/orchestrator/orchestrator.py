"""
Orchestrator: the single entry point route handlers call.

Implements the full pipeline from spec section 2:

    UserContext -> conversation context -> intent + plan -> financial data
    -> deterministic calculations -> minimal verified context -> OpenAI
    -> response

Route handlers stay thin (spec section 35) — all of this logic lives here,
never inline in app/api/routes/chat.py.
"""

from __future__ import annotations

from app.core.exceptions import (
    BackendServiceError,
    BackendTimeoutError,
    FinancialDataUnavailableError,
    ValidationFailedError,
)
from app.core.logging import get_logger, log_with_fields
from app.core.security import UserContext
from app.llm.base import LLMProvider
from app.orchestrator.context_builder import build_data_block, build_response_messages
from app.orchestrator.intent_router import route_and_execute
from app.orchestrator.planner import build_execution_plan
from app.prompts.response import CLARIFICATION_PROMPT_V2, RESPONSE_GENERATION_PROMPT_V2
from app.schemas.chat import ChatRequest
from app.schemas.responses import ChatMetadata, ChatResponse, ResponseMessage
from app.services.backend_client import BackendClient
from app.services.conversation_service import ConversationService
from app.services.financial_service import FinancialService
from app.services.memory_service import MemoryService
from app.utils.validation import enforce_max_length, looks_like_injection_attempt

logger = get_logger(__name__)


class Orchestrator:
    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        backend_client: BackendClient,
        conversation_service: ConversationService,
        memory_service: MemoryService,
    ) -> None:
        self._llm = llm_provider
        self._backend_client = backend_client
        self._conversations = conversation_service
        self._memory = memory_service

    async def run(self, request: ChatRequest, context: UserContext) -> ChatResponse:
        enforce_max_length(request.message)

        if looks_like_injection_attempt(request.message):
            # Signal-only (see utils/validation.py) — logged for observability,
            # never used to alter authorization. The message is still
            # processed normally; retrieved data is what actually needs the
            # untrusted-data envelope, not the user's own message to their
            # own assistant.
            log_with_fields(logger, 30, "possible_injection_pattern", session_id=context.session_id)

        await self._conversations.ensure_session(context)
        await self._conversations.add_user_message(context, request.message)

        recent_history, summary = await self._memory.build_context_window(context)
        # Exclude the message we just added from "history" passed to the planner
        # separately since build_execution_plan appends it itself.
        history_for_planner = recent_history[:-1] if recent_history and recent_history[-1].content == request.message else recent_history

        plan = await build_execution_plan(
            llm_provider=self._llm,
            context=context,
            current_message=request.message,
            recent_history=history_for_planner,
        )

        if plan.needs_clarification:
            answer = await self._llm.generate(
                system_prompt=CLARIFICATION_PROMPT_V2,
                messages=[{"role": "user", "content": plan.clarification_question or request.message}],
                temperature=0.3,
                max_tokens=150,
            )
            await self._conversations.add_assistant_message(
                context, answer, intent=plan.intent.value, is_clarification=True
            )
            return ChatResponse(
                session_id=context.session_id,
                message=ResponseMessage(content=answer),
                metadata=ChatMetadata(
                    intent=plan.intent,
                    tools_used=[],
                    data_used=False,
                    calculation_used=False,
                    needs_clarification=True,
                ),
            )

        financial_service = FinancialService(self._backend_client)
        tool_results: dict = {}
        tools_used: list[str] = []
        unavailable_reason: str | None = None

        if plan.requires_financial_data:
            try:
                tool_results, tools_used = await route_and_execute(financial_service, context, plan)
            except (FinancialDataUnavailableError, BackendTimeoutError, BackendServiceError) as exc:
                # A slow or failing backend is the most likely production
                # incident, and the user is mid-conversation. Answer in words
                # rather than returning 502/504 into the chat window: the
                # request itself succeeded, we simply could not read the data.
                tool_results, tools_used = {}, []
                unavailable_reason = (
                    "This user's financial data could not be retrieved right now because the data "
                    "service did not respond. Say plainly that their figures are temporarily "
                    "unavailable and suggest trying again shortly. Do NOT state, estimate, or "
                    "guess any amount, and do not describe this as them having no data."
                )
                log_with_fields(
                    logger, 40, "financial_data_unavailable",
                    session_id=context.session_id, error_type=type(exc).__name__,
                )
            except ValidationFailedError as exc:
                # A tool saying "I cannot compute this from what I have" is a
                # conversational outcome, not an HTTP failure. Left to
                # propagate it reached the user mid-chat as a red
                # "422 validation_failed" box -- observed live when an
                # affordability question named no price. Request-level
                # validation (a session id that does not match the token) is
                # still a 422; it is rejected before the orchestrator runs.
                tool_results, tools_used = {}, []
                unavailable_reason = str(exc)
                log_with_fields(
                    logger, 30, "tool_validation_failed", session_id=context.session_id, reason=unavailable_reason
                )

        if unavailable_reason:
            # Hand the model the reason so it can say what is missing, in its
            # own words, instead of falling back to a generic "unavailable".
            tool_results = {"unavailable": unavailable_reason}

        data_block = build_data_block(tool_results, plan)
        messages = build_response_messages(
            recent_history=history_for_planner,
            summary=summary,
            current_message=request.message,
            data_block=data_block,
            plan=plan,
        )

        answer = await self._llm.generate(
            system_prompt=RESPONSE_GENERATION_PROMPT_V2,
            messages=messages,
            temperature=0.4,
            max_tokens=600,
        )

        await self._conversations.add_assistant_message(
            context, answer, intent=plan.intent.value, date_phrase=plan.date_phrase
        )
        await self._memory.maybe_update_summary(context)

        return ChatResponse(
            session_id=context.session_id,
            message=ResponseMessage(content=answer),
            metadata=ChatMetadata(
                intent=plan.intent,
                tools_used=tools_used,
                # An unavailability NOTE is not data. tool_results is
                # non-empty in that case -- it carries the explanation the
                # model answers from -- so a bare bool(tool_results) reported
                # data_used=True for a request that read nothing at all, and
                # every dashboard built on this metric would count outages as
                # successful data reads.
                data_used=bool(tool_results) and unavailable_reason is None,
                calculation_used=(
                    plan.requires_calculation and bool(tool_results) and unavailable_reason is None
                ),
                needs_clarification=False,
                period_start=plan.date_range.start if plan.date_range else None,
                period_end=plan.date_range.end if plan.date_range else None,
                period_phrase=plan.date_phrase,
            ),
        )
