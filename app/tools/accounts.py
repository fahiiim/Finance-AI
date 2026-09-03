from __future__ import annotations

from decimal import Decimal

from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.money import require_same_currency


async def get_accounts_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    accounts = await financial_service.get_accounts(context)
    require_same_currency([a.balance.currency for a in accounts])
    total_balance = sum((a.balance.amount for a in accounts), Decimal("0"))
    return {
        "accounts": [
            {"name": a.name, "type": a.type, "balance": str(a.balance.amount), "currency": a.balance.currency}
            for a in accounts
        ],
        "account_count": len(accounts),
        "total_balance": str(total_balance),
        "currency": context.currency,
    }
