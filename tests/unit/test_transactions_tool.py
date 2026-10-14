"""
TRANSACTION_QUERY regression: must not return income rows.

Confirmed live against real data before this fix: a manually-entered
+$500,000 INCOME-category row surfaced in "show me every transaction over
$200 this month" because get_transactions_tool called
financial_service.get_transactions() (both signs) instead of get_expenses().
Every sibling tool (SPENDING_ANALYSIS, CATEGORY_ANALYSIS, MERCHANT_ANALYSIS)
already filtered to expenses; this brings TRANSACTION_QUERY in line.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.financial import Transaction
from app.schemas.planner import ExecutionPlan, Intent, ResolvedDateRange
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient


class _MixedSignClient(InMemoryBackendClient):
    async def get_transactions(self, context, *, start=None, end=None, category=None, merchant=None,
                                min_amount=None, max_amount=None):
        today = date.today()
        return [
            # An expense well over the threshold used below.
            Transaction(owner_user_id=context.user_id, transaction_id="e1", account_id="a",
                        amount=Decimal("250"), currency="USD", date=today - timedelta(days=1)),
            # A manually-entered income row with a large positive amount --
            # same shape as the real $500,000 junk row that exposed this bug.
            Transaction(owner_user_id=context.user_id, transaction_id="i1", account_id="a",
                        amount=Decimal("-500000"), currency="USD", date=today - timedelta(days=1)),
        ]


def _plan(**kw) -> ExecutionPlan:
    today = date.today()
    base = dict(
        intent=Intent.TRANSACTION_QUERY,
        requires_financial_data=True,
        requires_calculation=False,
        date_range=ResolvedDateRange(start=today - timedelta(days=7), end=today, label="past_week"),
    )
    base.update(kw)
    return ExecutionPlan(**base)


@pytest.mark.asyncio
async def test_income_rows_are_excluded_from_a_transaction_listing():
    fs = FinancialService(_MixedSignClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(min_amount=200.0))

    ids = {t["amount"] for t in result["sample_transactions"]}
    assert "-500000" not in ids
    assert result["transaction_count"] == 1
    assert result["total_outflow"] == "250.00"
