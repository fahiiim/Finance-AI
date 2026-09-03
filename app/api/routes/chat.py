from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_orchestrator
from app.core.exceptions import ValidationFailedError
from app.core.rate_limit import enforce_rate_limit
from app.core.security import UserContext, get_user_context
from app.orchestrator.orchestrator import Orchestrator
from app.schemas.chat import ChatRequest
from app.schemas.responses import ChatResponse

router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Send a chat message to the financial assistant",
    description=(
        "Runs the full pipeline: authenticated user context -> conversation "
        "context -> intent detection -> execution planning -> financial data "
        "retrieval -> deterministic calculation -> minimal verified context -> "
        "OpenAI -> response validation. User identity is derived exclusively "
        "from the Authorization/X-Session-Id headers, never from the request body."
    ),
    responses={
        401: {"description": "Missing, invalid, or expired authentication."},
        413: {"description": "Message exceeds the maximum allowed size."},
        422: {"description": "Request failed validation, or body session_id disagrees with the authenticated session."},
        429: {"description": "Rate limit exceeded for this user."},
        502: {"description": "The language model or backend data service returned an error."},
        503: {"description": "Financial data is temporarily unavailable."},
        504: {"description": "The language model or backend data service timed out."},
    },
)
async def chat(
    request: ChatRequest,
    context: UserContext = Depends(get_user_context),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ChatResponse:
    # The body's session_id is NOT used for scoping — context.session_id, from
    # the verified token/header, is. But leaving a divergence unreported means
    # a confused client silently writes into a different conversation than the
    # one it named. Reject the mismatch instead of quietly picking a winner.
    if request.session_id != context.session_id:
        raise ValidationFailedError("session_id in the request body does not match the authenticated session.")

    # Checked AFTER authentication so the limit is keyed to a verified user_id
    # rather than to anything a caller could rotate freely, and BEFORE the
    # orchestrator runs so a throttled request costs no OpenAI calls.
    enforce_rate_limit(context)

    return await orchestrator.run(request, context)
