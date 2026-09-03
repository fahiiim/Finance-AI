"""
How this service behaves when the production backend is slow, failing, or partial.

The demo runs on local SQLite, where every fetch is instant and nothing ever
times out. Production runs on `BackendAPIClient` over HTTP to Laravel, where
the opposite is true — and the failure modes that only exist on that path are
exactly the ones no demo will ever surface:

  - A backend timeout was wrapped into ToolExecutionError and reached the user
    mid-conversation as a red "500 tool_execution_error" box. A slow backend
    is the single most likely production incident, and the request itself had
    not failed — only the data read had.
  - The savings streak pulled a year of raw transactions in one call: 584 KB
    and 2,344 rows for one real user, on EVERY savings question, against a
    10-second client timeout.
  - A partial failure (one month of history unavailable) must not take down
    the savings rate the user actually asked for.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import (
    BackendServiceError,
    BackendTimeoutError,
    CrossUserDataError,
    FinancialDataUnavailableError,
    ToolExecutionError,
)
from app.orchestrator.intent_router import route_and_execute
from app.schemas.financial import FinancialSummary, Money
from app.schemas.planner import ExecutionPlan, Intent, ResolvedDateRange
from app.services.financial_service import FinancialService
from app.services.mock_backend_client import MockBackendClient
from tests.conftest import make_user_context

AUGUST = ResolvedDateRange(start=date(2026, 8, 1), end=date(2026, 8, 28), label="this_month")


def _plan(intent: Intent, **kw) -> ExecutionPlan:
    return ExecutionPlan(
        intent=intent,
        requires_financial_data=True,
        requires_calculation=True,
        required_tools=[],
        date_range=AUGUST,
        **kw,
    )


class _Failing(MockBackendClient):
    """A backend that fails the way a real one does: on the read, not the connect."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def get_expenses(self, context, *, start, end):
        raise self._error

    async def get_transactions(self, context, **kwargs):
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        BackendTimeoutError("timed out"),
        BackendServiceError("bad gateway"),
        FinancialDataUnavailableError("unavailable"),
    ],
)
async def test_a_failing_backend_stays_a_conversational_outcome(error):
    """
    These must reach the orchestrator AS THEMSELVES so it can answer in words.
    Wrapped in ToolExecutionError they become an HTTP 500 in the chat window.
    """
    with pytest.raises(type(error)):
        await route_and_execute(
            FinancialService(_Failing(error)), make_user_context(user_id="1"), _plan(Intent.SPENDING_ANALYSIS)
        )


@pytest.mark.asyncio
async def test_an_unexpected_error_is_still_wrapped():
    """The catch-all must keep catching; only the known-conversational ones pass through."""
    with pytest.raises(ToolExecutionError):
        await route_and_execute(
            FinancialService(_Failing(KeyError("something structural"))),
            make_user_context(user_id="1"),
            _plan(Intent.SPENDING_ANALYSIS),
        )


@pytest.mark.asyncio
async def test_a_cross_user_breach_is_never_degraded_into_an_answer():
    """
    Another user's records reaching this service is a containment breach. It
    must abort the request, not fall through to a partial reply.
    """
    with pytest.raises(CrossUserDataError):
        await route_and_execute(
            FinancialService(_Failing(CrossUserDataError("wrong owner"))),
            make_user_context(user_id="1"),
            _plan(Intent.SPENDING_ANALYSIS),
        )


class _CountingBackend(MockBackendClient):
    """Records every call so the request COST can be asserted, not just the result."""

    def __init__(self) -> None:
        super().__init__()
        self.transaction_calls: list[tuple[date, date]] = []
        self.summary_calls: list[tuple[date, date]] = []

    async def get_transactions(self, context, *, start=None, end=None, **kwargs):
        self.transaction_calls.append((start, end))
        return await super().get_transactions(context, start=start, end=end, **kwargs)

    async def get_financial_summary(self, context, *, start, end):
        self.summary_calls.append((start, end))
        return await super().get_financial_summary(context, start=start, end=end)


@pytest.mark.asyncio
async def test_the_savings_streak_reads_aggregates_not_a_year_of_rows():
    """
    Twelve small summary calls, not one enormous transaction fetch. Against
    the HTTP backend the difference measured 584 KB against 2.3 KB.

    The assertion is on the WINDOW SIZE the router asks for, not on the call
    count, because what each backend does internally differs: the mock and
    SQLite clients derive a summary by reading transactions, while
    BackendAPIClient -- the production one -- serves it from a real
    /internal/financial-summary endpoint. The router's contract is the same
    either way: never ask for a window wider than a month.
    """
    backend = _CountingBackend()
    result, _ = await route_and_execute(
        FinancialService(backend), make_user_context(user_id="42"), _plan(Intent.SAVINGS_ANALYSIS)
    )
    assert result["streak"]["months_in_surplus_in_a_row"] >= 0

    # One summary for the period asked about, plus one per month of history.
    assert len(backend.summary_calls) == 13

    # Every history call covers exactly one calendar month.
    for start, end in backend.summary_calls[1:]:
        assert start.day == 1
        assert start.month == end.month and start.year == end.year

    # And nothing anywhere asked for a year of rows in one go. A year-long
    # window is the shape of the bug: 2,344 rows and 584 KB in one response.
    for start, end in backend.transaction_calls:
        assert (end - start).days <= 40, f"a {(end - start).days}-day transaction fetch"


class _PartiallyFailingBackend(MockBackendClient):
    """One month of history is unavailable; everything else works."""

    async def get_financial_summary(self, context, *, start, end):
        if (start.year, start.month) == (2026, 3):
            raise BackendTimeoutError("that month timed out")
        return await super().get_financial_summary(context, start=start, end=end)


@pytest.mark.asyncio
async def test_one_unavailable_month_does_not_lose_the_savings_answer():
    """
    The streak is an extra. The savings rate is what was asked for, and it
    must survive a partial history failure.
    """
    result, _ = await route_and_execute(
        FinancialService(_PartiallyFailingBackend()),
        make_user_context(user_id="42"),
        _plan(Intent.SAVINGS_ANALYSIS),
    )
    assert result["net_saved"] is not None, "the actual question still got answered"

    history = {m["month"]: m for m in result["streak"]["monthly_history"]}
    failed = history["2026-03"]
    assert failed["has_data"] is False
    assert failed["income"] is None, "a month we could not read must not report a figure"


class _SlowHistoryBackend(MockBackendClient):
    """The merchant history lookup fails; the period lookup works."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def get_transactions(self, context, *, start=None, end=None, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise BackendTimeoutError("the wide lookback timed out")
        return []


@pytest.mark.asyncio
async def test_the_merchant_history_lookup_is_best_effort():
    """
    Looking up whether a merchant was EVER used is enrichment on an answer we
    already have. A timeout there must cost the extra sentence, not the reply.
    """
    backend = _SlowHistoryBackend()
    result, _ = await route_and_execute(
        FinancialService(backend),
        make_user_context(user_id="42"),
        _plan(Intent.MERCHANT_ANALYSIS, merchant_filter="Starbucks"),
    )
    assert backend.calls == 2, "it did attempt the history lookup"
    assert result["transaction_count"] == 0
    assert "searched_back_to" not in result, "no claim about history we could not read"


@pytest.mark.asyncio
async def test_a_merchant_question_without_a_name_does_not_raise():
    """
    This used to raise ValidationFailedError, which surfaced as a 422 in the
    chat window. Showing where the money went is a better answer than an error.
    """
    result, _ = await route_and_execute(
        FinancialService(MockBackendClient()), make_user_context(user_id="42"), _plan(Intent.MERCHANT_ANALYSIS)
    )
    assert result["no_merchant_named"] is True
    assert result["top_merchants"], "it names real merchants to choose from"


class _ZeroIncomeBackend(MockBackendClient):
    async def get_financial_summary(self, context, *, start, end):
        return FinancialSummary(
            owner_user_id=context.user_id,
            period_start=start,
            period_end=end,
            total_income=Money(amount=Decimal("0"), currency="USD"),
            total_expenses=Money(amount=Decimal("0"), currency="USD"),
        )


@pytest.mark.asyncio
async def test_an_empty_month_never_counts_towards_a_streak():
    """
    Zero in and zero out is indistinguishable from a month the backend has
    nothing for. Counting it as a surplus builds the streak out of the months
    we could not see.
    """
    result, _ = await route_and_execute(
        FinancialService(_ZeroIncomeBackend()), make_user_context(user_id="42"), _plan(Intent.SAVINGS_ANALYSIS)
    )
    assert result["streak"]["months_in_surplus_in_a_row"] == 0
    assert all(m["has_data"] is False for m in result["streak"]["monthly_history"])
