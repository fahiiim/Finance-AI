"""
"We don't have it" must never be reported as "it is zero".

These are the same class of bug as a wrong total, but harder to notice,
because the number that reaches the user is a perfectly ordinary one:

  - a portfolio the backend cannot see was reported as $0 market value, so
    a user with real holdings was told their investments were worth nothing;
  - no debts on file was reported as a $0 balance, which reads as being
    debt-free and invites congratulation;
  - no minimum payments on file produced a debt-to-income ratio of "0.0%",
    printed next to $42,035 of credit card balances;
  - budgets this backend cannot serve came back as an empty list with no
    marker, leaving the model free to discuss adherence to nothing.

Each returns availability="missing" and a null value instead, so the
response layer has something to distinguish and the prompt rules can bite.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.orchestrator.intent_router import _savings_streak, route_and_execute
from app.schemas.financial import (
    DataAvailability,
    FinancialSummary,
    Liability,
    Money,
)
from app.schemas.planner import ExecutionPlan, Intent, ResolvedDateRange
from app.services.financial_service import FinancialService
from app.services.mock_backend_client import MockBackendClient
from app.tools.budgets import get_budgets_tool
from app.tools.investments import get_investments_tool
from app.tools.liabilities import get_liabilities_tool
from tests.conftest import make_user_context


class EmptyBackend(MockBackendClient):
    """A backend that can see nothing -- which is not a backend seeing zero."""

    async def get_investments(self, context):
        return []

    async def get_liabilities(self, context):
        return []

    async def get_budgets(self, context, *, period_start, period_end):
        return []


def _plan(intent: Intent) -> ExecutionPlan:
    return ExecutionPlan(
        intent=intent,
        requires_financial_data=True,
        requires_calculation=False,
        required_tools=[],
        date_range=ResolvedDateRange(start=date(2026, 7, 1), end=date(2026, 7, 31), label="last_month"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,value_field",
    [
        (get_investments_tool, "total_market_value"),
        (get_liabilities_tool, "total_balance"),
    ],
)
async def test_an_unavailable_feature_reports_missing_not_zero(tool, value_field):
    result = await tool(
        FinancialService(EmptyBackend()), make_user_context(user_id="42"), _plan(Intent.INVESTMENT_QUERY)
    )
    assert result["availability"] == DataAvailability.MISSING.value
    assert result[value_field] is None, "a null is unmistakable; '0' is not"
    assert "not" in result["note"].lower()


@pytest.mark.asyncio
async def test_budgets_that_cannot_be_served_are_marked_missing():
    result = await get_budgets_tool(
        FinancialService(EmptyBackend()), make_user_context(user_id="42"), _plan(Intent.BUDGET_QUERY)
    )
    assert result["availability"] == DataAvailability.MISSING.value
    assert "never invent a budget" in result["note"].lower()


@pytest.mark.asyncio
async def test_available_data_is_still_marked_available():
    """The flag has to discriminate, or the response layer learns to ignore it."""
    result = await get_investments_tool(
        FinancialService(MockBackendClient()), make_user_context(user_id="42"), _plan(Intent.INVESTMENT_QUERY)
    )
    assert result["availability"] == DataAvailability.AVAILABLE.value
    assert Decimal(result["total_market_value"]) > 0


class NoMinimumPaymentBackend(MockBackendClient):
    """Balances are known; the monthly minimums are not. The real SQLite case."""

    async def get_liabilities(self, context):
        return [
            Liability(
                owner_user_id=context.user_id,
                liability_id="card-1",
                name="Business Advantage Card",
                type="credit_card",
                balance=Money(amount=Decimal("41968.63"), currency="USD"),
                minimum_payment=None,
            )
        ]


@pytest.mark.asyncio
async def test_debt_to_income_is_unavailable_not_zero_without_minimum_payments():
    """
    Summing an empty list of minimum payments gives 0, and 0/income is 0.0% --
    a healthy-looking ratio manufactured from no data, printed beside $41,968
    of debt.
    """
    result, _ = await route_and_execute(
        FinancialService(NoMinimumPaymentBackend()),
        make_user_context(user_id="42"),
        _plan(Intent.OVERALL_FINANCIAL_HEALTH),
    )
    assert result["debt_to_income_percent"] is None
    assert "cannot be computed" in result["debt_to_income_note"]
    assert Decimal(result["total_liabilities"]) > 0, "the balance itself IS known"


class NoIncomeBackend(MockBackendClient):
    async def get_financial_summary(self, context, *, start, end):
        return FinancialSummary(
            owner_user_id=context.user_id,
            period_start=start,
            period_end=end,
            total_income=Money(amount=Decimal("0"), currency="USD"),
            total_expenses=Money(amount=Decimal("500"), currency="USD"),
        )


@pytest.mark.asyncio
async def test_a_savings_rate_with_no_income_is_undefined_not_zero_percent():
    result, _ = await route_and_execute(
        FinancialService(NoIncomeBackend()),
        make_user_context(user_id="42"),
        _plan(Intent.SAVINGS_ANALYSIS),
    )
    assert result["savings_rate_percent"] is None
    assert "undefined, not 0%" in result["savings_rate_note"]
    assert result["net_saved"] == "-500.00", "the amount is still computable"


# ------------------------------------------------------------------ streaks


def _month(label: str, net: str, *, has_data: bool = True) -> dict:
    return {"month": label, "income": "0", "expenses": "0", "net": net, "has_data": has_data}


def test_a_streak_counts_back_from_the_most_recent_complete_month():
    streak = _savings_streak(
        [_month("2026-04", "-50"), _month("2026-05", "100"), _month("2026-06", "200"), _month("2026-07", "10")]
    )
    assert streak["months_in_surplus_in_a_row"] == 3
    assert "2026-04" in streak["streak_ended_at"]


def test_a_month_with_no_data_breaks_a_streak_rather_than_extending_it():
    """
    Counting an empty month as a saving month builds the streak out of the
    months we could not see -- the most flattering reading of the least
    reliable data, and the user has no way to tell.
    """
    streak = _savings_streak(
        [_month("2026-05", "100"), _month("2026-06", "0", has_data=False), _month("2026-07", "300")]
    )
    assert streak["months_in_surplus_in_a_row"] == 1
    assert "no transactions on record" in streak["streak_ended_at"]


def test_an_overspent_most_recent_month_means_no_streak():
    streak = _savings_streak([_month("2026-06", "500"), _month("2026-07", "-20")])
    assert streak["months_in_surplus_in_a_row"] == 0
    assert "spent more than came in" in streak["streak_ended_at"]


@pytest.mark.asyncio
async def test_the_savings_answer_carries_both_an_amount_and_a_rate():
    """Reporting only a rate forced the model to derive the amount in prose."""
    result, tools = await route_and_execute(
        FinancialService(MockBackendClient()),
        make_user_context(user_id="42"),
        _plan(Intent.SAVINGS_ANALYSIS),
    )
    assert result["net_saved"] is not None
    assert "streak" in result
    assert "calculate_cash_flow" in tools
