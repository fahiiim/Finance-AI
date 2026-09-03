"""
Affordability planning tests.

Regression: `max_amount` was overloaded as both a transaction-list FILTER and
the sum the user is asking about. The planner had no field meaning "the user
named a figure", so "Can I afford to spend $500?" reached the tool with no
amount and returned 422 -- a headline feature that always failed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.orchestrator.intent_router import route_and_execute
from app.schemas.planner import ExecutionPlan, Intent, RawExecutionPlan, ResolvedDateRange
from app.services.financial_service import FinancialService
from app.services.mock_backend_client import MockBackendClient
from tests.conftest import make_user_context


def _plan(**kw) -> ExecutionPlan:
    base = dict(
        intent=Intent.AFFORDABILITY_ANALYSIS,
        requires_financial_data=True,
        requires_calculation=True,
    )
    base.update(kw)
    return ExecutionPlan(**base)


def test_amount_in_question_survives_raw_to_resolved():
    raw = RawExecutionPlan(
        intent=Intent.AFFORDABILITY_ANALYSIS,
        requires_financial_data=True,
        requires_calculation=True,
        amount_in_question=500.0,
    )
    assert ExecutionPlan.from_raw(raw, None).amount_in_question == 500.0


@pytest.mark.asyncio
async def test_affordability_uses_amount_in_question():
    fs = FinancialService(MockBackendClient())
    result, tools = await route_and_execute(fs, make_user_context(user_id="42"), _plan(amount_in_question=500.0))
    assert Decimal(result["amount_in_question"]) == Decimal("500")
    assert "get_accounts" in tools


@pytest.mark.asyncio
async def test_max_amount_still_accepted_as_a_fallback():
    """Older plans put the figure in max_amount; don't 422 a question we can answer."""
    fs = FinancialService(MockBackendClient())
    result, _ = await route_and_execute(fs, make_user_context(user_id="42"), _plan(max_amount=250.0))
    assert Decimal(result["amount_in_question"]) == Decimal("250")


@pytest.mark.asyncio
async def test_no_amount_still_reports_what_the_user_has():
    """
    "Should I buy a 4K OLED TV?" names no price, and this used to raise --
    which reached the user mid-chat as a red "422 validation_failed" box.

    A missing price means no verdict, not no answer: report the uncommitted
    balance and ask what it costs.
    """
    fs = FinancialService(MockBackendClient())
    result, tools = await route_and_execute(fs, make_user_context(user_id="42"), _plan())

    assert result["amount_in_question"] is None
    assert "projected_remaining_after_spend" not in result, "no verdict without a price"
    assert Decimal(result["available_balance"]) >= 0
    assert Decimal(result["uncommitted_balance"]) <= Decimal(result["available_balance"])
    assert "ask what the item costs" in result["note"]
    assert "get_accounts" in tools


@pytest.mark.asyncio
async def test_min_max_amount_remain_transaction_filters():
    """
    The overload is gone: min/max_amount must still narrow a transaction list
    and must NOT be read as an affordability figure.
    """
    fs = FinancialService(MockBackendClient())
    ctx = make_user_context(user_id="42")
    plan = ExecutionPlan(
        intent=Intent.TRANSACTION_QUERY,
        requires_financial_data=True,
        requires_calculation=False,
        date_range=ResolvedDateRange(start="2026-07-01", end="2026-07-31", label="last_month"),
        min_amount=100.0,
    )
    result, _ = await route_and_execute(fs, ctx, plan)
    assert result is not None


# --------------------------------------------------------------------------
# Pricing an item the user names but does not price.
#
# "Should I buy a 4K OLED TV?" is the question people most want to ask a
# finance assistant, and answering only "tell me the price" fails it. The
# price is world knowledge, so the planner may supply one; every other figure
# -- balance, bills, what is left, the verdict -- still comes from the user's
# data and the application's arithmetic. Keeping those in separate fields is
# what lets the answer say which number came from where.
# --------------------------------------------------------------------------


class _Stub(MockBackendClient):
    """Fixed balance and no bills, so a verdict threshold is exactly testable."""

    def __init__(self, balance: str) -> None:
        self._balance = Decimal(balance)

    async def get_accounts(self, context):
        from app.schemas.financial import Account, Money

        return [
            Account(
                owner_user_id=context.user_id,
                account_id="a1",
                name="Checking",
                type="checking",
                balance=Money(amount=self._balance, currency="USD"),
            )
        ]

    async def get_bills(self, context, *, start=None, end=None):
        return []


async def _verdict(balance: str, **plan_kw) -> dict:
    fs = FinancialService(_Stub(balance))
    result, _ = await route_and_execute(fs, make_user_context(user_id="42"), _plan(**plan_kw))
    return result


@pytest.mark.asyncio
async def test_an_estimated_price_produces_a_verdict():
    result = await _verdict("7000", estimated_price=1500.0, estimated_price_basis="a 55-inch OLED runs $1,300-$1,800")
    assert result["amount_source"] == "estimated"
    assert Decimal(result["amount_in_question"]) == Decimal("1500")
    assert Decimal(result["projected_remaining_after_spend"]) == Decimal("5500")
    assert result["verdict"] == "comfortable"
    assert "1,300" in result["estimated_price_basis"]
    assert "ESTIMATE" in result["note"], "the answer must be told this figure is not from their data"


@pytest.mark.asyncio
async def test_a_price_the_user_states_beats_the_estimate():
    """Their number is authoritative; ours is a stand-in until they give one."""
    result = await _verdict("7000", amount_in_question=899.0, estimated_price=1500.0)
    assert result["amount_source"] == "stated"
    assert Decimal(result["amount_in_question"]) == Decimal("899")
    assert "estimated_price_basis" not in result
    assert "note" not in result, "nothing to disclaim when the user set the price"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "price,expected",
    [
        ("1000", "comfortable"),  # leaves 3,000 of 4,000
        ("3100", "tight"),        # leaves 900, under a quarter of 4,000
        ("4000", "tight"),        # leaves exactly nothing but clears
        ("4001", "not_affordable"),
    ],
)
async def test_the_verdict_thresholds(price, expected):
    result = await _verdict("4000", amount_in_question=float(price))
    assert result["verdict"] == expected


@pytest.mark.asyncio
async def test_clearing_to_almost_zero_is_not_called_comfortable():
    """
    Affordable in arithmetic and reckless in practice are not the same thing,
    and an assistant that calls them both "yes" is not worth trusting.
    """
    result = await _verdict("5000", amount_in_question=4900.0)
    assert result["verdict"] == "tight"
    assert Decimal(result["projected_remaining_after_spend"]) == Decimal("100")


@pytest.mark.asyncio
async def test_an_unpriceable_item_asks_rather_than_guessing():
    """"Can I afford a wedding?" has no typical price worth quoting."""
    result = await _verdict("7000")
    assert result["amount_in_question"] is None
    assert "verdict" not in result
    assert Decimal(result["uncommitted_balance"]) == Decimal("7000")
    assert "ask what the item costs" in result["note"]


@pytest.mark.asyncio
async def test_bills_are_committed_before_any_verdict():
    """Money already owed is not money available to spend on a television."""
    fs = FinancialService(MockBackendClient())
    result, _ = await route_and_execute(
        fs, make_user_context(user_id="42"), _plan(amount_in_question=100.0)
    )
    assert Decimal(result["uncommitted_balance"]) == (
        Decimal(result["available_balance"]) - Decimal(result["upcoming_unpaid_bills_14_days"])
    )


def test_the_estimate_fields_survive_raw_to_resolved():
    raw = RawExecutionPlan(
        intent=Intent.AFFORDABILITY_ANALYSIS,
        requires_financial_data=True,
        requires_calculation=True,
        estimated_price=1500.0,
        estimated_price_basis="typical range",
    )
    plan = ExecutionPlan.from_raw(raw, None)
    assert plan.estimated_price == 1500.0
    assert plan.estimated_price_basis == "typical range"
    assert plan.amount_in_question is None, "an estimate must never masquerade as a stated sum"


class _NoAccounts(MockBackendClient):
    async def get_accounts(self, context):
        return []

    async def get_bills(self, context, *, start=None, end=None):
        return []


@pytest.mark.asyncio
async def test_no_linked_accounts_is_unknown_not_unaffordable():
    """
    A user who has not connected their bank has an UNKNOWN balance, not a zero
    one. Treating the two as the same answered "can I afford this?" with a
    confident "no" for every such user, and nothing in the answer revealed
    that the balance had never been read.
    """
    fs = FinancialService(_NoAccounts())
    result, tools = await route_and_execute(
        fs, make_user_context(user_id="42"), _plan(amount_in_question=1200.0)
    )
    assert result["verdict"] is None, "no verdict is possible without a balance"
    assert result["available_balance"] is None
    assert "UNKNOWN" in result["note"]
    assert "Do NOT say the purchase is unaffordable" in result["note"]
    assert tools == ["get_accounts"]


@pytest.mark.asyncio
async def test_a_genuine_zero_balance_still_gets_a_verdict():
    """An account that exists and holds nothing is a real, knowable answer."""
    result = await _verdict("0", amount_in_question=50.0)
    assert result["verdict"] == "not_affordable"
    assert Decimal(result["available_balance"]) == Decimal("0")


@pytest.mark.asyncio
async def test_every_user_can_be_asked_the_same_question():
    """
    The capability is per-request, not per-user: nothing about affordability
    depends on which account is being asked about.
    """
    fs = FinancialService(MockBackendClient())
    for user_id in ("1", "42", "99", "7", "264", "12345"):
        result, _ = await route_and_execute(
            fs, make_user_context(user_id=user_id), _plan(estimated_price=1200.0)
        )
        assert result["verdict"] in {"comfortable", "tight", "not_affordable"}, user_id
        assert result["amount_source"] == "estimated", user_id
