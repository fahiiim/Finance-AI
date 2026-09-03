"""
Financial domain schemas.

These model the shape of data the backend/database is expected to provide.
Monetary fields use Decimal, never float, so calculations downstream never
accumulate floating-point rounding error in currency arithmetic.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class DataAvailability(str, Enum):
    """
    Distinguishes "the value is genuinely zero" from "we don't have it".

    Analytics tools return this alongside results so the prompt layer can
    tell the model when to say "unavailable" rather than silently treating
    missing data as zero (see financial-ai-analytics-engineer skill).
    """

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str = "USD"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.amount} {self.currency}"


class OwnedRecord(BaseModel):
    """
    Base for records that belong to exactly one user.

    `owner_user_id` lets this service VERIFY that data it received is data it
    asked for, instead of trusting the backend's scoping blindly (see
    app/core/data_isolation.py). Optional because the AI service cannot force
    the backend to emit it — but every record that carries it gets checked,
    and `STRICT_DATA_OWNERSHIP=true` makes an unlabeled record an error.
    """

    owner_user_id: str | None = None


class Account(OwnedRecord):
    account_id: str
    name: str
    type: str  # checking, savings, credit, investment, loan, etc.
    balance: Money
    institution_name: str | None = None
    is_active: bool = True


class Transaction(OwnedRecord):
    transaction_id: str
    account_id: str
    # POSITIVE = outflow (a purchase), NEGATIVE = inflow (a deposit) — Plaid's
    # own convention, verified against the production data. Never compare this
    # field directly; use app/utils/transactions.py, which defines the rule once.
    amount: Decimal
    currency: str = "USD"
    date: date
    merchant_name: str | None = None
    description: str | None = None
    category: str | None = None
    # The finer label under `category`, when the source has one: Plaid's
    # detailed tier separates Restaurant from Groceries inside Food And Drink.
    # Optional because not every backend supplies it -- callers that use it
    # must degrade to the parent category and SAY they did, never silently
    # answer "how much on restaurants?" with all food spending.
    subcategory: str | None = None
    pending: bool = False


class Bill(OwnedRecord):
    bill_id: str
    name: str
    amount: Money
    due_date: date
    is_paid: bool = False


class BudgetCategory(OwnedRecord):
    category: str
    budgeted_amount: Money
    spent_amount: Money


class Subscription(OwnedRecord):
    subscription_id: str
    name: str
    amount: Money
    billing_cycle: str  # monthly, yearly, weekly, etc.
    next_charge_date: date | None = None
    is_active: bool = True


class RecurringTransaction(OwnedRecord):
    recurring_id: str
    name: str
    amount: Money
    frequency: str
    category: str | None = None
    next_expected_date: date | None = None


class InvestmentHolding(OwnedRecord):
    holding_id: str
    account_id: str
    symbol: str | None = None
    name: str
    quantity: Decimal
    market_value: Money
    cost_basis: Money | None = None


class Liability(OwnedRecord):
    liability_id: str
    name: str
    type: str  # credit_card, student_loan, mortgage, auto_loan, etc.
    balance: Money
    interest_rate: Decimal | None = None
    minimum_payment: Money | None = None


class FinancialScore(OwnedRecord):
    """Authoritative, backend-computed score. The AI service explains, never invents, this."""

    score: int
    score_range_min: int
    score_range_max: int
    factors: list[str] = Field(default_factory=list)
    as_of_date: date | None = None


class FinancialSummary(OwnedRecord):
    period_start: date
    period_end: date
    total_income: Money | None = None
    total_expenses: Money | None = None
    net_cash_flow: Money | None = None
    income_availability: DataAvailability = DataAvailability.AVAILABLE
    expenses_availability: DataAvailability = DataAvailability.AVAILABLE
