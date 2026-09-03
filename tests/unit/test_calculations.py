from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.financial import Transaction
from app.tools.calculations import (
    calculate_average_daily_spending,
    calculate_budget_usage,
    calculate_category_total,
    calculate_debt_ratio,
    calculate_emergency_fund_coverage,
    calculate_percentage_change,
    calculate_savings_rate,
    calculate_total_income,
    calculate_total_spending,
)

# SIGN CONVENTION: positive = money OUT (a purchase), negative = money IN
# (a deposit). This is Plaid's convention and what the production database
# actually stores — verified against 122,326 real rows. See
# app/utils/transactions.py. These tests previously encoded the opposite and
# passed, because the code under test shared the same wrong assumption.


def _txn(amount: str, day: date = date(2026, 8, 1), category: str | None = None, merchant: str | None = None) -> Transaction:
    return Transaction(
        transaction_id="t1", account_id="a1", amount=Decimal(amount), date=day, category=category, merchant_name=merchant
    )


def test_calculate_total_spending_sums_outflows_only():
    """Four purchases (positive) plus one salary deposit (negative)."""
    txns = [_txn("50.00"), _txn("120.00"), _txn("80.00"), _txn("45.00"), _txn("-2000.00")]
    assert calculate_total_spending(txns) == Decimal("295.00")


def test_calculate_total_income_sums_inflows_only():
    """One purchase (positive) plus two paycheques (negative)."""
    txns = [_txn("50.00"), _txn("-1500.00"), _txn("-1500.00")]
    assert calculate_total_income(txns) == Decimal("3000.00")


def test_spending_and_income_are_not_swapped():
    """
    Regression guard for the inversion this suite used to encode: a salary
    deposit must never be counted as spending, and a purchase must never be
    counted as income.
    """
    salary = _txn("-3000.00")
    purchase = _txn("75.00")

    assert calculate_total_spending([salary, purchase]) == Decimal("75.00")
    assert calculate_total_income([salary, purchase]) == Decimal("3000.00")


def test_calculate_category_total():
    txns = [
        _txn("40.00", category="Dining"),
        _txn("20.00", category="Dining"),
        _txn("10.00", category="Groceries"),
    ]
    assert calculate_category_total(txns, "dining") == Decimal("60.00")


def test_percentage_change_normal_case():
    assert calculate_percentage_change(Decimal("120"), Decimal("100")) == Decimal("20.0")


def test_percentage_change_zero_baseline_is_none_not_zero_or_error():
    assert calculate_percentage_change(Decimal("50"), Decimal("0")) is None


def test_savings_rate():
    assert calculate_savings_rate(Decimal("4000"), Decimal("3000")) == Decimal("25.0")


def test_savings_rate_zero_income_is_none():
    assert calculate_savings_rate(Decimal("0"), Decimal("100")) is None


def test_budget_usage():
    assert calculate_budget_usage(Decimal("500"), Decimal("250")) == Decimal("50.0")


def test_average_daily_spending():
    txns = [_txn("100.00", day=date(2026, 8, 1)), _txn("100.00", day=date(2026, 8, 2))]
    result = calculate_average_daily_spending(txns, start=date(2026, 8, 1), end=date(2026, 8, 2))
    assert result == Decimal("100.00")


def test_debt_ratio():
    assert calculate_debt_ratio(Decimal("500"), Decimal("5000")) == Decimal("10.0")


def test_emergency_fund_coverage():
    assert calculate_emergency_fund_coverage(Decimal("6000"), Decimal("2000")) == Decimal("3.0")
