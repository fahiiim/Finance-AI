"""
A category question must be answered with that category's number.

The bug these guard against: the transaction list was fetched for the period
but never narrowed to the requested category, so the "how much on groceries?"
answer carried the whole period's total. The arithmetic was right and the
answer was wrong -- the most dangerous shape a financial bug can take, because
nothing looks broken.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from app.services.mock_backend_client import MockBackendClient
from app.tools.expenses import get_expenses_tool
from app.utils.dates import ResolvedDateRange
from tests.conftest import make_user_context


def _service() -> FinancialService:
    return FinancialService(MockBackendClient())


def _range() -> ResolvedDateRange:
    return ResolvedDateRange(start=date(2026, 7, 1), end=date(2026, 7, 31), label="last_month")


def _plan(intent: Intent, category: str | None) -> ExecutionPlan:
    return ExecutionPlan(
        intent=intent,
        requires_financial_data=True,
        requires_calculation=True,
        required_tools=[],
        date_range=_range(),
        category_filter=category,
    )


@pytest.mark.asyncio
async def test_a_category_total_is_smaller_than_the_overall_total():
    ctx = make_user_context(user_id="99")
    fs = _service()

    overall = await get_expenses_tool(fs, ctx, _plan(Intent.CATEGORY_ANALYSIS, None))
    assert overall["by_category"], "the fixture must contain several categories"
    category = next(iter(overall["by_category"]))

    scoped = await get_expenses_tool(fs, ctx, _plan(Intent.CATEGORY_ANALYSIS, category))

    assert Decimal(scoped["total_expenses"]) == Decimal(overall["by_category"][category])
    assert Decimal(scoped["total_expenses"]) < Decimal(overall["total_expenses"])
    assert scoped["transaction_count"] < overall["transaction_count"]
    assert scoped["category"] == category


@pytest.mark.asyncio
async def test_a_scoped_result_says_it_is_scoped():
    """An unlabelled total invites the model to present it as the overall one."""
    result = await get_expenses_tool(
        _service(), make_user_context(user_id="99"), _plan(Intent.CATEGORY_ANALYSIS, "Groceries")
    )
    assert "Groceries" in result["scope"]
    assert set(result["by_category"]) == {"Groceries"}


@pytest.mark.asyncio
async def test_a_real_category_with_no_spending_reports_zero_not_missing_data():
    """
    'Medical' is a category this service knows. A month with no doctor's visit
    is a genuine zero, and must not be reported as data we could not find --
    the user would go looking for a problem that is not there.
    """
    result = await get_expenses_tool(
        _service(), make_user_context(user_id="99"), _plan(Intent.CATEGORY_ANALYSIS, "Medical")
    )
    assert Decimal(result["total_expenses"]) == Decimal("0")
    assert result["transaction_count"] == 0
    assert "genuine zero" in " ".join(result["filter_notes"])


@pytest.mark.asyncio
async def test_a_category_that_does_not_exist_is_not_reported_as_zero():
    """
    The opposite failure, and the more dangerous one. A phrase that names no
    category at all used to come back as $0.00, which reads exactly like "you
    spent nothing there" -- so the user believes a category they never had is
    simply unused, and has no way to tell the difference.
    """
    result = await get_expenses_tool(
        _service(),
        make_user_context(user_id="99"),
        _plan(Intent.CATEGORY_ANALYSIS, "Category That Does Not Exist"),
    )
    assert result["transaction_count"] == 0
    notes = " ".join(result["filter_notes"])
    assert "NOT" in notes and "not found" in notes
    assert "genuine zero" not in notes
    assert result["categories_available_this_period"], "the answer must be able to name real ones"


@pytest.mark.asyncio
async def test_a_category_comparison_compares_only_that_category():
    ctx = make_user_context(user_id="99")
    fs = _service()

    overall, _ = await route_and_execute(fs, ctx, _plan(Intent.PERIOD_COMPARISON, None))
    category = next(iter(overall["by_category"]))
    scoped, _ = await route_and_execute(fs, ctx, _plan(Intent.PERIOD_COMPARISON, category))

    assert set(scoped["by_category"]) <= {category}
    assert Decimal(scoped["current_total"]) < Decimal(overall["current_total"])
    assert Decimal(scoped["current_total"]) == Decimal(overall["by_category"][category]["current"])
    assert Decimal(scoped["previous_total"]) == Decimal(overall["by_category"][category]["previous"])
    assert category in scoped["scope"]


@pytest.mark.asyncio
async def test_mock_merchants_and_categories_agree():
    """
    A demo that shows "Netflix (Groceries)" undermines every number beside it,
    and makes category totals impossible to check against the transaction list.
    """
    from app.services.mock_backend_client import _MERCHANT_CATEGORIES

    client = MockBackendClient()
    txns = await client._all_transactions(make_user_context(user_id="99"))
    assert txns
    for t in txns:
        if t.category == "Income":
            assert t.merchant_name == "Employer Payroll"
            continue
        assert t.merchant_name in _MERCHANT_CATEGORIES, t.merchant_name
        assert t.category == _MERCHANT_CATEGORIES[t.merchant_name], f"{t.merchant_name} -> {t.category}"


@pytest.mark.asyncio
async def test_the_router_honours_a_named_baseline():
    """A baseline the user named must not be replaced by the derived one."""
    from app.utils.dates import resolve_date_phrase

    ctx = make_user_context(user_id="42")
    fs = _service()
    current = ResolvedDateRange(start=date(2026, 8, 1), end=date(2026, 8, 23), label="this_month")
    named = resolve_date_phrase("last month", "America/New_York")

    plan = ExecutionPlan(
        intent=Intent.PERIOD_COMPARISON,
        requires_financial_data=True,
        requires_calculation=True,
        required_tools=[],
        date_range=current,
        baseline_range=named,
    )
    result, _ = await route_and_execute(fs, ctx, plan)
    assert result["previous_period"]["start"] == named.start.isoformat()
    assert result["previous_period"]["end"] == named.end.isoformat()
    assert result["current_period"]["start"] == current.start.isoformat()
