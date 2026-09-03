from __future__ import annotations

from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_recurring_cost


async def get_recurring_transactions_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    recurring = await financial_service.get_recurring_transactions(context)
    monthly_amounts = [r.amount.amount for r in recurring if r.frequency == "monthly"]
    return {
        "recurring_expenses": [
            {
                "name": r.name,
                "amount": str(r.amount.amount),
                "frequency": r.frequency,
                "category": r.category,
                "next_expected_date": r.next_expected_date.isoformat() if r.next_expected_date else None,
            }
            for r in recurring
        ],
        "recurring_count": len(recurring),
        "estimated_monthly_recurring_cost": str(calculate_recurring_cost(monthly_amounts)),
        "currency": context.currency,
    }
