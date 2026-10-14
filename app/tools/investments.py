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

    # Contributions are not in the holdings feed; the backend computes them
    # with the retirement-readiness metrics. Optional: a failure here must not
    # cost the user the holdings answer.
    contributions = None
    try:
        readiness = await financial_service.get_retirement_readiness(context)
    except Exception:  # noqa: BLE001 - supplementary data only
        readiness = None
    if readiness is not None and readiness.investment_contributions is not None:
        contributions = {
            "total_contributed": str(readiness.investment_contributions.amount),
            "months_with_contributions": readiness.investment_contribution_months,
            "note": (
                "The backend's own tally of money contributed to investments over the months it "
                "has contribution data for (not necessarily the calendar year). It is a total, not "
                "a dated list, so it cannot be split by month or by account."
            ),
        }

    total_value = sum((h.market_value.amount for h in holdings), Decimal("0"))

    # Unrealized gain/loss only exists where a cost basis was recorded.
    # Holdings without one are excluded from the total, not counted as
    # zero gain -- a missing cost basis is unknown, not break-even.
    priced_holdings = [h for h in holdings if h.cost_basis is not None]
    total_gain_loss = (
        sum((h.market_value.amount - h.cost_basis.amount for h in priced_holdings), Decimal("0"))
        if priced_holdings
        else None
    )

    return {
        "availability": DataAvailability.AVAILABLE.value,
        "holdings": [
            {
                "name": h.name,
                "symbol": h.symbol,
                "quantity": str(h.quantity),
                "market_value": str(h.market_value.amount),
                "cost_basis": str(h.cost_basis.amount) if h.cost_basis is not None else None,
                "unrealized_gain_loss": (
                    str(h.market_value.amount - h.cost_basis.amount) if h.cost_basis is not None else None
                ),
            }
            for h in holdings
        ],
        "holding_count": len(holdings),
        "total_market_value": str(total_value),
        "total_unrealized_gain_loss": str(total_gain_loss) if total_gain_loss is not None else None,
        "gain_loss_note": (
            "total_unrealized_gain_loss only covers holdings with a recorded cost basis -- "
            "say so if any holding lacks one, do not treat it as zero gain."
            if len(priced_holdings) < len(holdings)
            else None
        ),
        "contributions": contributions,
        "currency": context.currency,
        "note": "Market values reflect the backend's last recorded valuation, not guaranteed real-time pricing.",
        # The data has no valuation HISTORY, so anything period-shaped ("today",
        # "this week", "this month", "this year", "vs the S&P 500") cannot be
        # answered from it. Without this the model presented cumulative
        # unrealized gain as "this week's change".
        "limitations": [
            "unrealized_gain_loss is cumulative since purchase -- NOT a daily, weekly, monthly or "
            "year-to-date change. Never present it as performance for one of those periods; say "
            "that period-by-period performance is not available.",
            "No market-index or benchmark data (S&P 500, Dow) is available, so a comparison against "
            "the market cannot be made. Say so; do not ask which dates to use.",
        ],
    }
