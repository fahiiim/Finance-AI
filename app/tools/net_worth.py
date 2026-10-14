"""
Net worth tool: liquid account balances + investment holdings, minus debt.

Deliberately excludes 'credit' type rows from get_accounts() when summing
assets -- a credit card balance there is the SAME debt get_liabilities()
already reports from liability_accounts. Including it in both would count
it as an asset once and a liability once, which is not a wash: it inflates
both sides and can hide a real negative net worth behind two overstated
numbers that happen to net out.

Each component (liquid accounts, investments, liabilities) carries its own
DataAvailability, mirroring app/tools/investments.py and
app/tools/liabilities.py -- a user with none of some component gets that
line marked unavailable, never silently folded into the total as zero.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.money import require_same_currency


async def get_net_worth_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    # The backend's own figure wins when it serves one. It is what the user
    # sees in the app (it also counts manual assets, property, vehicles and
    # business value, which this service cannot see), so deriving a different
    # number here would make the assistant contradict the dashboard.
    reported = await financial_service.get_net_worth(context)
    if reported is not None:
        require_same_currency(
            [reported.net_worth.currency, reported.total_assets.currency, reported.total_liabilities.currency]
        )
        return {
            "source": "backend",
            "currency": reported.net_worth.currency,
            "net_worth": str(reported.net_worth.amount),
            "total_assets": str(reported.total_assets.amount),
            "total_liabilities": str(reported.total_liabilities.amount),
            "asset_breakdown": {name: str(m.amount) for name, m in reported.asset_breakdown.items()},
            "as_of_date": reported.as_of_date.isoformat() if reported.as_of_date else None,
            "monthly_history": reported.history,
            "note": (
                "This is the net worth the user sees in the app; state it as given. The breakdown "
                "lists where the assets are held -- a component of 0.00 means none, not unknown."
            ),
        }

    accounts = await financial_service.get_accounts(context)
    liquid_accounts = [a for a in accounts if a.type != "credit"]
    investments = await financial_service.get_investments(context)
    liabilities = await financial_service.get_liabilities(context)

    result: dict = {"currency": context.currency}
    net_worth: Decimal | None = None

    if liquid_accounts:
        require_same_currency([a.balance.currency for a in liquid_accounts])
        liquid_total = sum((a.balance.amount for a in liquid_accounts), Decimal("0"))
        result["liquid_accounts"] = {"availability": DataAvailability.AVAILABLE.value, "total": str(liquid_total)}
        net_worth = (net_worth or Decimal("0")) + liquid_total
    else:
        result["liquid_accounts"] = {
            "availability": DataAvailability.MISSING.value,
            "total": None,
            "note": "No checking/savings accounts are linked. This means NOT CONNECTED, not zero balance.",
        }

    if investments:
        require_same_currency([h.market_value.currency for h in investments])
        investment_total = sum((h.market_value.amount for h in investments), Decimal("0"))
        result["investments"] = {"availability": DataAvailability.AVAILABLE.value, "total": str(investment_total)}
        net_worth = (net_worth or Decimal("0")) + investment_total
    else:
        result["investments"] = {
            "availability": DataAvailability.MISSING.value,
            "total": None,
            "note": "No investment holdings are available. This means NOT CONNECTED, not a zero portfolio.",
        }

    if liabilities:
        require_same_currency([li.balance.currency for li in liabilities])
        liability_total = sum((li.balance.amount for li in liabilities), Decimal("0"))
        result["liabilities"] = {"availability": DataAvailability.AVAILABLE.value, "total": str(liability_total)}
        net_worth = (net_worth or Decimal("0")) - liability_total
    else:
        result["liabilities"] = {
            "availability": DataAvailability.MISSING.value,
            "total": None,
            "note": "No debts or credit accounts are on file. This means NOT CONNECTED, not debt-free.",
        }

    if net_worth is None:
        result["net_worth"] = None
        result["note"] = (
            "No assets or liabilities are available for this user at all, so net worth cannot be computed. "
            "Say net worth is unavailable -- do not state or imply zero."
        )
    else:
        result["net_worth"] = str(net_worth)
        excluded = [
            name
            for name in ("liquid_accounts", "investments", "liabilities")
            if result[name]["availability"] == DataAvailability.MISSING.value
        ]
        if excluded:
            result["note"] = (
                f"Net worth is computed only from what's available; {', '.join(excluded)} "
                "could not be included because that data is not connected -- say so, do not imply "
                "those components are zero."
            )

    return result
