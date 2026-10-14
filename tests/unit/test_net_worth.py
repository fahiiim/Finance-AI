"""
Net worth tool tests.

Regression guard: a credit-card balance must not be counted once as an
asset (via get_accounts) and once as a liability (via get_liabilities) --
that inflates both sides and can hide a real negative net worth.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.financial import Account, DataAvailability, Liability, Money
from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context
from tests.support.in_memory_backend import InMemoryBackendClient


def _plan() -> ExecutionPlan:
    return ExecutionPlan(intent=Intent.NET_WORTH_QUERY, requires_financial_data=True, requires_calculation=True)


class _StubClient(InMemoryBackendClient):
    """Overrides just what net worth reads, with fixed (non-random) values."""

    async def get_accounts(self, context):
        return [
            Account(owner_user_id=context.user_id, account_id="chk", name="Checking", type="checking",
                     balance=Money(amount=Decimal("1000"), currency="USD")),
            Account(owner_user_id=context.user_id, account_id="crd", name="Card", type="credit",
                     balance=Money(amount=Decimal("300"), currency="USD")),
        ]

    async def get_liabilities(self, context):
        return [
            Liability(owner_user_id=context.user_id, liability_id="crd", name="Card", type="credit_card",
                       balance=Money(amount=Decimal("300"), currency="USD")),
        ]


@pytest.mark.asyncio
async def test_a_credit_card_is_not_double_counted():
    fs = FinancialService(_StubClient())
    result, tools = await route_and_execute(fs, make_user_context(user_id="1"), _plan())

    # $1000 checking + investments (the test double seeds one holding) - $300 card.
    assert result["liquid_accounts"]["total"] == "1000"
    assert "get_accounts" in tools
    assert "get_liabilities" in tools

    net_worth = Decimal(result["net_worth"])
    liquid = Decimal(result["liquid_accounts"]["total"])
    investments = Decimal(result["investments"]["total"])
    liabilities = Decimal(result["liabilities"]["total"])
    assert net_worth == liquid + investments - liabilities


@pytest.mark.asyncio
async def test_missing_components_are_excluded_not_zeroed():
    class _NoLiabilities(InMemoryBackendClient):
        async def get_liabilities(self, context):
            return []

    fs = FinancialService(_NoLiabilities())
    result, _ = await route_and_execute(fs, make_user_context(user_id="2"), _plan())

    assert result["liabilities"]["availability"] == DataAvailability.MISSING.value
    assert result["liabilities"]["total"] is None
    # Net worth still computed from what IS available (accounts + investments).
    assert result["net_worth"] is not None
    assert "liabilities" in result["note"]


@pytest.mark.asyncio
async def test_nothing_available_reports_unavailable_not_zero():
    class _NothingClient(InMemoryBackendClient):
        async def get_accounts(self, context):
            return []

        async def get_investments(self, context):
            return []

        async def get_liabilities(self, context):
            return []

    fs = FinancialService(_NothingClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="3"), _plan())
    assert result["net_worth"] is None
