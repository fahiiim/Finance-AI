from __future__ import annotations

from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.money import require_same_currency


async def get_investments_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    holdings = await financial_service.get_investments(context)

    # No holdings is not a portfolio worth nothing. This backend has no
    # holdings table at all, so an empty list meant "we cannot see this",
    # and the tool reported total_market_value "0" -- which reads as a
    # balance, and had the assistant tell users with real portfolios that
    # their investments were worth zero.
    if not holdings:
        return {
            "availability": DataAvailability.MISSING.value,
            "holdings": [],
            "holding_count": 0,
            "total_market_value": None,
            "note": (
                "No investment holdings are available for this user. This means the data is NOT "
                "CONNECTED, not that the portfolio is empty or worth zero. Say that investment "
                "data is not available here -- never state or imply a value."
            ),
            "currency": context.currency,
        }

    require_same_currency([h.market_value.currency for h in holdings])
    total_value = sum((h.market_value.amount for h in holdings), Decimal("0"))
    return {
        "availability": DataAvailability.AVAILABLE.value,
        "holdings": [
            {
                "name": h.name,
                "symbol": h.symbol,
                "quantity": str(h.quantity),
                "market_value": str(h.market_value.amount),
            }
            for h in holdings
        ],
        "holding_count": len(holdings),
        "total_market_value": str(total_value),
        "currency": context.currency,
        "note": "Market values reflect the backend's last recorded valuation, not guaranteed real-time pricing.",
    }
