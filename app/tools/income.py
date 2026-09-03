from __future__ import annotations

from app.core.exceptions import ValidationFailedError
from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_total_income


async def get_income_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    if plan.date_range is None:
        raise ValidationFailedError("An income query requires a resolved date range.")

    transactions = await financial_service.get_income(
        context, start=plan.date_range.start, end=plan.date_range.end
    )
    total = calculate_total_income(transactions)
    return {
        "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
        "total_income": str(total),
        "transaction_count": len(transactions),
        "currency": context.currency,
    }
