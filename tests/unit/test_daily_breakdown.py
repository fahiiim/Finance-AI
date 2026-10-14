"""
Per-day spending data.

"How much did I spend each day this week?" used to be declined: the expenses
tool returned a period total and a per-category split, and nothing per day.
The assistant said so honestly rather than inventing figures, but the question
had no answer. It does now, with two constraints that matter:

  - days with no spending appear as zero, because a missing row and a zero row
    read very differently, and "nothing on Tuesday" is itself an answer;
  - long windows get no series at all, because a year of rows would displace
    everything else in the prompt for a question nobody asks day-by-day.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from app.tools.expenses import MAX_DAILY_BREAKDOWN_DAYS, get_expenses_tool
from app.utils.dates import ResolvedDateRange
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient


def _plan(start: date, end: date, category: str | None = None) -> ExecutionPlan:
    return ExecutionPlan(
        intent=Intent.SPENDING_ANALYSIS,
        requires_financial_data=True,
        requires_calculation=True,
        required_tools=[],
        date_range=ResolvedDateRange(start=start, end=end, label="window"),
        category_filter=category,
    )


async def _run(plan: ExecutionPlan) -> dict:
    return await get_expenses_tool(FinancialService(InMemoryBackendClient()), make_user_context(user_id="42"), plan)


@pytest.mark.asyncio
async def test_every_day_in_the_window_has_a_row():
    start = date(2026, 8, 17)
    end = date(2026, 8, 23)
    result = await _run(_plan(start, end))

    assert len(result["by_date"]) == 7
    expected = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    assert list(result["by_date"]) == expected, "rows must be present, in order, with no gaps"


@pytest.mark.asyncio
async def test_the_daily_rows_sum_to_the_period_total():
    result = await _run(_plan(date(2026, 8, 1), date(2026, 8, 23)))
    daily_sum = sum(Decimal(v) for v in result["by_date"].values())
    assert daily_sum == Decimal(result["total_expenses"])


@pytest.mark.asyncio
async def test_a_quiet_day_is_reported_as_zero_not_omitted():
    result = await _run(_plan(date(2026, 8, 1), date(2026, 8, 23)))
    zeros = [d for d, v in result["by_date"].items() if Decimal(v) == 0]
    assert zeros, "the fixture should contain at least one day with no spending"
    for day in zeros:
        assert result["by_date"][day] == "0.00"


@pytest.mark.asyncio
async def test_a_long_window_gets_no_series_and_says_why():
    start = date(2026, 1, 1)
    end = start + timedelta(days=MAX_DAILY_BREAKDOWN_DAYS + 10)
    result = await _run(_plan(start, end))

    assert result["by_date"] == {}
    assert "by_date_note" in result
    assert str(MAX_DAILY_BREAKDOWN_DAYS) in result["by_date_note"]
    assert Decimal(result["total_expenses"]) >= 0, "the total is still answered"


@pytest.mark.asyncio
async def test_the_boundary_window_still_gets_a_series():
    start = date(2026, 6, 1)
    end = start + timedelta(days=MAX_DAILY_BREAKDOWN_DAYS - 1)
    result = await _run(_plan(start, end))
    assert len(result["by_date"]) == MAX_DAILY_BREAKDOWN_DAYS
    assert "by_date_note" not in result


@pytest.mark.asyncio
async def test_a_single_day_window_reports_that_day_alone():
    """"How much did I spend on August 20?" is a one-row window."""
    day = date(2026, 8, 20)
    result = await _run(_plan(day, day))
    assert list(result["by_date"]) == [day.isoformat()]
    assert Decimal(result["by_date"][day.isoformat()]) == Decimal(result["total_expenses"])


@pytest.mark.asyncio
async def test_a_category_scoped_breakdown_counts_only_that_category():
    start, end = date(2026, 8, 1), date(2026, 8, 23)
    everything = await _run(_plan(start, end))
    category = next(iter(everything["by_category"]))
    scoped = await _run(_plan(start, end, category))

    assert len(scoped["by_date"]) == len(everything["by_date"]), "the window is unchanged"
    daily_sum = sum(Decimal(v) for v in scoped["by_date"].values())
    assert daily_sum == Decimal(scoped["total_expenses"])
    assert daily_sum < sum(Decimal(v) for v in everything["by_date"].values())


@pytest.mark.asyncio
async def test_a_weekday_filter_narrows_the_total_and_the_series_together():
    """
    "Fridays in June": the total covers Fridays, so the daily series must too.
    A month of rows underneath a Friday-only total contradicts it -- zeros for
    days that were never counted.
    """
    plan = _plan(date(2026, 6, 1), date(2026, 6, 30))
    plan = plan.model_copy(update={"weekday_filter": 4})
    result = await _run(plan)

    assert result["weekday"] == "Friday"
    days = [date.fromisoformat(d) for d in result["by_date"]]
    assert days, "June has Fridays in it"
    assert all(d.weekday() == 4 for d in days), "a non-Friday reached the series"
    assert sum(Decimal(v) for v in result["by_date"].values()) == Decimal(result["total_expenses"])


@pytest.mark.asyncio
async def test_without_a_weekday_filter_every_day_is_still_listed():
    result = await _run(_plan(date(2026, 6, 1), date(2026, 6, 30)))
    assert result["weekday"] is None
    assert len(result["by_date"]) == 30
