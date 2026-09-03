from __future__ import annotations

from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.money import require_same_currency


async def get_liabilities_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    liabilities = await financial_service.get_liabilities(context)

    # Same distinction as investments: no debts on file is not proof of no
    # debt. Reporting "total_balance: 0" for a user whose credit accounts
    # simply are not linked congratulates them on being debt-free.
    if not liabilities:
        return {
            "availability": DataAvailability.MISSING.value,
            "liabilities": [],
            "liability_count": 0,
            "total_balance": None,
            "note": (
                "No debts or credit accounts are on file for this user. This means NOT CONNECTED, "
                "not debt-free. Say the data is not available -- do not congratulate them on "
                "having no debt."
            ),
            "currency": context.currency,
        }

    require_same_currency([li.balance.currency for li in liabilities])
    total_balance = sum((li.balance.amount for li in liabilities), Decimal("0"))
    return {
        "availability": DataAvailability.AVAILABLE.value,
        "liabilities": [
            {
                "name": li.name,
                "type": li.type,
                "balance": str(li.balance.amount),
                "interest_rate": str(li.interest_rate) if li.interest_rate is not None else None,
                "minimum_payment": str(li.minimum_payment.amount) if li.minimum_payment else None,
            }
            for li in liabilities
        ],
        "liability_count": len(liabilities),
        "total_balance": str(total_balance),
        "currency": context.currency,
    }
