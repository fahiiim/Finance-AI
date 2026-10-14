"""
Spending since the most recent paycheck.

Anchored to the deposit, not the calendar month: "since my last paycheck" on
the 20th, with pay landing on the 5th, is the 15 days since the 5th. The old
answer reported month-to-date and merely named the 1st as "your paycheck".
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_total_spending
from app.utils.dates import current_date_in
from app.utils.liquidity import INFLOW_CATEGORIES
from app.utils.merchants import clean_merchant_name, top_merchants
from app.utils.money import quantize
from app.utils.transactions import income_amount

LOOKBACK_DAYS = 120
OUTLIER_MULTIPLE = Decimal("10")
# A deposit smaller than this is not treated as a paycheck (refunds, interest).
MIN_PAYCHECK_AMOUNT = Decimal("200")
NOT_PAY_CATEGORIES = frozenset({"Transfer In", "Transfer Out"})


async def get_spending_since_paycheck_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    today = current_date_in(context.timezone)
    deposits = await financial_service.get_income(context, start=today - timedelta(days=LOOKBACK_DAYS), end=today)
    candidates = [
        t for t in deposits
        if income_amount(t) >= MIN_PAYCHECK_AMOUNT and (t.category or "") not in NOT_PAY_CATEGORIES
    ]
    # A single deposit many times larger than the typical one is a test or
    # one-off entry (a $500,000 "paycheck" next to $2,000 ones), not payday.
    if len(candidates) >= 3:
        amounts = sorted(income_amount(t) for t in candidates)
        median = amounts[len(amounts) // 2]
        candidates = [t for t in candidates if income_amount(t) <= median * OUTLIER_MULTIPLE]

    # Prefer deposits the data itself calls income; fall back to any large inflow.
    labelled = [t for t in candidates if (t.category or "").lower() == "income"]
    pool = labelled or candidates
    if not pool:
        return {
            "availability": DataAvailability.MISSING.value,
            "note": (
                f"No paycheck-sized deposit (at least {MIN_PAYCHECK_AMOUNT}) was found in the last {LOOKBACK_DAYS} "
                "days, so spending cannot be anchored to a paycheck. Say that and offer month-to-date instead."
            ),
        }

    paycheck = max(pool, key=lambda t: (t.date, income_amount(t)))
    spending = await financial_service.get_expenses(context, start=paycheck.date, end=today)
    # The total is reported as stored. Expense-signed rows in inflow-only
    # categories are flagged, not removed, so the answer can say the total
    # includes what look like mis-entered deposits.
    suspect = [t for t in spending if (t.category or "").lower() in INFLOW_CATEGORIES]
    return {
        "possibly_mis_signed_rows": (
            {
                "count": len(suspect),
                "total": str(quantize(sum((abs(t.amount) for t in suspect), Decimal("0")))),
                "examples": [
                    {"merchant": clean_merchant_name(t.merchant_name), "category": t.category,
                     "date": t.date.isoformat(), "amount": str(abs(t.amount))}
                    for t in sorted(suspect, key=lambda t: abs(t.amount), reverse=True)[:3]
                ],
                "note": (
                    "These expense-signed rows sit in income-type categories and look like deposits entered "
                    "with the wrong sign. They ARE included in total_spent_since because the data records them as "
                    "outflows -- say so, and give the total both with and without them."
                ),
            }
            if suspect
            else None
        ),
        "total_spent_since_excluding_suspect_rows": (
            str(quantize(calculate_total_spending([t for t in spending if t not in suspect]))) if suspect else None
        ),
        "availability": DataAvailability.AVAILABLE.value,
        "paycheck": {
            "date": paycheck.date.isoformat(),
            "amount": str(quantize(income_amount(paycheck))),
            "source": clean_merchant_name(paycheck.merchant_name),
            "identified_as": "income-category deposit" if labelled else "largest-recent-deposit fallback",
        },
        "period": {"start": paycheck.date.isoformat(), "end": today.isoformat()},
        "days_since_paycheck": (today - paycheck.date).days,
        "total_spent_since": str(calculate_total_spending(spending)),
        "transaction_count": len(spending),
        "top_merchants": top_merchants(spending, limit=5),
        "currency": context.currency,
        "note": (
            "The paycheck is inferred (there is no payroll flag in the data), so name the date and amount used "
            "and let the user correct it if it was a different deposit."
        ),
    }
