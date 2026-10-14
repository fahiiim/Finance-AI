"""
What one question COSTS the backend.

On a local source every fetch is free, so none of this shows up in a local run. Against the
production HTTP backend each call is a round trip and each response is real
bytes — measured on one live user, a year of transactions is 2,344 rows and
584 KB, against a client timeout.

These assert the shape of the request, not the shape of the answer, because
the answer was already correct in every case below. That is exactly what makes
this class of regression easy to reintroduce: nothing about the output looks
wrong when a handler quietly starts fetching ten times the data.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.planner import ExecutionPlan, Intent, ResolvedDateRange
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient

AUGUST = ResolvedDateRange(start=date(2026, 8, 1), end=date(2026, 8, 28), label="this_month")


def _plan(intent: Intent, **kw) -> ExecutionPlan:
    return ExecutionPlan(
        intent=intent, requires_financial_data=True, requires_calculation=True,
        required_tools=[], date_range=AUGUST, **kw,
    )


class _Recording(InMemoryBackendClient):
    """Counts backend calls, the way the HTTP client would count round trips."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    async def get_transactions(self, context, *, start=None, end=None, merchant=None, **kwargs):
        self.calls.append(("get_transactions", start, end, merchant))
        return await super().get_transactions(context, start=start, end=end, merchant=merchant, **kwargs)

    async def get_accounts(self, context):
        self.calls.append(("get_accounts", None, None, None))
        return await super().get_accounts(context)


# ------------------------------------------------------- per-request caching


@pytest.mark.asyncio
async def test_repeating_a_read_within_one_request_costs_one_call():
    backend = _Recording()
    service = FinancialService(backend)
    context = make_user_context(user_id="42")

    await service.get_accounts(context)
    await service.get_accounts(context)
    await service.get_accounts(context)

    assert [c[0] for c in backend.calls] == ["get_accounts"], "the repeats were served from cache"


@pytest.mark.asyncio
async def test_income_and_expenses_reuse_the_transaction_fetch():
    """
    Both are a filter over the same rows under one sign convention, so a turn
    that needs transactions AND expenses should cost one round trip, not two.
    """
    backend = _Recording()
    service = FinancialService(backend)
    context = make_user_context(user_id="42")

    await service.get_transactions(context, start=AUGUST.start, end=AUGUST.end)
    got_expenses = await service.get_expenses(context, start=AUGUST.start, end=AUGUST.end)
    got_income = await service.get_income(context, start=AUGUST.start, end=AUGUST.end)

    assert len(backend.calls) == 1, [c[0] for c in backend.calls]
    assert got_expenses and got_income, "and both still return real data"
    assert all(t.amount > 0 for t in got_expenses)
    assert all(t.amount < 0 for t in got_income)


@pytest.mark.asyncio
async def test_the_cache_is_scoped_to_one_user():
    """
    A cache that ignored user_id would be a cross-user data leak wearing the
    costume of an optimisation.
    """
    backend = _Recording()
    service = FinancialService(backend)

    alice = await service.get_transactions(make_user_context(user_id="1"), start=AUGUST.start, end=AUGUST.end)
    bob = await service.get_transactions(make_user_context(user_id="2"), start=AUGUST.start, end=AUGUST.end)

    assert len(backend.calls) == 2, "one user's fetch must never satisfy another's"
    assert {t.owner_user_id for t in alice} == {"1"}
    assert {t.owner_user_id for t in bob} == {"2"}


@pytest.mark.asyncio
async def test_different_windows_are_not_confused_for_each_other():
    backend = _Recording()
    service = FinancialService(backend)
    context = make_user_context(user_id="42")

    await service.get_transactions(context, start=date(2026, 8, 1), end=date(2026, 8, 28))
    await service.get_transactions(context, start=date(2026, 7, 1), end=date(2026, 7, 31))

    assert len(backend.calls) == 2


# ------------------------------------------------- merchant history lookback


@pytest.mark.asyncio
async def test_the_merchant_history_lookup_filters_at_the_backend():
    """
    Fetching six months of everything to find one merchant cost 276 KB for a
    question already answered. The name goes to the backend instead.
    """
    backend = _Recording()
    await route_and_execute(
        FinancialService(backend),
        make_user_context(user_id="42"),
        _plan(Intent.MERCHANT_ANALYSIS, merchant_filter="Nowhere Ltd"),
    )

    wide = [c for c in backend.calls if c[1] and c[2] and (c[2] - c[1]).days > 40]
    assert wide, "the history lookback did run"
    assert all(c[3] == "Nowhere Ltd" for c in wide), "and it was filtered server-side, not fetched whole"


@pytest.mark.asyncio
async def test_no_negative_claim_is_made_from_the_weaker_backend_matcher():
    """
    The backend's filter is a plain substring match and can MISS a merchant
    spelled differently. So finding rows proves existence, and finding none
    proves nothing -- the result must not claim the search was exhaustive.
    """
    result, _ = await route_and_execute(
        FinancialService(InMemoryBackendClient()),
        make_user_context(user_id="42"),
        _plan(Intent.MERCHANT_ANALYSIS, merchant_filter="Nowhere Ltd"),
    )
    assert result["transaction_count"] == 0
    assert "last_seen_outside_period" not in result
    assert "searched_back_to" not in result, "claiming a searched range implies the search was conclusive"
