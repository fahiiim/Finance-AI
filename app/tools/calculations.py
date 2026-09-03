"""
Deterministic calculation engine.

Every function here is pure Python over already-retrieved data — no LLM
involvement, no I/O. This is what makes financial arithmetic reproducible
and testable (see tests/unit/test_calculations.py). The LLM only ever
explains these results; it never recomputes them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.financial import Transaction
from app.utils.categories import (
    matches_category,
    matches_subcategory,
    resolve_category,
    subcategory_requested,
)
from app.utils.merchants import find_merchant_matches
from app.utils.money import percentage_change, quantize, require_same_currency, sum_amounts
from app.utils.transactions import expense_amount, expenses, income, income_amount, is_expense


def calculate_total_spending(transactions: list[Transaction]) -> Decimal:
    """Sum of all outflows as a positive total. See utils/transactions.py for the sign convention."""
    outflows = expenses(transactions)
    require_same_currency([t.currency for t in outflows])
    return sum_amounts([expense_amount(t) for t in outflows])


def calculate_total_income(transactions: list[Transaction]) -> Decimal:
    inflows = income(transactions)
    require_same_currency([t.currency for t in inflows])
    return sum_amounts([income_amount(t) for t in inflows])


def calculate_category_total(transactions: list[Transaction], category: str) -> Decimal:
    """
    Total for one category, resolving the caller's word to the data's label.

    An exact label still matches exactly -- resolve_category scores that
    highest -- so callers passing a label read out of the data are unaffected.
    What changes is the caller passing a user's phrase: "restaurants" against
    a "Food And Drink" label used to sum an empty list and return a confident
    zero.
    """
    match = resolve_category(category, {t.category for t in transactions if t.category})
    if match is None:
        return Decimal("0")
    matched = [t for t in transactions if is_expense(t) and matches_category(t.category, match)]

    # Narrow to the finer label too, when the caller named one and the data
    # carries it. Resolving the category but not the subcategory made this
    # function disagree with the tool layer about the same question:
    # "restaurants" totalled $134.13 through apply_filters and $206.57 here,
    # because this one silently answered about all food and drink. Two
    # totals for one question is worse than one wrong total -- neither can
    # be trusted, and nothing on screen says which is which.
    wanted = match.subcategory or subcategory_requested(category)
    if wanted:
        narrowed = [t for t in matched if matches_subcategory(t.subcategory, wanted)]
        # Only when it selects something. No finer labels in the data means
        # the parent total is the honest answer -- the tool layer says so in
        # its notes, and returning zero here would contradict it.
        if narrowed:
            matched = narrowed

    require_same_currency([t.currency for t in matched])
    return sum_amounts([expense_amount(t) for t in matched])


def calculate_merchant_total(transactions: list[Transaction], merchant: str) -> Decimal:
    """
    Total at one merchant, matched on names rather than raw substrings.

    The substring test this replaces could not see past the payment rails:
    "Wendy's" never matched the "WENDY S 3319" the statement actually carries.
    See app/utils/merchants.py for what the descriptors look like.
    """
    matched = [
        t
        for group in find_merchant_matches(transactions, merchant)
        for t in group.transactions
        if is_expense(t)
    ]
    require_same_currency([t.currency for t in matched])
    return sum_amounts([expense_amount(t) for t in matched])


def calculate_average_spending(transactions: list[Transaction], *, num_periods: int) -> Decimal | None:
    if num_periods <= 0:
        return None
    total = calculate_total_spending(transactions)
    return quantize(total / Decimal(num_periods))


def calculate_average_daily_spending(transactions: list[Transaction], *, start: date, end: date) -> Decimal | None:
    num_days = (end - start).days + 1
    if num_days <= 0:
        return None
    total = calculate_total_spending(transactions)
    return quantize(total / Decimal(num_days))


def calculate_percentage_change(current: Decimal, previous: Decimal) -> Decimal | None:
    """Returns None when undefined (previous == 0) rather than 0 or an error — see utils/money.py."""
    return percentage_change(current, previous)


def calculate_savings_rate(total_income: Decimal, total_expenses: Decimal) -> Decimal | None:
    if total_income == 0:
        return None
    rate = ((total_income - total_expenses) / total_income) * Decimal("100")
    return rate.quantize(Decimal("0.1"))


def calculate_budget_usage(budgeted: Decimal, spent: Decimal) -> Decimal | None:
    if budgeted == 0:
        return None
    return ((spent / budgeted) * Decimal("100")).quantize(Decimal("0.1"))


def calculate_cash_flow(total_income: Decimal, total_expenses: Decimal) -> Decimal:
    return quantize(total_income - total_expenses)


def calculate_recurring_cost(recurring_amounts: list[Decimal]) -> Decimal:
    return sum_amounts(recurring_amounts)


def calculate_debt_ratio(total_monthly_debt_payments: Decimal, total_monthly_income: Decimal) -> Decimal | None:
    """Classic debt-to-income style ratio, expressed as a percentage."""
    if total_monthly_income == 0:
        return None
    return ((total_monthly_debt_payments / total_monthly_income) * Decimal("100")).quantize(Decimal("0.1"))


def calculate_emergency_fund_coverage(liquid_savings: Decimal, average_monthly_expenses: Decimal) -> Decimal | None:
    """Returns coverage in months (e.g. 3.2 = 3.2 months of expenses covered)."""
    if average_monthly_expenses == 0:
        return None
    return (liquid_savings / average_monthly_expenses).quantize(Decimal("0.1"))
