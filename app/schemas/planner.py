"""
Structured intent + execution plan schemas.

The planner LLM call is constrained to return one of these shapes via
structured output (see llm/schemas.py + orchestrator/planner.py). The
orchestrator never parses fragile free-form text to decide which tools
to run.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Intent(str, Enum):
    GENERAL_CONVERSATION = "GENERAL_CONVERSATION"
    TRANSACTION_QUERY = "TRANSACTION_QUERY"
    SPENDING_ANALYSIS = "SPENDING_ANALYSIS"
    INCOME_QUERY = "INCOME_QUERY"
    BALANCE_QUERY = "BALANCE_QUERY"
    BILL_QUERY = "BILL_QUERY"
    BUDGET_QUERY = "BUDGET_QUERY"
    SUBSCRIPTION_QUERY = "SUBSCRIPTION_QUERY"
    RECURRING_EXPENSE_QUERY = "RECURRING_EXPENSE_QUERY"
    CATEGORY_ANALYSIS = "CATEGORY_ANALYSIS"
    MERCHANT_ANALYSIS = "MERCHANT_ANALYSIS"
    SPENDING_SPIKE = "SPENDING_SPIKE"
    PERIOD_COMPARISON = "PERIOD_COMPARISON"
    SAVINGS_ANALYSIS = "SAVINGS_ANALYSIS"
    # "Am I on track for my savings goal?" / "how much do I need to save
    # monthly?" / "when will I hit my goal?" -- all need the stored goal
    # (target, monthly target, deadline), distinct from SAVINGS_ANALYSIS's
    # generic savings-rate-over-a-period question.
    SAVINGS_GOAL_QUERY = "SAVINGS_GOAL_QUERY"
    CASH_FLOW_ANALYSIS = "CASH_FLOW_ANALYSIS"
    AFFORDABILITY_ANALYSIS = "AFFORDABILITY_ANALYSIS"
    FORECAST_QUERY = "FORECAST_QUERY"
    INVESTMENT_QUERY = "INVESTMENT_QUERY"
    LIABILITY_QUERY = "LIABILITY_QUERY"
    FINANCIAL_SCORE_QUERY = "FINANCIAL_SCORE_QUERY"
    # The app's own 0-100 financial-health score (flow/stability/retirement) --
    # distinct from FINANCIAL_SCORE_QUERY's credit-bureau score. See AlloScore.
    ALLO_SCORE_QUERY = "ALLO_SCORE_QUERY"
    # Total assets (liquid accounts + investments) minus total liabilities.
    NET_WORTH_QUERY = "NET_WORTH_QUERY"
    # Backend-computed retirement readiness (target age, readiness ratio,
    # projected future assets) -- distinct from a generic investment query.
    RETIREMENT_QUERY = "RETIREMENT_QUERY"
    # Spendable-cash questions: safe to spend/save, cushion, emergency fund,
    # runway, cash left after bills, month-end sufficiency.
    LIQUIDITY_QUERY = "LIQUIDITY_QUERY"
    # What-if debt questions: extra monthly payment, paying off the smallest
    # debt, late or at-risk payments.
    DEBT_SCENARIO_QUERY = "DEBT_SCENARIO_QUERY"
    # Where spending could be trimmed: discretionary categories, top
    # merchants and subscriptions over recent complete months.
    SAVINGS_OPPORTUNITIES = "SAVINGS_OPPORTUNITIES"
    # Spending since the most recent paycheck (anchored to the deposit, not
    # the calendar month).
    SPENDING_SINCE_PAYCHECK = "SPENDING_SINCE_PAYCHECK"
    # Broad recap across cash flow, net worth, debt, bills, scores
    # ("catch me up", "summarize my finances", "why isn't my net worth growing").
    FINANCIAL_OVERVIEW = "FINANCIAL_OVERVIEW"
    FINANCIAL_CALCULATION = "FINANCIAL_CALCULATION"
    OVERALL_FINANCIAL_HEALTH = "OVERALL_FINANCIAL_HEALTH"
    UNKNOWN = "UNKNOWN"


# Intents that never touch financial data or tools at all.
NO_DATA_INTENTS = frozenset({Intent.GENERAL_CONVERSATION, Intent.UNKNOWN})

# Intents whose answer is meaningless without a window: "how much do I spend
# at Amazon?" has no answer until you say over what period.
#
# The user often does not name one, because in conversation it is obvious --
# and the tools raised ValidationFailedError, which surfaced to the user as a
# raw 422 error body. Four of five ordinary questions failed that way: "what
# is my dining budget?", "how much do I spend at Amazon?", "what is my
# savings rate?", "am I spending more than usual?".
#
# These default to the current month and SAY so, which is what a person would
# do. An assumption stated in the answer can be corrected; an error cannot.
# tests/unit/test_period_defaults.py checks this set against the handlers
# that actually demand a range, so the two cannot drift apart.
PERIOD_SCOPED_INTENTS = frozenset(
    {
        Intent.SPENDING_ANALYSIS,
        Intent.INCOME_QUERY,
        Intent.BUDGET_QUERY,
        Intent.CATEGORY_ANALYSIS,
        Intent.MERCHANT_ANALYSIS,
        Intent.SPENDING_SPIKE,
        Intent.PERIOD_COMPARISON,
        Intent.SAVINGS_ANALYSIS,
        Intent.CASH_FLOW_ANALYSIS,
    }
)

# The window assumed when a period-scoped question names no period.
DEFAULT_PERIOD_PHRASE = "this month"


class ResolvedDateRange(BaseModel):
    start: date
    end: date
    label: str  # e.g. "this_month", "last_30_days" — the phrase that was resolved

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: date, info) -> date:
        start = info.data.get("start")
        if start and v < start:
            raise ValueError("end date must not be before start date")
        return v


class RawExecutionPlan(BaseModel):
    """
    Direct structured output FROM THE LLM.

    Deliberately holds a raw `date_phrase` string (e.g. "last month",
    "past 30 days") rather than concrete dates — the model is never trusted
    to do date arithmetic. app/utils/dates.py resolves the phrase
    deterministically afterward, in the user's actual timezone.
    """

    intent: Intent
    requires_financial_data: bool
    requires_calculation: bool
    required_tools: list[str] = Field(default_factory=list)
    date_phrase: str | None = None
    # A comparison names TWO periods, so it needs two slots. With only
    # date_phrase, the model put the BASELINE in it ("compare that to last
    # month" -> "last month") and the router, which treats date_phrase as the
    # CURRENT period, compared July against June for a question about August
    # against July. Both numbers were real, the arithmetic was right, and the
    # answer was about the wrong two months.
    comparison_baseline_phrase: str | None = None
    category_filter: str | None = None
    merchant_filter: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    # The amount the user is ASKING ABOUT ("can I afford $500?"), which is a
    # different thing from min/max_amount — those filter a transaction list.
    # Overloading max_amount for both meant the planner had no way to express
    # "the user named a sum", and affordability questions failed with a 422.
    amount_in_question: float | None = None
    # A typical market price for an item the user named but did not price
    # ("should I buy a 4K OLED TV?"). This is WORLD KNOWLEDGE, not the user's
    # financial data -- the distinction that makes it safe. The user's
    # balances and spending must always come from the database; what a
    # television costs does not, and refusing to know it made the assistant
    # useless for the one question people most want to ask.
    #
    # It is an ESTIMATE and is always labelled as one in the answer. The
    # verdict itself is computed by the application from real balances.
    estimated_price: float | None = None
    estimated_price_basis: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    resolution_note: str | None = None


class ExecutionPlan(BaseModel):
    """The fully resolved plan used by the orchestrator — dates are concrete, not phrases."""

    intent: Intent
    requires_financial_data: bool
    requires_calculation: bool
    required_tools: list[str] = Field(default_factory=list)
    date_range: ResolvedDateRange | None = None
    # The phrase the range was resolved FROM, kept so the conversation can
    # remember which period it is talking about. A follow-up that names no
    # period inherits this one instead of provoking another question.
    date_phrase: str | None = None
    # The earlier side of a comparison, when the user named it. None means
    # "derive it" -- see previous_calendar_period().
    baseline_range: ResolvedDateRange | None = None
    baseline_phrase: str | None = None
    category_filter: str | None = None
    # "Fridays in June" is a month-long window narrowed to one weekday -- not
    # a contiguous range, so it cannot be expressed as a date range alone.
    # Monday is 0, matching date.weekday().
    #
    # DERIVED, never taken from the model. It was briefly a field on
    # RawExecutionPlan and the model filled it with 5 for Friday, shifting
    # every figure by a day with nothing in the output to show it -- the same
    # reason concrete dates are resolved here and not asked of the LLM.
    weekday_filter: int | None = None
    merchant_filter: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    amount_in_question: float | None = None
    estimated_price: float | None = None
    estimated_price_basis: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    # Free-text note carried to the response-generation prompt about what
    # follow-up context (e.g. "the month before") this plan resolved from.
    resolution_note: str | None = None

    @classmethod
    def from_raw(
        cls,
        raw: RawExecutionPlan,
        date_range: ResolvedDateRange | None,
        baseline_range: ResolvedDateRange | None = None,
        weekday_filter: int | None = None,
    ) -> ExecutionPlan:
        return cls(
            intent=raw.intent,
            requires_financial_data=raw.requires_financial_data,
            requires_calculation=raw.requires_calculation,
            required_tools=raw.required_tools,
            date_range=date_range,
            date_phrase=raw.date_phrase,
            baseline_range=baseline_range,
            baseline_phrase=raw.comparison_baseline_phrase,
            category_filter=raw.category_filter,
            weekday_filter=weekday_filter,
            merchant_filter=raw.merchant_filter,
            min_amount=raw.min_amount,
            max_amount=raw.max_amount,
            amount_in_question=raw.amount_in_question,
            estimated_price=raw.estimated_price,
            estimated_price_basis=raw.estimated_price_basis,
            needs_clarification=raw.needs_clarification,
            clarification_question=raw.clarification_question,
            resolution_note=raw.resolution_note,
        )
