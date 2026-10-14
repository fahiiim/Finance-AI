"""
Savings goal tool.

Answers "am I on track", "how much do I need to save monthly", and "when
will I hit my goal" from ONE stored goal (target, monthly target, deadline)
plus a derived progress figure -- there is no "current progress" column in
the source data, so progress is computed as net savings (income minus
expenses) from the goal's start_date through today, via the same
deterministic path calculate_savings_rate and friends already use.

The projected completion date is an ESTIMATE based on the pace achieved so
far and is labelled as one, the same way app/orchestrator/planner.py labels
an estimated item price -- never presented as a guarantee.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.dates import current_date_in
from app.utils.money import quantize
from app.utils.transactions import expense_amount, income_amount

# Average days per month, for turning a monthly pace into a projected date
# without a calendar-month-adding utility. An approximation, and labelled
# as one in the result -- never presented as an exact date.
_AVG_DAYS_PER_MONTH = Decimal("30.44")


async def get_savings_goal_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    goal = await financial_service.get_savings_goal(context)
    if goal is None:
        return {
            "availability": DataAvailability.MISSING.value,
            "note": "No savings goal is on file for this user. Say so rather than asking them to restate one.",
        }

    today = current_date_in(context.timezone)
    result: dict = {
        "availability": DataAvailability.AVAILABLE.value,
        "target_amount": str(goal.target_amount.amount),
        "monthly_target": str(goal.monthly_target.amount) if goal.monthly_target else None,
        "start_date": goal.start_date.isoformat() if goal.start_date else None,
        "end_date": goal.end_date.isoformat() if goal.end_date else None,
        "currency": context.currency,
    }

    if goal.start_date is None or goal.start_date > today:
        result["progress_note"] = "The goal's start date is missing or in the future, so progress cannot be measured yet."
        return result

    if goal.saved_amount is not None:
        # The backend's own progress figure -- what the user sees in the app.
        # Preferred over a derivation: its rules for transfers and business
        # flows are the ones the dashboard uses, and a different number here
        # would have the assistant contradict the app.
        progress = goal.saved_amount.amount
        result["progress_source"] = "backend"
    else:
        period_end = min(goal.end_date, today) if goal.end_date else today
        income = await financial_service.get_income(context, start=goal.start_date, end=period_end)
        expenses = await financial_service.get_expenses(context, start=goal.start_date, end=period_end)
        progress = sum((income_amount(t) for t in income), Decimal("0")) - sum(
            (expense_amount(t) for t in expenses), Decimal("0")
        )
        result["progress_source"] = "derived_from_transactions"
    result["progress_saved"] = str(quantize(progress))

    remaining = goal.target_amount.amount - progress
    result["remaining_to_target"] = str(quantize(remaining))

    if goal.end_date:
        days_remaining = (goal.end_date - today).days
        result["days_remaining"] = days_remaining
        if days_remaining > 0 and remaining > 0:
            months_remaining = Decimal(days_remaining) / _AVG_DAYS_PER_MONTH
            result["required_monthly_pace"] = str(quantize(remaining / months_remaining))
        elif remaining <= 0:
            result["required_monthly_pace"] = "0"
            result["goal_already_reached"] = True

    elapsed_days = (today - goal.start_date).days
    if elapsed_days >= 7 and remaining > 0:
        elapsed_months = Decimal(elapsed_days) / _AVG_DAYS_PER_MONTH
        actual_pace = progress / elapsed_months
        result["actual_monthly_pace"] = str(quantize(actual_pace))
        if actual_pace > 0:
            months_needed = remaining / actual_pace
            projected_days = int(months_needed * _AVG_DAYS_PER_MONTH)
            result["projected_completion_date_estimate"] = (today + timedelta(days=projected_days)).isoformat()
            result["projection_note"] = (
                "This projected date is an ESTIMATE based on the saving pace achieved so far, not a guarantee -- "
                "label it as an estimate in the answer."
            )
        else:
            result["projected_completion_date_estimate"] = None
            result["projection_note"] = (
                "At the current pace (zero or negative net savings), the goal will not be reached -- say so plainly."
            )

    return result
