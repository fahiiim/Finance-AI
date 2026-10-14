"""
Transaction query tool.

Returns an aggregated summary plus a capped sample of transactions — never
the full raw list — to keep what gets sent to the LLM minimal (spec section
28: context minimization). The full count is always included so the model
can say "17 transactions totaling $X" without needing all 17 rows.
"""

from __future__ import annotations

from datetime import date

from app.core.security import UserContext
from app.schemas.financial import Transaction
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_total_spending
from app.tools.filters import apply_filters
from app.utils.merchants import clean_merchant_name, top_merchants
from app.utils.transactions import expenses

MAX_SAMPLE_TRANSACTIONS = 15


def _sample(transactions: list[Transaction]) -> list[dict]:
    return [
        {
            "date": t.date.isoformat(),
            # The cleaned name, not the statement descriptor. What the user is
            # shown here is what they will type in the next question, and
            # "CHECKCARD 0607 CHEESECAKE SUMMERLIN LAS VEGAS NV XXXXX0061..."
            # is neither readable nor something anyone would type back.
            "merchant": clean_merchant_name(t.merchant_name),
            "category": t.category,
            "subcategory": t.subcategory,
            "amount": str(t.amount),
        }
        for t in transactions[:MAX_SAMPLE_TRANSACTIONS]
    ]


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


async def get_transactions_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    start: date | None = plan.date_range.start if plan.date_range else None
    end: date | None = plan.date_range.end if plan.date_range else None

    # Expenses only, not get_transactions(): every sibling tool
    # (SPENDING_ANALYSIS, CATEGORY_ANALYSIS, MERCHANT_ANALYSIS) already
    # filters to outflows, and an unqualified "show me transactions over
    # $200" / "largest purchase" / "purchases yesterday" means purchases,
    # not deposits -- confirmed live, a manually-entered +$500,000 INCOME
    # row surfaced in a "transactions over $200" answer before this filter.
    # A user who wants inflows asks an income question (INCOME_QUERY).
    transactions = expenses(await financial_service.get_transactions(context, start=start, end=end))

    # min_amount/max_amount applied here instead of at the backend: get_expenses
    # doesn't take them, since it's also used by callers with no amount filter.
    if plan.min_amount is not None:
        transactions = [t for t in transactions if abs(float(t.amount)) >= plan.min_amount]
    if plan.max_amount is not None:
        transactions = [t for t in transactions if abs(float(t.amount)) <= plan.max_amount]

    if plan.weekday_filter is not None:
        transactions = [t for t in transactions if t.date.weekday() == plan.weekday_filter]

    resolved = apply_filters(
        transactions, category_filter=plan.category_filter, merchant_filter=plan.merchant_filter
    )
    transactions = resolved.transactions
    total_spent = calculate_total_spending(transactions)
    result = {
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "filters": {
            "weekday": _WEEKDAY_NAMES[plan.weekday_filter] if plan.weekday_filter is not None else None,
            "category": resolved.category or plan.category_filter,
            "merchant": ", ".join(resolved.merchants) if resolved.merchants else plan.merchant_filter,
            "min_amount": plan.min_amount,
            "max_amount": plan.max_amount,
        },
        "transaction_count": len(transactions),
        "total_outflow": str(total_spent),
        # Grouped by shop as well as listed by row. A 60-transaction month
        # truncates to 15 rows, and a truncated list is a bad basis for "where
        # did my money go" -- these totals cover every transaction, not 15.
        "by_merchant": top_merchants(transactions),
        "sample_transactions": _sample(transactions),
        "sample_truncated": len(transactions) > MAX_SAMPLE_TRANSACTIONS,
        "currency": context.currency,
    }
    result.update(resolved.as_dict())
    return result
