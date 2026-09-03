from __future__ import annotations

from app.core.exceptions import ValidationFailedError
from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_budget_usage


async def get_budgets_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    if plan.date_range is None:
        raise ValidationFailedError("A budget query requires a resolved date range.")

    budgets = await financial_service.get_budgets(
        context, period_start=plan.date_range.start, period_end=plan.date_range.end
    )

    # An empty budget list means no budgets are configured OR that this
    # backend cannot serve them -- either way there is nothing to compare
    # spending against, and an unmarked empty list let the assistant talk
    # about budget adherence it had no figures for.
    if not budgets:
        return {
            "availability": DataAvailability.MISSING.value,
            "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
            "budgets": [],
            "note": (
                "No budgets are available for this user. Say plainly that budgets are not set up "
                "or not available, and stop there. Do NOT offer to show something else instead -- "
                "a closing offer is banned by the response rules, and this note is not an "
                "exception to them. Never invent a budget target or infer one from past spending."
            ),
            "currency": context.currency,
        }

    return {
        "availability": DataAvailability.AVAILABLE.value,
        "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
        "budgets": [
            {
                "category": b.category,
                "budgeted": str(b.budgeted_amount.amount),
                "spent": str(b.spent_amount.amount),
                "usage_percent": (
                    str(calculate_budget_usage(b.budgeted_amount.amount, b.spent_amount.amount))
                    if calculate_budget_usage(b.budgeted_amount.amount, b.spent_amount.amount) is not None
                    else None
                ),
            }
            for b in budgets
        ],
        "currency": context.currency,
    }
