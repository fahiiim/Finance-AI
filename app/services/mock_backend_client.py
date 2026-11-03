"""
Mock backend client.

Used for local development and the test suite when no real backend is
available. Data is generated deterministically per user_id (via a seeded
RNG) so that:
  - the same user always sees the same data across calls, and
  - two different user_ids NEVER see each other's data —
    this is what the cross-user isolation tests exercise (see
    tests/unit/test_user_isolation.py).

This is explicitly a development aid, not a production integration — see
docs/assumptions.md.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import (
    Account,
    AlloScore,
    Bill,
    BudgetCategory,
    FinancialScore,
    FinancialSummary,
    InvestmentHolding,
    Liability,
    LiabilitySnapshot,
    Money,
    RecurringTransaction,
    RetirementReadiness,
    SavingsGoal,
    Subscription,
    Transaction,
)
from app.services.backend_client import BackendClient
from app.utils.transactions import expense_amount, expenses, income, income_amount

# Merchant -> category, rather than two independent draws. Picking each at
# random produced "Netflix (Groceries)" and "Trader Joe's (Subscriptions)" in
# real output: harmless to the arithmetic, but it makes every demo look broken
# and gives category totals no meaning a reader can check against the
# transaction list.
_MERCHANT_CATEGORIES: dict[str, str] = {
    "Trader Joe's": "Groceries",
    "Whole Foods": "Groceries",
    "Shell Gas": "Transportation",
    "Uber": "Transportation",
    "Netflix": "Subscriptions",
    "Spotify": "Subscriptions",
    "Gym Membership": "Subscriptions",
    "Amazon": "Shopping",
    "Target": "Shopping",
    "Local Cafe": "Dining",
    "Chipotle": "Dining",
    "City Power & Light": "Utilities",
    "AMC Theatres": "Entertainment",
    "Steam": "Entertainment",
}
_MERCHANTS = list(_MERCHANT_CATEGORIES)
_CATEGORIES = sorted(set(_MERCHANT_CATEGORIES.values()))


def _seeded_rng(user_id: str) -> random.Random:
    seed = int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


class MockBackendClient(BackendClient):
    async def get_user_profile(self, context: UserContext) -> dict:
        return {"user_id": context.user_id, "currency": context.currency, "timezone": context.timezone}

    async def get_accounts(self, context: UserContext) -> list[Account]:
        rng = _seeded_rng(context.user_id)
        return [
            Account(
                owner_user_id=context.user_id,
                account_id=f"{context.user_id}-checking",
                name="Everyday Checking",
                type="checking",
                balance=Money(amount=Decimal(rng.randrange(500, 6000)), currency=context.currency),
            ),
            Account(
                owner_user_id=context.user_id,
                account_id=f"{context.user_id}-savings",
                name="Savings",
                type="savings",
                balance=Money(amount=Decimal(rng.randrange(1000, 20000)), currency=context.currency),
            ),
        ]

    async def _all_transactions(self, context: UserContext) -> list[Transaction]:
        rng = _seeded_rng(context.user_id)
        today = date.today()
        txns: list[Transaction] = []
        for i in range(120):
            days_ago = rng.randrange(0, 120)
            txn_date = today - timedelta(days=days_ago)
            is_income = rng.random() < 0.05
            # Plaid's sign convention, matching the production database:
            # NEGATIVE = deposit (money in), POSITIVE = purchase (money out).
            # See app/utils/transactions.py.
            amount = -Decimal(rng.randrange(1500, 3500)) if is_income else Decimal(rng.randrange(3, 250))
            merchant = "Employer Payroll" if is_income else rng.choice(_MERCHANTS)
            txns.append(
                Transaction(
                    owner_user_id=context.user_id,
                    transaction_id=f"{context.user_id}-txn-{i}",
                    account_id=f"{context.user_id}-checking",
                    amount=amount,
                    currency=context.currency,
                    date=txn_date,
                    merchant_name=merchant,
                    category="Income" if is_income else _MERCHANT_CATEGORIES[merchant],
                )
            )
        return txns

    async def get_transactions(
        self,
        context: UserContext,
        *,
        start: date | None = None,
        end: date | None = None,
        category: str | None = None,
        merchant: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
    ) -> list[Transaction]:
        txns = await self._all_transactions(context)
        if start:
            txns = [t for t in txns if t.date >= start]
        if end:
            txns = [t for t in txns if t.date <= end]
        if category:
            txns = [t for t in txns if (t.category or "").lower() == category.lower()]
        if merchant:
            txns = [t for t in txns if merchant.lower() in (t.merchant_name or "").lower()]
        if min_amount is not None:
            txns = [t for t in txns if abs(float(t.amount)) >= min_amount]
        if max_amount is not None:
            txns = [t for t in txns if abs(float(t.amount)) <= max_amount]
        return sorted(txns, key=lambda t: t.date, reverse=True)

    async def get_income(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        # Use the shared predicates, never a bare comparison — the sign rule has
        # exactly one definition (app/utils/transactions.py) and this override
        # must not fork it.
        txns = await self.get_transactions(context, start=start, end=end)
        return income(txns)

    async def get_expenses(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        txns = await self.get_transactions(context, start=start, end=end)
        return expenses(txns)

    async def get_bills(
        self, context: UserContext, *, start: date | None = None, end: date | None = None
    ) -> list[Bill]:
        rng = _seeded_rng(context.user_id)
        today = date.today()
        bills = [
            Bill(
                owner_user_id=context.user_id,
                bill_id=f"{context.user_id}-bill-{i}",
                name=name,
                amount=Money(amount=Decimal(rng.randrange(20, 200)), currency=context.currency),
                due_date=today + timedelta(days=rng.randrange(-10, 30)),
                is_paid=rng.random() < 0.3,
            )
            for i, name in enumerate(["Electric", "Internet", "Phone", "Rent"])
        ]
        if start:
            bills = [b for b in bills if b.due_date >= start]
        if end:
            bills = [b for b in bills if b.due_date <= end]
        return bills

    async def get_budgets(self, context: UserContext, *, period_start: date, period_end: date) -> list[BudgetCategory]:
        rng = _seeded_rng(context.user_id)
        # NB: named `outflows`, not `expenses` — that name is the imported
        # predicate helper, and shadowing it here would break any later use.
        outflows = await self.get_expenses(context, start=period_start, end=period_end)
        by_category: dict[str, Decimal] = {}
        for t in outflows:
            by_category[t.category or "Other"] = by_category.get(t.category or "Other", Decimal("0")) + abs(t.amount)
        return [
            BudgetCategory(
                owner_user_id=context.user_id,
                category=cat,
                budgeted_amount=Money(amount=Decimal(rng.randrange(100, 600)), currency=context.currency),
                spent_amount=Money(amount=spent, currency=context.currency),
            )
            for cat, spent in by_category.items()
        ]

    async def get_subscriptions(self, context: UserContext) -> list[Subscription]:
        rng = _seeded_rng(context.user_id)
        names = ["Netflix", "Spotify", "Gym Membership"]
        return [
            Subscription(
                owner_user_id=context.user_id,
                subscription_id=f"{context.user_id}-sub-{i}",
                name=name,
                amount=Money(amount=Decimal(rng.randrange(8, 60)), currency=context.currency),
                billing_cycle="monthly",
                next_charge_date=date.today() + timedelta(days=rng.randrange(1, 30)),
            )
            for i, name in enumerate(names)
        ]

    async def get_recurring_transactions(self, context: UserContext) -> list[RecurringTransaction]:
        rng = _seeded_rng(context.user_id)
        names = ["Rent", "Car Payment", "Insurance"]
        return [
            RecurringTransaction(
                owner_user_id=context.user_id,
                recurring_id=f"{context.user_id}-rec-{i}",
                name=name,
                amount=Money(amount=Decimal(rng.randrange(50, 1500)), currency=context.currency),
                frequency="monthly",
                category="Housing" if name == "Rent" else "Transportation",
            )
            for i, name in enumerate(names)
        ]

    async def get_investments(self, context: UserContext) -> list[InvestmentHolding]:
        rng = _seeded_rng(context.user_id)
        return [
            InvestmentHolding(
                owner_user_id=context.user_id,
                holding_id=f"{context.user_id}-hold-1",
                account_id=f"{context.user_id}-investment",
                symbol="VOO",
                name="Vanguard S&P 500 ETF",
                quantity=Decimal(rng.randrange(1, 40)),
                market_value=Money(amount=Decimal(rng.randrange(500, 15000)), currency=context.currency),
            )
        ]

    async def get_liabilities(self, context: UserContext) -> list[Liability]:
        rng = _seeded_rng(context.user_id)
        return [
            Liability(
                owner_user_id=context.user_id,
                liability_id=f"{context.user_id}-liab-1",
                name="Visa Credit Card",
                type="credit_card",
                balance=Money(amount=Decimal(rng.randrange(0, 4000)), currency=context.currency),
                interest_rate=Decimal("19.99"),
                minimum_payment=Money(amount=Decimal(rng.randrange(25, 150)), currency=context.currency),
            )
        ]

    async def get_financial_summary(self, context: UserContext, *, start: date, end: date) -> FinancialSummary:
        # This endpoint reports MAGNITUDES: both totals are positive regardless
        # of the stored sign (see docs/handover-backend.md 5.10). Summing raw
        # amounts here produced a negative income total and, through
        # calculate_savings_rate, an impossible 160% savings rate.
        # Locals are not named income/expenses: those are the imported helpers.
        inflows = await self.get_income(context, start=start, end=end)
        outflows = await self.get_expenses(context, start=start, end=end)
        total_income = sum((income_amount(t) for t in inflows), Decimal("0"))
        total_expenses = sum((expense_amount(t) for t in outflows), Decimal("0"))
        return FinancialSummary(
            owner_user_id=context.user_id,
            period_start=start,
            period_end=end,
            total_income=Money(amount=total_income, currency=context.currency),
            total_expenses=Money(amount=total_expenses, currency=context.currency),
            net_cash_flow=Money(amount=total_income - total_expenses, currency=context.currency),
        )

    async def get_financial_score(self, context: UserContext) -> FinancialScore | None:
        rng = _seeded_rng(context.user_id)
        return FinancialScore(
            owner_user_id=context.user_id,
            score=rng.randrange(550, 800),
            score_range_min=300,
            score_range_max=850,
            factors=["Payment history", "Credit utilization", "Savings rate"],
            as_of_date=date.today(),
        )

    async def get_allo_score(self, context: UserContext) -> AlloScore | None:
        rng = _seeded_rng(context.user_id)
        today = date.today()
        return AlloScore(
            owner_user_id=context.user_id,
            overall_score=rng.randrange(40, 90),
            overall_tier="Moderate",
            overall_insight="Your finances show a mix of strengths and areas to improve.",
            flow_score=rng.randrange(40, 90),
            flow_tier="Moderate",
            flow_insight="Your day-to-day cash flow is reasonably balanced.",
            stability_score=rng.randrange(40, 90),
            stability_tier="Moderate",
            stability_insight="Your financial safety net has room to grow.",
            retirement_score=rng.randrange(40, 90),
            retirement_tier="Moderate",
            retirement_insight="Your long-term retirement readiness is on track but not secure.",
            period_start=date(today.year - 1, today.month, 1),
            period_end=today,
        )

    async def get_financial_score_history(self, context: UserContext) -> list[FinancialScore]:
        rng = _seeded_rng(context.user_id)
        today = date.today()
        current = rng.randrange(550, 800)
        return [
            FinancialScore(
                owner_user_id=context.user_id,
                score=current,
                score_range_min=300,
                score_range_max=850,
                factors=["Payment history", "Credit utilization"],
                as_of_date=today,
            ),
            FinancialScore(
                owner_user_id=context.user_id,
                score=current - rng.randrange(-20, 20),
                score_range_min=300,
                score_range_max=850,
                factors=["Payment history", "Credit utilization"],
                as_of_date=today - timedelta(days=90),
            ),
        ]

    async def get_allo_score_history(self, context: UserContext) -> list[AlloScore]:
        current = await self.get_allo_score(context)
        rng = _seeded_rng(context.user_id)
        previous = current.model_copy(
            update={
                "overall_score": max(0, current.overall_score - rng.randrange(-10, 10)),
                "period_end": current.period_end - timedelta(days=90),
                "period_start": current.period_start - timedelta(days=90),
            }
        )
        return [current, previous]

    async def get_liability_history(self, context: UserContext) -> list[LiabilitySnapshot]:
        rng = _seeded_rng(context.user_id)
        today = date.today()
        current_total = Decimal(rng.randrange(0, 4000))
        return [
            LiabilitySnapshot(
                owner_user_id=context.user_id, account_id="all", account_type="total",
                balance=Money(amount=current_total, currency=context.currency),
                snapshot_date=today - timedelta(days=30),
            ),
            LiabilitySnapshot(
                owner_user_id=context.user_id, account_id="all", account_type="total",
                balance=Money(amount=current_total + Decimal(rng.randrange(-500, 500)), currency=context.currency),
                snapshot_date=today - timedelta(days=90),
            ),
        ]

    async def get_retirement_readiness(self, context: UserContext) -> RetirementReadiness | None:
        rng = _seeded_rng(context.user_id)
        age = rng.randrange(25, 55)
        target_age = age + rng.randrange(10, 35)
        return RetirementReadiness(
            owner_user_id=context.user_id,
            age=age,
            target_retirement_age=target_age,
            years_remaining=target_age - age,
            retirement_target=Money(amount=Decimal(rng.randrange(200000, 1000000)), currency=context.currency),
            future_assets=Money(amount=Decimal(rng.randrange(50000, 800000)), currency=context.currency),
            monthly_retirement_income_goal=Money(amount=Decimal(rng.randrange(1000, 6000)), currency=context.currency),
            annual_contribution=Money(amount=Decimal(rng.randrange(0, 15000)), currency=context.currency),
            savings_rate=Decimal(rng.randrange(-10, 20)) / Decimal(100),
            investment_rate=Decimal(rng.randrange(0, 15)) / Decimal(100),
            readiness_ratio=Decimal(rng.randrange(-50, 100)) / Decimal(100),
        )

    async def get_savings_goal(self, context: UserContext) -> SavingsGoal | None:
        rng = _seeded_rng(context.user_id)
        today = date.today()
        target = Decimal(rng.randrange(2000, 20000))
        return SavingsGoal(
            owner_user_id=context.user_id,
            goal_id=f"{context.user_id}-goal-1",
            target_amount=Money(amount=target, currency=context.currency),
            monthly_target=Money(amount=(target / 12).quantize(Decimal("1")), currency=context.currency),
            start_date=date(today.year, 1, 1),
            end_date=date(today.year, 12, 31),
        )
