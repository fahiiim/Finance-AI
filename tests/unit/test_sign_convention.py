"""
Sign convention regression tests.

The convention is Plaid's: POSITIVE = money out (a purchase), NEGATIVE =
money in (a deposit). Verified against 122,326 production rows —
known purchases were 18,300 positive vs 167 negative, known payroll/deposits
were 7,557 negative vs 6 positive.

This suite exists because the codebase previously encoded the OPPOSITE rule
in eight separate places, and its tests encoded the same inversion, so
everything agreed with itself and nothing failed. These tests anchor to the
real-world meaning ("a salary is income") rather than to a sign, so they
cannot drift together with the implementation again.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.financial import Transaction
from app.tools.calculations import calculate_total_income, calculate_total_spending
from app.utils.transactions import expenses, income, is_expense, is_income


def _txn(amount: str, merchant: str) -> Transaction:
    return Transaction(
        transaction_id=f"t-{merchant}",
        account_id="a1",
        amount=Decimal(amount),
        date=date(2026, 8, 1),
        merchant_name=merchant,
    )


# Shaped exactly like the production rows they are named after.
SALARY = _txn("-3200.00", "Employer Payroll Direct Deposit")
GROCERIES = _txn("84.10", "Walmart")
STREAMING = _txn("15.99", "Netflix")


def test_a_salary_deposit_is_income_not_spending():
    assert is_income(SALARY)
    assert not is_expense(SALARY)


def test_a_purchase_is_spending_not_income():
    for purchase in (GROCERIES, STREAMING):
        assert is_expense(purchase)
        assert not is_income(purchase)


def test_totals_are_not_inverted():
    txns = [SALARY, GROCERIES, STREAMING]
    assert calculate_total_spending(txns) == Decimal("100.09")
    assert calculate_total_income(txns) == Decimal("3200.00")


def test_filters_partition_transactions_completely():
    txns = [SALARY, GROCERIES, STREAMING]
    assert len(expenses(txns)) + len(income(txns)) == len(txns)


def test_in_memory_backend_generates_data_in_the_real_convention():
    """
    Local dev must model production, not a mirror image of it — otherwise the
    whole pipeline can be validated against data that is wrong in exactly the
    way the code is wrong.
    """
    import asyncio

    from tests.conftest import make_user_context
    from tests.support.in_memory_backend import InMemoryBackendClient

    txns = asyncio.run(InMemoryBackendClient().get_transactions(make_user_context(user_id="sign-user")))
    payroll = [t for t in txns if t.merchant_name == "Employer Payroll"]
    purchases = [t for t in txns if t.merchant_name != "Employer Payroll"]

    assert payroll, "expected the test double to generate some income"
    assert all(is_income(t) for t in payroll)
    assert all(is_expense(t) for t in purchases)


def test_in_memory_backend_income_and_expense_splits_are_not_inverted():
    """
    Regression: InMemoryBackendClient OVERRIDES get_income/get_expenses. When the
    sign convention was centralised these two overrides were missed, so in the test double
    mode "how much did I spend last month?" answered $0.00 while every purchase
    sat in the income bucket. Overrides must use the shared predicates.
    """
    import asyncio
    from datetime import date, timedelta

    from tests.conftest import make_user_context
    from tests.support.in_memory_backend import InMemoryBackendClient

    ctx = make_user_context(user_id="sign-test-user")
    end = date.today()
    start = end - timedelta(days=90)
    client = InMemoryBackendClient()

    outflows = asyncio.run(client.get_expenses(ctx, start=start, end=end))
    inflows = asyncio.run(client.get_income(ctx, start=start, end=end))

    assert outflows, "expected the test double to generate spending"
    assert inflows, "expected the test double to generate income"
    assert all(is_expense(t) for t in outflows)
    assert all(is_income(t) for t in inflows)
    # Payroll is income, never spending.
    assert all(t.merchant_name != "Employer Payroll" for t in outflows)
    assert all(t.merchant_name == "Employer Payroll" for t in inflows)


def test_financial_summary_reports_positive_magnitudes():
    """
    Regression: the summary summed income RAW. Income is stored negative, so
    total_income came back negative and calculate_savings_rate returned an
    impossible 160%. Both totals are magnitudes and must be positive.
    """
    import asyncio
    from datetime import date, timedelta

    from app.tools.calculations import calculate_savings_rate
    from tests.conftest import make_user_context
    from tests.support.in_memory_backend import InMemoryBackendClient

    ctx = make_user_context(user_id="summary-user")
    end = date.today()
    start = end - timedelta(days=90)
    s = asyncio.run(InMemoryBackendClient().get_financial_summary(ctx, start=start, end=end))

    assert s.total_income.amount > 0, "income must be a positive magnitude"
    assert s.total_expenses.amount > 0, "expenses must be a positive magnitude"
    assert s.net_cash_flow.amount == s.total_income.amount - s.total_expenses.amount

    # Deliberately NOT asserting a range. A savings rate below -100% is legal
    # arithmetic (spending more than twice your income), and a partial month
    # produces exactly that in test data. Assert the formula instead: the bug
    # being guarded against was a SIGN error, which shows up as a rate above
    # +100% -- impossible, since you cannot save more than you earned.
    rate = calculate_savings_rate(s.total_income.amount, s.total_expenses.amount)
    assert rate is not None
    expected = ((s.total_income.amount - s.total_expenses.amount) / s.total_income.amount) * 100
    assert abs(rate - expected) < Decimal("0.1")
    assert rate <= 100, f"savings rate {rate}% exceeds 100% -- income sign is inverted"
