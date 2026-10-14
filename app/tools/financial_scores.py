"""
Financial score tool.

Per spec section 32: if the backend already computes an authoritative
financial score, this tool retrieves and passes it through — it never
recreates a proprietary scoring formula. If unavailable, that's reported
explicitly rather than silently omitted.
"""

from __future__ import annotations

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService


async def get_financial_score_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    score = await financial_service.get_financial_score(context)
    if score is None:
        return {"availability": DataAvailability.MISSING.value}
    result: dict = {
        "availability": DataAvailability.AVAILABLE.value,
        "score": score.score,
        "range": {"min": score.score_range_min, "max": score.score_range_max},
        "factors": score.factors,
        "as_of_date": score.as_of_date.isoformat() if score.as_of_date else None,
    }

    # Trend is OPTIONAL enrichment -- a question like "has my score gone up
    # or down?" needs a prior point, but "what is my score?" does not, so no
    # history found is not reported as an error, just omitted.
    history = await financial_service.get_financial_score_history(context)
    previous = next((h for h in history if h.as_of_date != score.as_of_date), None)
    if previous is not None:
        result["previous"] = {"score": previous.score, "as_of_date": previous.as_of_date.isoformat() if previous.as_of_date else None}
        result["change"] = score.score - previous.score

    return result
