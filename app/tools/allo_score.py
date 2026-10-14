"""
Allo Score tool -- the app's own 0-100 financial-health score.

Deliberately separate from app/tools/financial_scores.py: that tool passes
through a credit-bureau score (e.g. 300-850, Equifax/Vantage). This one
passes through a different backend-computed score with its own scale and
per-dimension breakdown (flow/stability/retirement). Same principle as the
credit score tool applies: never recompute the score, pass it through, and
say plainly when it is unavailable rather than inventing one.
"""

from __future__ import annotations

from datetime import date

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService


async def get_allo_score_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    score = await financial_service.get_allo_score(context)
    if score is None:
        return {"availability": DataAvailability.MISSING.value}
    result: dict = {
        "availability": DataAvailability.AVAILABLE.value,
        "overall": {"score": score.overall_score, "tier": score.overall_tier, "insight": score.overall_insight},
        "flow": {"score": score.flow_score, "tier": score.flow_tier, "insight": score.flow_insight},
        "stability": {"score": score.stability_score, "tier": score.stability_tier, "insight": score.stability_insight},
        "retirement": {"score": score.retirement_score, "tier": score.retirement_tier, "insight": score.retirement_insight},
        "period_start": score.period_start.isoformat(),
        "period_end": score.period_end.isoformat(),
    }

    # period_end is the end of the scoring WINDOW, which can fall after today
    # (a snapshot covering a year that is still running). It was reported as
    # "as of September 30" on September 13. It is not an as-of date.
    result["period_note"] = (
        "period_start/period_end describe the scoring window the score covers -- say 'for the period', "
        "never 'as of' that end date"
        + (" (this window ends after today)." if score.period_end > date.today() else ".")
        + " The data holds no simulation, so a what-if ('if I paid off my card...') cannot be quantified: "
        "say the direction in general terms and that the size of the change is not available."
    )

    # Optional enrichment, same reasoning as get_financial_score_tool: only
    # present when a genuinely earlier snapshot exists.
    history = await financial_service.get_allo_score_history(context)
    previous = next((h for h in history if h.period_end != score.period_end), None)
    if previous is not None:
        result["previous"] = {
            "overall_score": previous.overall_score,
            "period_end": previous.period_end.isoformat(),
        }
        result["change"] = {
            "overall": score.overall_score - previous.overall_score,
            "flow": score.flow_score - previous.flow_score,
            "stability": score.stability_score - previous.stability_score,
            "retirement": score.retirement_score - previous.retirement_score,
        }

    return result
