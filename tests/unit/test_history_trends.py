"""
Trend-enrichment tests for financial score, Allo Score, and liabilities.

Regression guard: "has my credit score gone up or down?", "is my Allo
score improving?", and "is my debt getting better or worse?" all used to
answer "I don't have an earlier figure to compare it to" even when history
exists, because the tools only ever fetched the single latest record.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.financial import AlloScore, FinancialScore, Liability, LiabilitySnapshot, Money
from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient


def _plan(intent: Intent) -> ExecutionPlan:
    return ExecutionPlan(intent=intent, requires_financial_data=True, requires_calculation=False)


class _ScoredClient(InMemoryBackendClient):
    async def get_financial_score(self, context):
        return FinancialScore(
            owner_user_id=context.user_id, score=742, score_range_min=300, score_range_max=850,
            factors=[], as_of_date=date(2026, 7, 1),
        )

    async def get_financial_score_history(self, context):
        return [
            await self.get_financial_score(context),
            FinancialScore(
                owner_user_id=context.user_id, score=700, score_range_min=300, score_range_max=850,
                factors=[], as_of_date=date(2026, 1, 1),
            ),
        ]


@pytest.mark.asyncio
async def test_credit_score_trend_is_included_when_history_exists():
    fs = FinancialService(_ScoredClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.FINANCIAL_SCORE_QUERY))
    assert result["score"] == 742
    assert result["previous"]["score"] == 700
    assert result["change"] == 42


@pytest.mark.asyncio
async def test_credit_score_has_no_trend_without_history():
    class _NoHistory(InMemoryBackendClient):
        async def get_financial_score_history(self, context):
            return []

    fs = FinancialService(_NoHistory())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.FINANCIAL_SCORE_QUERY))
    assert "previous" not in result
    assert "change" not in result


class _AlloClient(InMemoryBackendClient):
    async def get_allo_score(self, context):
        return AlloScore(
            owner_user_id=context.user_id, overall_score=82, overall_tier="Strong",
            flow_score=70, flow_tier="Strong", stability_score=90, stability_tier="Strong",
            retirement_score=80, retirement_tier="Strong",
            period_start=date(2026, 1, 1), period_end=date(2026, 7, 1),
        )

    async def get_allo_score_history(self, context):
        current = await self.get_allo_score(context)
        previous = current.model_copy(update={
            "overall_score": 65, "flow_score": 55, "period_end": date(2026, 1, 1),
            "period_start": date(2025, 7, 1),
        })
        return [current, previous]


@pytest.mark.asyncio
async def test_allo_score_trend_includes_per_dimension_change():
    fs = FinancialService(_AlloClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.ALLO_SCORE_QUERY))
    assert result["change"]["overall"] == 17
    assert result["change"]["flow"] == 15
    assert result["change"]["stability"] == 0


class _DebtClient(InMemoryBackendClient):
    async def get_liabilities(self, context):
        return [
            Liability(owner_user_id=context.user_id, liability_id="c1", name="Card", type="credit_card",
                      balance=Money(amount=Decimal("800"), currency="USD")),
        ]

    async def get_liability_history(self, context):
        today = date.today()
        return [
            LiabilitySnapshot(owner_user_id=context.user_id, account_id="all", account_type="total",
                              balance=Money(amount=Decimal("1000"), currency="USD"),
                              snapshot_date=today - timedelta(days=30)),
            LiabilitySnapshot(owner_user_id=context.user_id, account_id="all", account_type="total",
                              balance=Money(amount=Decimal("1200"), currency="USD"),
                              snapshot_date=date(today.year, 1, 1)),
        ]


@pytest.mark.asyncio
async def test_debt_trend_reports_direction_and_ytd_paydown():
    fs = FinancialService(_DebtClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.LIABILITY_QUERY))
    assert result["total_balance"] == "800"
    assert result["trend"]["previous_total"] == "1000"
    assert result["trend"]["change"] == "-200"  # improved: debt went down
    assert result["trend"]["year_to_date_paydown"] == "400"  # 1200 at year start -> 800 now


@pytest.mark.asyncio
async def test_debt_has_no_trend_without_history():
    class _NoHistory(InMemoryBackendClient):
        async def get_liability_history(self, context):
            return []

    fs = FinancialService(_NoHistory())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.LIABILITY_QUERY))
    assert "trend" not in result
