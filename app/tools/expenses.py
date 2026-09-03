from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.exceptions import ValidationFailedError
from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_category_total, calculate_total_spending
from app.tools.filters import apply_filters
from app.utils.merchants import top_merchants
from app.utils.money import quantize
from app.utils.transactions import expense_amount

# Longest window that still gets a per-day series. A month of rows is
# readable; a year of them is noise that displaces the rest of the data.
MAX_DAILY_BREAKDOWN_DAYS = 62


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


async def get_expenses_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    if plan.date_range is None:
        raise ValidationFailedError("An expense query requires a resolved date range.")

    transactions = await financial_service.get_expenses(
        context, start=plan.date_range.start, end=plan.date_range.end
    )

    # A category question must be answered with that category's number. The
    # backend's get_expenses takes no category argument, so narrowing happens
    # here -- and it must happen BEFORE the total is computed. Totalling the
    # unfiltered list and labelling it "groceries" reports a whole month's
    # spending as one category's: right arithmetic, wrong question.
    #
    # Resolution goes through apply_filters rather than a string comparison,
    # because the user's word for a category is rarely the data's label:
    # "restaurants" has to find "Food And Drink", and an exact match on it
    # found nothing and reported $0.00.
    all_categories = sorted({t.category for t in transactions if t.category})
    resolved = apply_filters(
        transactions, category_filter=plan.category_filter, merchant_filter=plan.merchant_filter
    )
    transactions = resolved.transactions
    category = resolved.category

    # "Fridays in June": the window is the month, the days are scattered
    # through it. Narrowing happens here, before the total, for the same
    # reason the category filter does.
    if plan.weekday_filter is not None:
        transactions = [t for t in transactions if t.date.weekday() == plan.weekday_filter]

    total = calculate_total_spending(transactions)

    by_category: dict[str, str] = {}
    if category is None:
        for cat in {t.category for t in transactions if t.category}:
            by_category[cat] = str(calculate_category_total(transactions, cat))
    else:
        # Still keyed by category, so the shape the model reads is identical
        # whether one category was asked about or all of them.
        by_category[category] = str(total)

    # The finer split inside whatever is in scope: Restaurant vs Groceries vs
    # Coffee under Food And Drink. Present only when the data carries it, so
    # its absence is itself informative -- an answer cannot offer a breakdown
    # that does not exist.
    by_subcategory: dict[str, str] = {}
    for sub in {t.subcategory for t in transactions if t.subcategory}:
        by_subcategory[sub] = str(
            quantize(sum((expense_amount(t) for t in transactions if t.subcategory == sub), Decimal("0")))
        )

    # Per-day totals, so "how much did I spend each day this week?" can be
    # answered from data rather than declined. Capped: a year-long window is
    # 365 rows, which would crowd out everything else in the prompt for a
    # question nobody asks day-by-day. Beyond the cap the key is absent and
    # the model says the breakdown is not available -- the same honest answer
    # it already gives, rather than a truncated series read as complete.
    by_date: dict[str, str] = {}
    span_days = (plan.date_range.end - plan.date_range.start).days + 1
    if span_days <= MAX_DAILY_BREAKDOWN_DAYS:
        # Every day in the window, including the ones with no spending: a
        # missing row and a zero row read very differently, and "nothing on
        # Tuesday" is itself an answer.
        daily = {
            day: Decimal("0")
            for day in (plan.date_range.start + timedelta(days=offset) for offset in range(span_days))
            # With a weekday filter the total above covers only those days, so
            # a series spanning the whole month would contradict it -- rows of
            # zero for days that were never counted.
            if plan.weekday_filter is None or day.weekday() == plan.weekday_filter
        }
        for t in transactions:
            if t.date in daily:
                daily[t.date] += expense_amount(t)
        by_date = {day.isoformat(): str(quantize(amount)) for day, amount in sorted(daily.items())}

    result = {
        "weekday": _WEEKDAY_NAMES[plan.weekday_filter] if plan.weekday_filter is not None else None,
        "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
        "total_expenses": str(total),
        "transaction_count": len(transactions),
        "by_category": by_category,
        "by_subcategory": by_subcategory,
        # Named shops, cleaned and grouped. A category total answers "how
        # much"; this answers the question that always follows it, and every
        # name here is one the user can ask about next and get a match.
        "top_merchants": top_merchants(transactions),
        "categories_available_this_period": all_categories,
        "by_date": by_date,
        "currency": context.currency,
    }
    result.update(resolved.as_dict())
    if not by_date:
        result["by_date_note"] = (
            f"A day-by-day breakdown is only provided for windows of "
            f"{MAX_DAILY_BREAKDOWN_DAYS} days or fewer; this one covers {span_days} days."
        )
    # Name the scope explicitly: "total_expenses" alone is ambiguous once the
    # list has been narrowed, and an unlabelled total invites the model to
    # present a category figure as the overall one.
    if resolved.subcategory:
        result["scope"] = f"expenses in '{resolved.subcategory}' within '{category}' only"
    elif category is not None:
        result["scope"] = f"expenses in category '{category}' only"
    elif resolved.merchants:
        result["scope"] = f"expenses at {', '.join(resolved.merchants)} only"

    if not transactions and (category or resolved.merchants):
        result["note"] = (
            f"No expenses matched this filter during {plan.date_range.start.isoformat()} to "
            f"{plan.date_range.end.isoformat()}. This is a real result -- zero -- not missing data."
        )
    elif resolved.filtered_to_nothing:
        # apply_filters already explained WHY nothing matched, in filter_notes.
        # Overwriting that with "you spent zero" is the bug this branch avoids.
        result.pop("note", None)
    return result
