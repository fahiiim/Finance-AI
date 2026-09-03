"""
A period-scoped question that names no period must not error.

Four of five ordinary questions once returned a raw 422 body to the user --
"what is my dining budget?", "how much do I spend at Amazon?", "what is my
savings rate?", "am I spending more than usual?" -- because the tools demand
a date range and the user had not stated one. In conversation the period is
usually obvious; the fix is to assume the current month and say so, which is
what a person would do. An assumption stated in the answer can be corrected.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ValidationFailedError
from app.orchestrator.intent_router import route_and_execute
from app.orchestrator.planner import build_execution_plan
from app.schemas.planner import (
    DEFAULT_PERIOD_PHRASE,
    NO_DATA_INTENTS,
    PERIOD_SCOPED_INTENTS,
    ExecutionPlan,
    Intent,
    RawExecutionPlan,
)
from app.services.financial_service import FinancialService
from app.services.mock_backend_client import MockBackendClient
from tests.conftest import make_user_context


class _Planner:
    def __init__(self, plan: RawExecutionPlan) -> None:
        self._plan = plan

    async def structured_generate(self, **kw):
        return self._plan

    async def generate(self, **kw) -> str:  # pragma: no cover
        return ""


@pytest.mark.parametrize("intent", sorted(PERIOD_SCOPED_INTENTS, key=lambda i: i.value))
@pytest.mark.asyncio
async def test_a_period_scoped_intent_defaults_its_window(intent):
    plan = await build_execution_plan(
        llm_provider=_Planner(
            RawExecutionPlan(intent=intent, requires_financial_data=True, requires_calculation=True)
        ),
        context=make_user_context(user_id="42"),
        current_message="what is my dining budget?",
        recent_history=[],
    )
    assert plan.date_range is not None, f"{intent.value} would reach the tools with no range"
    assert plan.date_phrase == DEFAULT_PERIOD_PHRASE
    assert plan.resolution_note, "the assumed window must be disclosed to the user"


@pytest.mark.asyncio
async def test_a_stated_period_is_never_overridden():
    plan = await build_execution_plan(
        llm_provider=_Planner(
            RawExecutionPlan(
                intent=Intent.CATEGORY_ANALYSIS,
                requires_financial_data=True,
                requires_calculation=True,
                date_phrase="last month",
            )
        ),
        context=make_user_context(user_id="42"),
        current_message="how much on groceries last month?",
        recent_history=[],
    )
    assert plan.date_phrase == "last month"


@pytest.mark.asyncio
async def test_intents_that_need_no_period_do_not_get_one():
    """A balance is a fact about now, not about a window."""
    for intent in (Intent.BALANCE_QUERY, Intent.SUBSCRIPTION_QUERY, Intent.FINANCIAL_SCORE_QUERY):
        plan = await build_execution_plan(
            llm_provider=_Planner(
                RawExecutionPlan(intent=intent, requires_financial_data=True, requires_calculation=False)
            ),
            context=make_user_context(user_id="42"),
            current_message="what is my balance?",
            recent_history=[],
        )
        assert plan.date_range is None, intent.value


@pytest.mark.asyncio
async def test_no_period_scoped_intent_can_reach_the_tools_without_a_range():
    """
    The drift guard.

    PERIOD_SCOPED_INTENTS is a hand-maintained list; the handlers that demand
    a range live elsewhere. If someone adds a range requirement to a handler
    and forgets the list, this fails rather than the user seeing a 422.
    """
    fs = FinancialService(MockBackendClient())
    ctx = make_user_context(user_id="42")
    missed = []
    for intent in Intent:
        if intent in NO_DATA_INTENTS or intent in PERIOD_SCOPED_INTENTS:
            continue
        plan = ExecutionPlan(
            intent=intent,
            requires_financial_data=True,
            requires_calculation=False,
            required_tools=[],
            date_range=None,
            amount_in_question=100.0,  # affordability needs this, unrelated to periods
        )
        try:
            await route_and_execute(fs, ctx, plan)
        except ValidationFailedError as exc:
            if "date range" in str(exc):
                missed.append(intent.value)
        except Exception:
            pass  # other failures are not this test's concern
    assert not missed, f"these demand a period but are absent from PERIOD_SCOPED_INTENTS: {missed}"
