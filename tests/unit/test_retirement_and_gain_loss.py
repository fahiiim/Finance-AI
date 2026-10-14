"""
Retirement readiness and unrealized gain/loss tool tests.

Retirement: every field is backend-computed and passed through; the tool
must never invent a derived figure. Gain/loss: a holding without a cost
basis is unknown, not break-even, and must not be summed as zero.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.financial import DataAvailability, InvestmentHolding, Money, RetirementReadiness
from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient


def _plan(intent: Intent) -> ExecutionPlan:
    return ExecutionPlan(intent=intent, requires_financial_data=True, requires_calculation=False)


class _RetirementClient(InMemoryBackendClient):
    async def get_retirement_readiness(self, context):
        return RetirementReadiness(
            owner_user_id=context.user_id,
            age=34,
            target_retirement_age=64,
            years_remaining=30,
            retirement_target=Money(amount=Decimal("900000"), currency="USD"),
            future_assets=Money(amount=Decimal("450000"), currency="USD"),
            monthly_retirement_income_goal=Money(amount=Decimal("3000"), currency="USD"),
            annual_contribution=Money(amount=Decimal("12000"), currency="USD"),
            savings_rate=Decimal("0.05"),
            investment_rate=Decimal("0.04"),
            readiness_ratio=Decimal("0.5"),
        )


@pytest.mark.asyncio
async def test_retirement_fields_are_passed_through_unchanged():
    fs = FinancialService(_RetirementClient())
    result, tools = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.RETIREMENT_QUERY))

    assert result["availability"] == DataAvailability.AVAILABLE.value
    assert result["target_retirement_age"] == 64
    assert result["years_remaining"] == 30
    assert result["retirement_target"] == "900000"
    assert result["projected_future_assets"] == "450000"
    assert result["readiness_ratio"] == "0.5"
    assert "get_retirement_readiness" in tools


@pytest.mark.asyncio
async def test_no_retirement_data_is_reported_as_missing_not_invented():
    class _None(InMemoryBackendClient):
        async def get_retirement_readiness(self, context):
            return None

    fs = FinancialService(_None())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.RETIREMENT_QUERY))
    assert result["availability"] == DataAvailability.MISSING.value
    assert "readiness_ratio" not in result


class _HoldingsClient(InMemoryBackendClient):
    async def get_investments(self, context):
        return [
            InvestmentHolding(owner_user_id=context.user_id, holding_id="h1", account_id="a", name="Fund A",
                              symbol="AAA", quantity=Decimal("10"),
                              market_value=Money(amount=Decimal("1200"), currency="USD"),
                              cost_basis=Money(amount=Decimal("1000"), currency="USD")),
            InvestmentHolding(owner_user_id=context.user_id, holding_id="h2", account_id="a", name="Fund B",
                              symbol="BBB", quantity=Decimal("5"),
                              market_value=Money(amount=Decimal("400"), currency="USD"),
                              cost_basis=Money(amount=Decimal("500"), currency="USD")),
            # No recorded cost basis: unknown, must not count as zero gain.
            InvestmentHolding(owner_user_id=context.user_id, holding_id="h3", account_id="a", name="Fund C",
                              symbol="CCC", quantity=Decimal("1"),
                              market_value=Money(amount=Decimal("300"), currency="USD")),
        ]


@pytest.mark.asyncio
async def test_unrealized_gain_loss_only_covers_holdings_with_a_cost_basis():
    fs = FinancialService(_HoldingsClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.INVESTMENT_QUERY))

    by_symbol = {h["symbol"]: h for h in result["holdings"]}
    assert by_symbol["AAA"]["unrealized_gain_loss"] == "200"
    assert by_symbol["BBB"]["unrealized_gain_loss"] == "-100"
    assert by_symbol["CCC"]["unrealized_gain_loss"] is None
    # 200 + (-100); the cost-basis-less holding is excluded, not treated as zero.
    assert result["total_unrealized_gain_loss"] == "100"
    assert result["gain_loss_note"] is not None


@pytest.mark.asyncio
async def test_no_gain_loss_total_when_no_holding_has_a_cost_basis():
    class _NoBasis(InMemoryBackendClient):
        async def get_investments(self, context):
            return [
                InvestmentHolding(owner_user_id=context.user_id, holding_id="h", account_id="a", name="X",
                                  quantity=Decimal("1"),
                                  market_value=Money(amount=Decimal("10"), currency="USD")),
            ]

    fs = FinancialService(_NoBasis())
    result, _ = await route_and_execute(fs, make_user_context(user_id="1"), _plan(Intent.INVESTMENT_QUERY))
    assert result["total_unrealized_gain_loss"] is None
