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
    # Payment status, where the source records it (credit cards in this
    # database do). None means unknown, not "fine".
    is_overdue: bool | None = None
    next_payment_due_date: date | None = None
    last_payment_date: date | None = None


class FinancialScore(OwnedRecord):
    """Authoritative, backend-computed score. The AI service explains, never invents, this."""

    score: int
    score_range_min: int
    score_range_max: int
    factors: list[str] = Field(default_factory=list)
    as_of_date: date | None = None


class AlloScore(OwnedRecord):
    """
    The app's own 0-100 financial-health score, with per-dimension insight.

    Deliberately NOT merged into FinancialScore: that type's score_range is
    a credit-bureau scale (e.g. 300-850) and its factors are a flat string
    list. This is a different scoring system entirely (overall + flow +
    stability + retirement, each with its own tier and narrative insight) --
    conflating them would mean forcing one shape onto two unrelated scores.
    """

    overall_score: int
    overall_tier: str
    overall_insight: str | None = None
    flow_score: int
    flow_tier: str
    flow_insight: str | None = None
    stability_score: int
    stability_tier: str
    stability_insight: str | None = None
    retirement_score: int
    retirement_tier: str
    retirement_insight: str | None = None
    period_start: date
    period_end: date


class SavingsGoal(OwnedRecord):
    """A user-set savings target. The backend stores the target and dates;
    progress toward it is derived (see app/tools/savings_goal.py), never
    stored, since there is no "current progress" column to pass through."""

    goal_id: str
    target_amount: Money
    monthly_target: Money | None = None
    start_date: date | None = None
    end_date: date | None = None
    # Progress as the BACKEND computes it (what the app shows), when it serves
    # one. None means the source has no such figure and the tool derives
    # progress from income minus expenses instead.
    saved_amount: Money | None = None


class LiabilitySnapshot(OwnedRecord):
    """A point-in-time total debt balance, for trend questions ('is my debt
    getting better or worse'). Deliberately just account_type + balance +
    date -- the per-account detail lives in Liability; this is for the
    aggregate trend, not a second copy of the current breakdown."""

    account_id: str
    account_type: str
    balance: Money
    snapshot_date: date


class RetirementReadiness(OwnedRecord):
    """
    Backend-computed retirement-readiness figures, parsed from the Allo
    Score snapshot's raw_metrics JSON. Every field here is something the
    backend already computed -- this service structures and passes it
    through, never recomputes "years behind" or similar itself.
    """

    age: int | None = None
    target_retirement_age: int | None = None
    years_remaining: int | None = None
    retirement_target: Money | None = None
    future_assets: Money | None = None
    monthly_retirement_income_goal: Money | None = None
    annual_contribution: Money | None = None
    savings_rate: Decimal | None = None
    investment_rate: Decimal | None = None
    readiness_ratio: Decimal | None = None
    # Current position, from the same metrics -- distinct from `future_assets`,
    # which is a PROJECTION at retirement age and was once quoted as "what you
    # have saved" (a negative $716,979).
    total_savings: Money | None = None
    total_assets: Money | None = None
    total_cash: Money | None = None
    total_debt: Money | None = None
    investment_contributions: Money | None = None
    investment_contribution_months: int | None = None


class NetWorth(OwnedRecord):
    """
    The backend's own net-worth figure, as the app itself displays it.

    Distinct from the figure net_worth.py derives from accounts + holdings -
    debts: the backend also counts manually entered assets, property,
    vehicles and business value, so the two legitimately differ. When the
    backend supplies this, it is what the user sees in the app and is
    therefore the number the assistant must agree with.
    """

    net_worth: Money
    total_assets: Money
    total_liabilities: Money
    as_of_date: date | None = None
    # Month-end history as the backend serves it, oldest first:
    # [{"month": "May 2026", "net_worth": ..., "total_assets": ..., "total_liabilities": ...}]
    history: list[dict] = Field(default_factory=list)
    # Component name -> value, e.g. {"property": Money(...)}. Kept as the
    # backend labels them: this service does not reinterpret the breakdown.
    asset_breakdown: dict[str, Money] = Field(default_factory=dict)


class FinancialSummary(OwnedRecord):
    period_start: date
    period_end: date
    total_income: Money | None = None
    total_expenses: Money | None = None
    net_cash_flow: Money | None = None
    income_availability: DataAvailability = DataAvailability.AVAILABLE
    expenses_availability: DataAvailability = DataAvailability.AVAILABLE
