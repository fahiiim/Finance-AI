from __future__ import annotations

from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService


async def get_bills_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    start = plan.date_range.start if plan.date_range else None
    end = plan.date_range.end if plan.date_range else None
    bills = await financial_service.get_bills(context, start=start, end=end)
    return {
        "bills": [
            {
                "name": b.name,
                "amount": str(b.amount.amount),
                "due_date": b.due_date.isoformat(),
                "is_paid": b.is_paid,
            }
            for b in bills
        ],
        "bill_count": len(bills),
        "unpaid_count": sum(1 for b in bills if not b.is_paid),
        "currency": context.currency,
    }
