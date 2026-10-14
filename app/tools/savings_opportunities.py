"""
Where spending could be trimmed.

Pulls the breakdown the service already has (category and merchant totals
over recent complete months, subscriptions, bank fees) and puts it in one
place, so "where can I save money?" is answered from the user's own numbers
instead of "I don't have a breakdown". The figures are facts; which of them
to cut is the user's call, and the answer should frame ideas as suggestions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.dates import current_date_in, month_bounds
from app.utils.liquidity import INFLOW_CATEGORIES
from app.utils.merchants import top_merchants
from app.utils.money import quantize
from app.utils.transactions import expense_amount, expenses

WINDOW_MONTHS = 3
# Spending that is not realistically "trimmable" in a month: housing and
# utilities, debt service, medical, and money merely moved around.
FIXED_OR_NON_SPENDING = frozenset(
    {"Rent And Utilities", "Loan Payments", "Medical", "Transfer Out", "Transfer In", "Income"}
)


async def get_savings_opportunities_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    today = current_date_in(context.timezone)
    first_this_month = date(today.year, today.month, 1)
    cursor = first_this_month
    for _ in range(WINDOW_MONTHS):
        cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
    window_start = cursor
    _, window_end = month_bounds(*(
        (first_this_month.year - 1, 12) if first_this_month.month == 1
        else (first_this_month.year, first_this_month.month - 1)
    ))

    txns = await financial_service.get_transactions(context, start=window_start, end=window_end)
    outflows = expenses(txns)
    if not outflows:
        return {
            "availability": DataAvailability.MISSING.value,
            "note": "No spending is on record for the last three complete months, so there is nothing to break down.",
        }

    # Inflow-only categories on an expense-signed row are a deposit keyed in
    # backwards (see INFLOW_CATEGORIES); they are not spending to trim.
    discretionary = [
        t for t in outflows
        if (t.category or "Uncategorised") not in FIXED_OR_NON_SPENDING
        and (t.category or "").lower() not in INFLOW_CATEGORIES
    ]

    by_category: dict[str, Decimal] = {}
    for t in discretionary:
        key = t.category or "Uncategorised"
        by_category[key] = by_category.get(key, Decimal("0")) + expense_amount(t)
    ranked = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)[:8]

    discretionary_total = sum((expense_amount(t) for t in discretionary), Decimal("0"))
    monthly_discretionary = quantize(discretionary_total / WINDOW_MONTHS)

    subs = await financial_service.get_subscriptions(context)
    sub_monthly = sum(
        (s.amount.amount if s.billing_cycle == "monthly" else s.amount.amount / Decimal("12") for s in subs if s.is_active),
        Decimal("0"),
    )

    fees = sum((expense_amount(t) for t in outflows if t.category == "Bank Fees"), Decimal("0"))

    return {
        "availability": DataAvailability.AVAILABLE.value,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat(), "months": WINDOW_MONTHS},
        "average_monthly_discretionary_spending": str(monthly_discretionary),
        "top_discretionary_categories": [
            {"category": c, "average_per_month": str(quantize(v / WINDOW_MONTHS)), "total": str(quantize(v))}
            for c, v in ranked
        ],
        "top_merchants": top_merchants(discretionary, limit=8),
        "recurring_monthly_outflows_largest": sorted(
            (
                {
                    "name": s.name,
                    "monthly_equivalent": str(
                        quantize(s.amount.amount if s.billing_cycle == "monthly" else s.amount.amount / Decimal("12"))
                    ),
                }
                for s in subs
                if s.is_active
            ),
            key=lambda row: Decimal(row["monthly_equivalent"]),
            reverse=True,
        )[:8],
        "recurring_monthly_outflows_total": str(quantize(sub_monthly)),
        "recurring_outflows_note": (
            "These are monthly outflows detected from payment patterns, not a confirmed subscription list -- "
            "they can include payroll, tax or business payments. Name only the ones that look like real "
            "subscriptions as candidates to review, and do not present the total as subscription spending."
        ),
        "bank_fees_in_window": str(quantize(fees)),
        "illustration_ten_percent_of_discretionary_per_month": str(quantize(monthly_discretionary / 10)),
        "currency": context.currency,
        "note": (
            "Excluded as not realistically trimmable: " + ", ".join(sorted(FIXED_OR_NON_SPENDING)) + ". "
            "Bank fees and subscriptions are usually the easiest to cut. The ten-percent figure is only an "
            "illustration of scale, not a target. Offer these as suggestions the user can accept or ignore -- "
            "never as instructions, and never claim a saving the numbers do not show."
        ),
    }
