"""
Tests for the cash-position, weekly cash-flow, debt-scenario, savings,
since-paycheck, income-by-source and overview tools.

Each guards a failure observed in the client's QA: cash taken from ONE
account, weekly breakdowns declared unavailable, explanatory answers about
future dates, payoff questions refused, and totals that did not reconcile.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.orchestrator.intent_router import _elapsed_range, _weekly_cash_flow, route_and_execute
from app.schemas.financial import (
    Account,
    Bill,
    FinancialSummary,
    Liability,
    Money,
    Subscription,
    Transaction,
)
from app.schemas.planner import ExecutionPlan, Intent, ResolvedDateRange
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient

USD = "USD"


def _m(value: str) -> Money:
    return Money(amount=Decimal(value), currency=USD)


def _plan(intent: Intent, **kw) -> ExecutionPlan:
    return ExecutionPlan(intent=intent, requires_financial_data=True, requires_calculation=False, **kw)


def _txn(uid, tid, amount, day, *, category=None, name="MERCHANT"):
    return Transaction(owner_user_id=uid, transaction_id=tid, account_id="a", amount=Decimal(str(amount)),
                       currency=USD, date=day, merchant_name=name, category=category)


def _mid_month(months_back: int) -> date:
    """The 15th of the month `months_back` before this one -- always inside a complete month."""
    first = date.today().replace(day=1)
    year, month = first.year, first.month - months_back
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, 15)


class _CashClient(InMemoryBackendClient):
    async def get_accounts(self, context):
        u = context.user_id
        return [
            Account(owner_user_id=u, account_id="c1", name="Checking", type="checking", balance=_m("1000")),
            Account(owner_user_id=u, account_id="s1", name="Savings", type="savings", balance=_m("4000")),
            Account(owner_user_id=u, account_id="cc", name="Card", type="credit", balance=_m("900")),
        ]

    async def get_bills(self, context, *, start=None, end=None):
        today = date.today()
        return [
            Bill(owner_user_id=context.user_id, bill_id="b1", name="Rent", amount=_m("500"),
                 due_date=today, is_paid=False),
            Bill(owner_user_id=context.user_id, bill_id="b2", name="Paid already", amount=_m("999"),
                 due_date=today, is_paid=True),
        ]

    async def get_financial_summary(self, context, *, start, end):
        return FinancialSummary(owner_user_id=context.user_id, period_start=start, period_end=end,
                                total_income=_m("3000"), total_expenses=_m("1500"),
                                net_cash_flow=_m("1500"))

    async def get_transactions(self, context, **kwargs):
        # One spending row in each of the three complete history months:
        # completeness now comes from the transactions themselves.
        return [_txn(context.user_id, "t1", 300, _mid_month(1), category="Rent And Utilities"),
                _txn(context.user_id, "t2", 300, _mid_month(2), category="Food And Drink"),
                _txn(context.user_id, "t3", 300, _mid_month(3), category="Food And Drink")]


@pytest.mark.asyncio
async def test_cash_is_summed_across_every_liquid_account_not_one():
    fs = FinancialService(_CashClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.LIQUIDITY_QUERY))

    # 1000 checking + 4000 savings; the credit card is debt, not cash.
    assert result["liquid_cash_total"] == "5000.00"
    assert {a["name"] for a in result["liquid_accounts"]} == {"Checking", "Savings"}
    # Paid bills are not obligations.
    assert result["upcoming_unpaid_bills_to_month_end"] == "500.00"
    # 900 of spending across the 3 complete months with data = 300/month.
    assert result["average_monthly_expenses"] == "300.00"
    assert result["emergency_fund_months_all_spending"] == "16.7"
    assert result["emergency_fund_status"] == "6_plus_months"
    assert "buffer_held_back" in result and "assumptions" in result


@pytest.mark.asyncio
async def test_income_labelled_rows_are_left_out_of_typical_spending_and_disclosed():
    class _Junk(_CashClient):
        async def get_transactions(self, context, **kwargs):
            return [
                _txn(context.user_id, "s1", 300, _mid_month(1), category="Food And Drink"),
                _txn(context.user_id, "s2", 300, _mid_month(2), category="Food And Drink"),
                _txn(context.user_id, "s3", 300, _mid_month(3), category="Food And Drink"),
                # expense-signed but categorised Income: a deposit keyed in backwards
                _txn(context.user_id, "j", 500000, _mid_month(1), category="Income"),
            ]

    fs = FinancialService(_Junk())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.LIQUIDITY_QUERY))
    assert result["average_monthly_expenses"] == "300.00"
    assert result["excluded_from_typical_spending"]["amount"] == "500000.00"


@pytest.mark.asyncio
async def test_the_hardest_upcoming_week_is_the_one_with_the_most_going_out():
    class _Weeks(_CashClient):
        async def get_bills(self, context, *, start=None, end=None):
            today = date.today()
            return [
                Bill(owner_user_id=context.user_id, bill_id="a", name="Small", amount=_m("50"),
                     due_date=today + timedelta(days=1), is_paid=False),
                Bill(owner_user_id=context.user_id, bill_id="b", name="Big", amount=_m("900"),
                     due_date=today + timedelta(days=15), is_paid=False),
            ]

    result, _ = await route_and_execute(
        FinancialService(_Weeks()), make_user_context(user_id="1"), _plan(Intent.LIQUIDITY_QUERY)
    )
    weeks = result["next_four_weeks_outflow"]
    assert len(weeks) == 4
    assert result["hardest_upcoming_week"]["bill_names"] == ["Big"]
    assert weeks[2] == result["hardest_upcoming_week"]


@pytest.mark.asyncio
async def test_no_cash_account_is_unknown_not_zero():
    class _NoCash(InMemoryBackendClient):
        async def get_accounts(self, context):
            return [Account(owner_user_id=context.user_id, account_id="cc", name="Card", type="credit",
                            balance=_m("50"))]

    fs = FinancialService(_NoCash())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.LIQUIDITY_QUERY))
    assert result["availability"] == "missing"
    assert "liquid_cash_total" not in result


@pytest.mark.asyncio
async def test_affordability_uses_total_liquid_cash():
    fs = FinancialService(_CashClient())
    plan = _plan(Intent.AFFORDABILITY_ANALYSIS, amount_in_question=1500.0)
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), plan)
    assert Decimal(result["available_balance"]) == Decimal("5000")


def test_a_future_window_is_cut_off_at_today():
    today = date.today()
    start, end, note = _elapsed_range(
        ResolvedDateRange(start=today - timedelta(days=5), end=today + timedelta(days=25), label="x")
    )
    assert end == today and start == today - timedelta(days=5) and note

    start, end, note = _elapsed_range(
        ResolvedDateRange(start=today + timedelta(days=1), end=today + timedelta(days=30), label="x")
    )
    assert end == today and start == date(today.year, today.month, 1) and note


def test_a_past_window_is_left_alone():
    past = ResolvedDateRange(start=date(2026, 1, 1), end=date(2026, 1, 31), label="x")
    assert _elapsed_range(past) == (date(2026, 1, 1), date(2026, 1, 31), None)


def test_weekly_cash_flow_buckets_monday_to_sunday_and_clips_edges():
    mon = date(2026, 8, 3)  # a Monday
    income = [_txn("1", "i", -1000, mon + timedelta(days=1))]
    spend = [_txn("1", "e1", 300, mon + timedelta(days=2)), _txn("1", "e2", 700, mon + timedelta(days=9))]
    rows = _weekly_cash_flow(income, spend, mon + timedelta(days=1), mon + timedelta(days=15))

    assert [r["net"] for r in rows] == ["700.00", "-700.00", "0.00"]
    assert rows[0]["week_start"] == (mon + timedelta(days=1)).isoformat()  # clipped to the period
    assert rows[2]["has_data"] is False


@pytest.mark.asyncio
async def test_cash_flow_over_a_month_includes_a_weekly_breakdown_and_worst_week():
    class _Flow(InMemoryBackendClient):
        async def get_financial_summary(self, context, *, start, end):
            return FinancialSummary(owner_user_id=context.user_id, period_start=start, period_end=end,
                                    total_income=_m("1000"), total_expenses=_m("1000"))

        async def get_transactions(self, context, **kwargs):
            d = date.today() - timedelta(days=20)
            return [_txn(context.user_id, "a", -1000, d), _txn(context.user_id, "b", 1500, d + timedelta(days=8))]

    today = date.today()
    plan = _plan(Intent.CASH_FLOW_ANALYSIS,
                 date_range=ResolvedDateRange(start=today - timedelta(days=30), end=today, label="x"))
    result, _ = await route_and_execute(FinancialService(_Flow()), make_user_context(user_id="1"), plan)
    assert result["weekly_breakdown"]
    assert Decimal(result["worst_week"]["net"]) == Decimal("-1500")


class _DebtClient(InMemoryBackendClient):
    async def get_liabilities(self, context):
        u = context.user_id
        return [
            Liability(owner_user_id=u, liability_id="a", name="Big card", type="credit_card", balance=_m("3000"),
                      minimum_payment=_m("100"), is_overdue=False, next_payment_due_date=date.today() + timedelta(days=3)),
            Liability(owner_user_id=u, liability_id="b", name="Small card", type="credit_card", balance=_m("500"),
                      minimum_payment=_m("25"), is_overdue=True, next_payment_due_date=date.today() - timedelta(days=10)),
            Liability(owner_user_id=u, liability_id="c", name="HELOC", type="home_equity_loan", balance=_m("40000")),
        ]

    async def get_accounts(self, context):
        return [Account(owner_user_id=context.user_id, account_id="c", name="Chk", type="checking", balance=_m("2000"))]


@pytest.mark.asyncio
async def test_extra_payment_scenario_shortens_payoff_and_states_the_interest_assumption():
    plan = _plan(Intent.DEBT_SCENARIO_QUERY, amount_in_question=100.0)
    result, _ = await route_and_execute(FinancialService(_DebtClient()), make_user_context(user_id="1"), plan)

    scenario = result["extra_monthly_payment"]
    # 3500 covered / 125 minimums = 28 months; with +100 -> 3500/225 = 16 months.
    assert scenario["months_at_minimums_only"] == 28
    assert scenario["months_with_extra"] == 16
    assert scenario["months_saved"] == 12
    assert result["debts_without_a_known_minimum_payment"] == ["HELOC"]
    assert "IGNORES interest" in result["assumptions"]


@pytest.mark.asyncio
async def test_smallest_debt_and_payment_status():
    result, _ = await route_and_execute(
        FinancialService(_DebtClient()), make_user_context(user_id="1"), _plan(Intent.DEBT_SCENARIO_QUERY)
    )
    smallest = result["smallest_debt"]
    assert smallest["name"] == "Small card"
    assert smallest["can_pay_from_cash_today"] is True
    assert smallest["liquid_cash_after_paying_it_off"] == "1500.00"
    assert smallest["debt_count_after"] == 2

    status = {s["name"]: s for s in result["payment_status"]}
    assert status["Small card"]["flagged_overdue"] is True
    assert status["Big card"]["due_within_a_week"] is True
    assert "HELOC" not in status  # no payment-status data: not reported as fine


@pytest.mark.asyncio
async def test_since_paycheck_anchors_to_the_deposit_not_the_month():
    today = date.today()
    pay_day = today - timedelta(days=6)

    class _Pay(InMemoryBackendClient):
        async def get_transactions(self, context, **kwargs):
            u = context.user_id
            rows = [
                _txn(u, "old", -2000, today - timedelta(days=36), category="Income", name="ACME PAYROLL"),
                _txn(u, "pay", -2000, pay_day, category="Income", name="ACME PAYROLL"),
                _txn(u, "small", -20, today - timedelta(days=2), category="Income", name="Refund"),
                _txn(u, "before", 90, pay_day - timedelta(days=3), name="Before pay"),
                _txn(u, "after1", 40, pay_day + timedelta(days=1), name="Shop"),
                _txn(u, "after2", 60, today, name="Cafe"),
            ]
            # Like a real backend, honour the requested window.
            start, end = kwargs.get("start"), kwargs.get("end")
            return [t for t in rows if (start is None or t.date >= start) and (end is None or t.date <= end)]

    result, _ = await route_and_execute(
        FinancialService(_Pay()), make_user_context(user_id="1"), _plan(Intent.SPENDING_SINCE_PAYCHECK)
    )
    assert result["paycheck"]["date"] == pay_day.isoformat()
    assert result["days_since_paycheck"] == 6
    assert result["total_spent_since"] == "100.00"  # only spending on/after the paycheck


@pytest.mark.asyncio
async def test_no_paycheck_found_is_reported_not_guessed():
    class _None(InMemoryBackendClient):
        async def get_transactions(self, context, **kwargs):
            return [_txn(context.user_id, "x", 10, date.today())]

    result, _ = await route_and_execute(
        FinancialService(_None()), make_user_context(user_id="1"), _plan(Intent.SPENDING_SINCE_PAYCHECK)
    )
    assert result["availability"] == "missing"


@pytest.mark.asyncio
async def test_income_by_source_reconciles_with_the_total_exactly():
    today = date.today()

    class _Inc(InMemoryBackendClient):
        async def get_transactions(self, context, **kwargs):
            u = context.user_id
            return [_txn(u, "1", -3000, today, name="ACME PAYROLL"), _txn(u, "2", -3000, today - timedelta(days=14), name="ACME PAYROLL"),
                    _txn(u, "3", -100, today, name="Dividend")]

    plan = _plan(Intent.INCOME_QUERY, date_range=ResolvedDateRange(start=today - timedelta(days=30), end=today, label="x"))
    result, _ = await route_and_execute(FinancialService(_Inc()), make_user_context(user_id="1"), plan)

    assert sum(Decimal(s["total"]) for s in result["by_source"]) == Decimal(result["total_income"])
    assert result["primary_source"]["source"].lower().startswith("acme")


@pytest.mark.asyncio
async def test_savings_opportunities_excludes_fixed_costs_and_lists_subscriptions():
    today = date.today()

    class _Save(InMemoryBackendClient):
        async def get_transactions(self, context, **kwargs):
            u = context.user_id
            d = date(today.year, today.month, 1) - timedelta(days=20)
            return [_txn(u, "r", 900, d, category="Rent And Utilities"), _txn(u, "f", 300, d, category="Food And Drink", name="Cafe"),
                    _txn(u, "b", 30, d, category="Bank Fees", name="Overdraft")]

        async def get_subscriptions(self, context):
            return [Subscription(owner_user_id=context.user_id, subscription_id="s", name="Stream", amount=_m("15"),
                                 billing_cycle="monthly")]

    result, _ = await route_and_execute(
        FinancialService(_Save()), make_user_context(user_id="1"), _plan(Intent.SAVINGS_OPPORTUNITIES)
    )
    categories = {c["category"] for c in result["top_discretionary_categories"]}
    assert categories == {"Food And Drink", "Bank Fees"}  # rent is not trimmable
    assert result["bank_fees_in_window"] == "30.00"
    assert result["recurring_monthly_outflows_total"] == "15.00"
    assert result["recurring_monthly_outflows_largest"][0]["name"] == "Stream"


@pytest.mark.asyncio
async def test_overview_survives_a_failing_section():
    class _Broken(InMemoryBackendClient):
        async def get_liabilities(self, context):
            raise RuntimeError("boom")

    result, _ = await route_and_execute(
        FinancialService(_Broken()), make_user_context(user_id="1"), _plan(Intent.FINANCIAL_OVERVIEW)
    )
    assert result["debt"]["availability"] == "missing"
    assert result["cash_flow_this_month_so_far"]["income"] is not None
    assert result["credit_score"]["availability"] == "available"


@pytest.mark.asyncio
async def test_one_extraordinary_month_does_not_set_typical_spending():
    """A single typed-in $100,000 expense made 'typical' spending $2,200 a day."""
    class _Outlier(_CashClient):
        async def get_transactions(self, context, **kwargs):
            return [
                _txn(context.user_id, "a", 900, _mid_month(1), category="Food And Drink"),
                _txn(context.user_id, "b", 1000, _mid_month(2), category="Food And Drink"),
                _txn(context.user_id, "c", 100000, _mid_month(3), category="Entertainment"),
            ]

    result, _ = await route_and_execute(
        FinancialService(_Outlier()), make_user_context(user_id="1"), _plan(Intent.LIQUIDITY_QUERY)
    )

    assert result["average_monthly_expenses"] == "1000.00", "the middle month, not the mean ($33,966.67)"
