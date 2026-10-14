"""
Retirement readiness tool.

Every field here is something the backend already computed (parsed from
the Allo Score snapshot's raw_metrics JSON, served by the backend's
/internal/retirement-readiness). This tool passes it through --
it never computes "years behind" or a required savings rate itself, the
same principle as app/tools/financial_scores.py and app/tools/allo_score.py.
"""

from __future__ import annotations

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService


async def get_retirement_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    readiness = await financial_service.get_retirement_readiness(context)
    if readiness is None:
        return {
            "availability": DataAvailability.MISSING.value,
            "note": "No retirement-readiness data is available for this user.",
        }
    # The closest thing to "money available for retirement" the data holds:
    # investment and deposit accounts, from the backend's net-worth breakdown.
    investable = None
    try:
        net_worth = await financial_service.get_net_worth(context)
        bucket = net_worth.asset_breakdown.get("investment_and_depository") if net_worth else None
        investable = str(bucket.amount) if bucket is not None else None
    except Exception:  # noqa: BLE001 - supplementary; the readiness answer stands without it
        investable = None

    return {
        "availability": DataAvailability.AVAILABLE.value,
        "age": readiness.age,
        "target_retirement_age": readiness.target_retirement_age,
        "years_remaining": readiness.years_remaining,
        "retirement_target": str(readiness.retirement_target.amount) if readiness.retirement_target else None,
        # A PROJECTION at retirement age -- not what the user has saved today
        # (see retirement_balance_note).
        "projected_future_assets": (
            str(readiness.future_assets.amount) if readiness.future_assets else None
        ),
        "current_position": {
            "total_assets_all_types": (
                str(readiness.total_assets.amount) if readiness.total_assets else None
            ),
            "total_assets_note": (
                "Includes business, property, vehicle and manually tracked assets -- not just "
                "investments and deposits. Do not describe it as investment or retirement savings."
            ),
            "investment_and_deposit_accounts": investable,
            "total_cash": str(readiness.total_cash.amount) if readiness.total_cash else None,
            "total_debt": str(readiness.total_debt.amount) if readiness.total_debt else None,
            "cumulative_net_savings": str(readiness.total_savings.amount) if readiness.total_savings else None,
            "investment_contributions": (
                str(readiness.investment_contributions.amount) if readiness.investment_contributions else None
            ),
            "investment_contribution_months": readiness.investment_contribution_months,
        },
        "retirement_balance_note": (
            "The data does not separate retirement-designated accounts (401k/IRA) from other assets, "
            "so there is NO figure for 'saved for retirement'. Do not quote projected_future_assets "
            "as savings. Report current_position as what exists today and say the "
            "retirement-specific balance is not broken out."
        ),
        "monthly_retirement_income_goal": (
            str(readiness.monthly_retirement_income_goal.amount)
            if readiness.monthly_retirement_income_goal
            else None
        ),
        "annual_contribution": (
            str(readiness.annual_contribution.amount) if readiness.annual_contribution else None
        ),
        "savings_rate": str(readiness.savings_rate) if readiness.savings_rate is not None else None,
        "investment_rate": str(readiness.investment_rate) if readiness.investment_rate is not None else None,
        "readiness_ratio": str(readiness.readiness_ratio) if readiness.readiness_ratio is not None else None,
        "readiness_ratio_note": (
            "readiness_ratio is the backend's own on-track measure: roughly 1.0 or above means on "
            "track to hit the retirement target, below 1.0 means behind. State this plainly rather "
            "than inventing a different metric."
        ),
        "currency": context.currency,
    }
