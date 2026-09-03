from __future__ import annotations

from decimal import Decimal

from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_recurring_cost


async def get_subscriptions_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    subs = await financial_service.get_subscriptions(context)
    monthly_equivalents = [
        s.amount.amount if s.billing_cycle == "monthly" else s.amount.amount / Decimal("12")
        for s in subs
        if s.is_active
    ]
    return {
        "subscriptions": [
            {
                "name": s.name,
                "amount": str(s.amount.amount),
                "billing_cycle": s.billing_cycle,
                "next_charge_date": s.next_charge_date.isoformat() if s.next_charge_date else None,
                "is_active": s.is_active,
            }
            for s in subs
        ],
        "active_subscription_count": sum(1 for s in subs if s.is_active),
        "estimated_monthly_cost": str(calculate_recurring_cost(monthly_equivalents)),
        "currency": context.currency,
    }
