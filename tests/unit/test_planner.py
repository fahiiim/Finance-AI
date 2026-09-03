from __future__ import annotations

import pytest

from app.orchestrator.planner import build_execution_plan
from app.schemas.planner import Intent, RawExecutionPlan
from tests.conftest import FakeLLMProvider, make_user_context


@pytest.mark.asyncio
async def test_planner_resolves_date_phrase_deterministically():
    raw = RawExecutionPlan(
        intent=Intent.SPENDING_ANALYSIS,
        requires_financial_data=True,
        requires_calculation=True,
        required_tools=["get_expenses"],
        date_phrase="this month",
    )
    llm = FakeLLMProvider(plans=[raw])
    context = make_user_context()

    plan = await build_execution_plan(
        llm_provider=llm, context=context, current_message="how much did I spend this month?", recent_history=[]
    )

    assert plan.intent == Intent.SPENDING_ANALYSIS
    assert plan.date_range is not None
    assert plan.needs_clarification is False


@pytest.mark.asyncio
async def test_planner_asks_for_clarification_on_unresolvable_phrase():
    raw = RawExecutionPlan(
        intent=Intent.SPENDING_ANALYSIS,
        requires_financial_data=True,
        requires_calculation=True,
        date_phrase="sometime last spring break",
    )
    llm = FakeLLMProvider(plans=[raw])
    context = make_user_context()

    plan = await build_execution_plan(
        llm_provider=llm, context=context, current_message="how much did I spend then?", recent_history=[]
    )

    assert plan.needs_clarification is True
    assert plan.clarification_question is not None


@pytest.mark.asyncio
async def test_planner_forces_no_data_for_general_conversation():
    raw = RawExecutionPlan(
        intent=Intent.GENERAL_CONVERSATION,
        requires_financial_data=True,  # deliberately wrong, planner should still zero this out
        requires_calculation=True,
        required_tools=["get_transactions"],
    )
    llm = FakeLLMProvider(plans=[raw])
    context = make_user_context()

    plan = await build_execution_plan(
        llm_provider=llm, context=context, current_message="hey, how's it going?", recent_history=[]
    )

    assert plan.requires_financial_data is False
    assert plan.requires_calculation is False
    assert plan.required_tools == []


@pytest.mark.asyncio
async def test_planner_with_no_date_phrase_has_no_date_range():
    raw = RawExecutionPlan(
        intent=Intent.BALANCE_QUERY, requires_financial_data=True, requires_calculation=False, required_tools=["get_accounts"]
    )
    llm = FakeLLMProvider(plans=[raw])
    context = make_user_context()

    plan = await build_execution_plan(
        llm_provider=llm, context=context, current_message="how much money do I have?", recent_history=[]
    )
    assert plan.date_range is None
