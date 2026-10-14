"""
Savings goal tool tests.

Regression guard: the service used to ask the user for a goal amount and
deadline even when one was stored (`save_goals` in the real data) -- this
checks the tool reads a stored goal and derives progress from it instead.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.financial import DataAvailability, Money, SavingsGoal, Transaction
from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient


def _plan() -> ExecutionPlan:
    return ExecutionPlan(intent=Intent.SAVINGS_GOAL_QUERY, requires_financial_data=True, requires_calculation=True)


class _GoalClient(InMemoryBackendClient):
    """A fixed goal, start date 60 days ago, with known income/expenses since then."""

    async def get_savings_goal(self, context):
        return SavingsGoal(
            owner_user_id=context.user_id,
            goal_id="g1",
            target_amount=Money(amount=Decimal("10000"), currency="USD"),
            monthly_target=Money(amount=Decimal("500"), currency="USD"),
            start_date=date.today() - timedelta(days=60),
            end_date=date.today() + timedelta(days=300),
        )

    async def get_transactions(self, context, *, start=None, end=None, category=None, merchant=None,
                                min_amount=None, max_amount=None):
        # FinancialService.get_income/get_expenses both derive from
        # get_transactions (see app/services/financial_service.py) rather
        # than delegating to the backend client's own get_income/get_expenses
        # -- so the fixed transactions belong here, not on those methods.
        return [
            Transaction(owner_user_id=context.user_id, transaction_id="i1", account_id="a", amount=Decimal("-3000"),
                        currency="USD", date=start + timedelta(days=5)),
            Transaction(owner_user_id=context.user_id, transaction_id="e1", account_id="a", amount=Decimal("1200"),
                        currency="USD", date=start + timedelta(days=10)),
        ]


@pytest.mark.asyncio
async def test_a_stored_goal_is_used_instead_of_asking_for_one():
    fs = FinancialService(_GoalClient())
    result, tools = await route_and_execute(fs, make_user_context(user_id="1"), _plan())

    assert result["availability"] == DataAvailability.AVAILABLE.value
    assert result["target_amount"] == "10000"
    assert "get_savings_goal" in tools
    # progress = income (3000) - expenses (1200) = 1800
    assert result["progress_saved"] == "1800.00"
    assert result["remaining_to_target"] == "8200.00"
    assert result["days_remaining"] == 300


@pytest.mark.asyncio
async def test_no_stored_goal_says_so_rather_than_asking():
    class _NoGoal(InMemoryBackendClient):
        async def get_savings_goal(self, context):
            return None

    fs = FinancialService(_NoGoal())
    result, _ = await route_and_execute(fs, make_user_context(user_id="2"), _plan())
    assert result["availability"] == DataAvailability.MISSING.value


@pytest.mark.asyncio
async def test_a_goal_already_reached_is_flagged():
    class _ReachedGoal(_GoalClient):
        async def get_savings_goal(self, context):
            goal = await super().get_savings_goal(context)
            return goal.model_copy(update={"target_amount": Money(amount=Decimal("1000"), currency="USD")})

    fs = FinancialService(_ReachedGoal())
    result, _ = await route_and_execute(fs, make_user_context(user_id="3"), _plan())
    assert result.get("goal_already_reached") is True
