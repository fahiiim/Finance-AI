from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import get_conversation_service
from app.core.security import UserContext, get_user_context
from app.schemas.conversation import ConversationResponse
from app.services.conversation_service import ConversationService

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation session",
    description="Creates an empty conversation scoped to the authenticated user and the caller-supplied session_id header.",
    responses={401: {"description": "Missing or invalid authentication."}, 409: {"description": "Session already exists."}},
)
async def create_conversation(
    context: UserContext = Depends(get_user_context),
    service: ConversationService = Depends(get_conversation_service),
) -> dict:
    await service.ensure_session(context)
    return {"session_id": context.session_id, "status": "created"}


@router.get(
    "/{session_id}",
    response_model=ConversationResponse,
    summary="Get a conversation's history",
    description=(
        "Returns message history for a conversation. Scoped to BOTH the authenticated "
        "user_id and the session_id in the path — a caller can never read another "
        "user's conversation even if they know the session_id."
    ),
    responses={401: {"description": "Missing or invalid authentication."}, 404: {"description": "Conversation not found."}},
)
async def get_conversation(
    session_id: str,
    context: UserContext = Depends(get_user_context),
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationResponse:
    # The path addresses the conversation; the authenticated user_id scopes it.
    # `for_session` carries user_id over unchanged, so this can only ever reach
    # one of the CALLER'S OWN sessions — never another user's, even given an
    # exact session_id. Previously the path value was ignored entirely, which
    # silently returned the header's session instead of the requested one.
    scoped = context.for_session(session_id)
    messages = await service.get_history(scoped)
    summary = await service.get_summary(scoped)
    return ConversationResponse(
        session_id=scoped.session_id, user_id=scoped.user_id, messages=messages, summary=summary
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
    description="Deletes a conversation's history and summary, scoped to the authenticated user.",
    responses={401: {"description": "Missing or invalid authentication."}},
)
async def delete_conversation(
    session_id: str,
    context: UserContext = Depends(get_user_context),
    service: ConversationService = Depends(get_conversation_service),
) -> None:
    # Same scoping rule as the GET above: path addresses, token authorizes.
    await service.delete(context.for_session(session_id))
