"""
Read-only SQLite backend client, over a local copy of the MyFi database.

Purpose: demos and validation against real data. The mock client invents
plausible numbers, which is right for tests and wrong for a client demo --
a figure on screen should be one that is actually in the database. Build the
file with `scripts/build_demo_db.py`, then set BACKEND_INTEGRATION_MODE=sqlite.

This is NOT the production path. Production is `BackendAPIClient`, calling the
Laravel service (docs/handover-backend.md). What this class does do is prove
the schema mapping in that handover is right, against the real rows, before
the backend work starts.

Isolation: every statement filters on context.user_id, and every object is
stamped with owner_user_id so `verify_ownership` re-checks it at the boundary
(app/core/data_isolation.py). The connection is opened read-only so a bug
here cannot write to the demo database, let alone the real one.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.core.security import UserContext
from app.schemas.financial import (
    Account,
    Bill,
    BudgetCategory,
    FinancialScore,
    FinancialSummary,
    InvestmentHolding,
    Liability,
    Money,
    RecurringTransaction,
    Subscription,
    Transaction,
)
from app.services.backend_client import BackendClient
from app.utils.merchants import clean_merchant_name
from app.utils.transactions import expense_amount, expenses, income, income_amount

# Plaid data in this database is US-dollar; there is no currency column to
# read one from. Stated here rather than sprinkled through the queries so it
# is a single, findable assumption.
CURRENCY = "USD"

# Rows the app has FLAGGED FOR REVIEW as possible movements between a user's
# own accounts. Counting a genuine internal transfer as spending double-counts
# money that never left: a card payment appears as an expense on the checking
# side while the purchases it settles were already counted.
#
# THE FLAG ALONE IS NOT THE ANSWER, and treating it as one was a real bug.
# 2,252 rows in this dump carry it, but only 911 are categorised TRANSFER_IN
# or TRANSFER_OUT. The other 1,341 are ordinary purchases -- 438 of them
# FOOD_AND_DRINK. Dropping every flagged row understated one demo user's food
# spending by 45% ($1,139 reported against $2,084 actual) and made named
# merchants vanish outright: KFC, Little Caesars and Cheesecake Summerlin all
# returned "no transactions found" while sitting in the database.
#
# So the flag is a QUESTION and the category is the ANSWER: exclude a flagged
# row only when its category agrees that it is a transfer. The duplicate-row
# risk this was guarding against is handled by is_deleted -- each flagged
# purchase has a deleted twin -- and across 117,631 live rows only 22
# user/amount/date groups could double-count at all.
#
# Still the open question in docs/handover-backend.md: the backend team must
# confirm the rule against the Laravel implementation.
EXCLUDE_REVIEW_TRANSFERS = True

# Categories that corroborate the review flag. Kept as raw Plaid primary
# names -- the join reads plaid_categories.primary_name, not the prettified
# display_name, so a display relabelling upstream cannot quietly widen this.
TRANSFER_CATEGORY_NAMES = ("TRANSFER_IN", "TRANSFER_OUT")

_ACCOUNT_TYPES = {
    ("depository", "checking"): "checking",
    ("depository", "savings"): "savings",
    ("credit", "credit card"): "credit",
}


def _pretty_category(raw: str | None) -> str | None:
    """FOOD_AND_DRINK -> Food And Drink. Plaid's constants are not for reading."""
    if not raw:
        return None
    return raw.replace("_", " ").title()


def _pretty_subcategory(detailed: str | None, primary: str | None) -> str | None:
    """
    FOOD_AND_DRINK_RESTAURANT under FOOD_AND_DRINK -> 'Restaurant'.

    This tier is what makes "how much did I spend on restaurants?" answerable
    at all. `display_name` collapses it -- every one of the seven FOOD_AND_DRINK
    rows in plaid_categories displays as "FOOD_AND_DRINK" -- so the detail was
    present in the database and thrown away before anything could use it.

    Returns None when the detailed name adds nothing beyond the primary, so a
    caller can tell "no finer label exists" from "the finer label is X".
    """
    if not detailed:
        return None
    trimmed = detailed
    if primary and detailed.startswith(primary):
        trimmed = detailed[len(primary) :]
    trimmed = trimmed.strip("_")
    if not trimmed:
        return None
    return trimmed.replace("_", " ").title()


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _money(value: float | int | None) -> Money:
    return Money(amount=Decimal(str(value or 0)), currency=CURRENCY)


class SqliteBackendClient(BackendClient):
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        if not self._path.exists():
            raise FileNotFoundError(
                f"Demo database not found at {self._path}. "
                "Build it with: python scripts/build_demo_db.py <dump.sql>"
            )
        # Read-only at the URI level, not by convention: this process must not
        # be able to modify the data it is demonstrating.
        self._uri = f"file:{self._path.as_posix()}?mode=ro"

    def _query(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self._uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    async def _run(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        # SQLite is synchronous; keep it off the event loop so one slow query
        # cannot stall every other request the service is serving.
        return await asyncio.to_thread(self._query, sql, params)

    # ---------------------------------------------------------------- profile

    async def get_user_profile(self, context: UserContext) -> dict:
        rows = await self._run(
            "SELECT id, Name, email, country FROM application_users WHERE id = ?",
            (context.user_id,),
        )
        row = rows[0] if rows else None
        return {
            "user_id": context.user_id,
            "name": row["Name"] if row else None,
            "currency": context.currency,
            "timezone": context.timezone,
        }

    # --------------------------------------------------------------- accounts

    async def get_accounts(self, context: UserContext) -> list[Account]:
        rows = await self._run(
            "SELECT account_id, name, current_balance, avaliable_balance, type, subtype "
            "FROM plaid_accounts WHERE user_id = ?",
            (context.user_id,),
        )
        return [
            Account(
                owner_user_id=context.user_id,
                account_id=r["account_id"] or "",
                name=r["name"] or "Account",
                type=_ACCOUNT_TYPES.get((r["type"], r["subtype"]), r["subtype"] or r["type"] or "other"),
                balance=_money(r["current_balance"]),
            )
            for r in rows
            if r["account_id"]
        ]

    # ----------------------------------------------------------- transactions

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
        sql = [
            "SELECT t.id, t.is_plaid, t.transaction_id, t.account_id, t.name, t.amount,",
            "       t.transaction_date, t.pending, k.display_name AS category,",
            "       k.primary_name, k.detailed_name",
            "FROM plaid_transactions t",
            "LEFT JOIN plaid_categories k ON k.id = t.plaid_category_id",
            # is_deleted is not optional: deleted rows would be counted in
            # every total. The handover requires the same of the Laravel
            # implementation.
            "WHERE t.user_id = ? AND t.is_deleted = 0",
        ]
        params: list = [context.user_id]

        if EXCLUDE_REVIEW_TRANSFERS:
            # Flagged AND categorised as a transfer -- see the constant above.
            # `k.primary_name IS NULL` keeps flagged rows with no category at
            # all: an uncategorised purchase is still a purchase.
            placeholders = ", ".join("?" for _ in TRANSFER_CATEGORY_NAMES)
            sql.append(
                f"AND NOT (t.is_review_transfer = 1 AND k.primary_name IN ({placeholders}))"
            )
            params.extend(TRANSFER_CATEGORY_NAMES)
        if start:
            sql.append("AND t.transaction_date >= ?")
            params.append(start.isoformat())
        if end:
            sql.append("AND t.transaction_date <= ?")
            params.append(end.isoformat())
        if merchant:
            sql.append("AND LOWER(t.name) LIKE ?")
            params.append(f"%{merchant.lower()}%")
        if min_amount is not None:
            sql.append("AND ABS(t.amount) >= ?")
            params.append(min_amount)
        if max_amount is not None:
            sql.append("AND ABS(t.amount) <= ?")
            params.append(max_amount)

        sql.append("ORDER BY t.transaction_date DESC")
        rows = await self._run(" ".join(sql), tuple(params))

        transactions = [
            Transaction(
                owner_user_id=context.user_id,
                # A manually-entered transaction has no Plaid id. Dropping
                # those rows made the assistant's totals disagree with the
                # database by exactly the amounts a user had typed in -- money
                # that is every bit as real as the imported kind. The primary
                # key identifies them instead.
                transaction_id=r["transaction_id"] or f"manual-{r['id']}",
                account_id=r["account_id"] or "manual",
                amount=Decimal(str(r["amount"] or 0)),
                currency=CURRENCY,
                date=_to_date(r["transaction_date"]) or date.today(),
                merchant_name=r["name"],
                category=_pretty_category(r["category"]),
                subcategory=_pretty_subcategory(r["detailed_name"], r["primary_name"]),
                pending=str(r["pending"]) == "1",
            )
            for r in rows
            if r["transaction_date"]
        ]

        # Category is matched after prettifying, so the filter compares what
        # the user was shown ("Food And Drink") and not Plaid's raw constant.
        if category:
            wanted = category.lower()
            transactions = [t for t in transactions if (t.category or "").lower() == wanted]
        return transactions

    async def get_income(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        return income(await self.get_transactions(context, start=start, end=end))

    async def get_expenses(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        return expenses(await self.get_transactions(context, start=start, end=end))

    # ----------------------------------------------------- recurring / bills

    async def _streams(self, context: UserContext) -> list[sqlite3.Row]:
        return await self._run(
            "SELECT s.stream_id, s.description, s.amount, s.frequency, s.next_payment_date, "
            "       s.last_payment_date, s.active, k.display_name AS category "
            "FROM plaid_recurring_streams s "
            "LEFT JOIN plaid_categories k ON k.id = s.plaid_category_id "
            "WHERE s.user_id = ? AND s.active = 1",
            (context.user_id,),
        )

    async def get_bills(
        self, context: UserContext, *, start: date | None = None, end: date | None = None
    ) -> list[Bill]:
        # This database has no bills table. The nearest true thing is a
        # recurring outflow with a known next payment date. Inflows are
        # excluded: a recurring paycheck is not a bill.
        bills: list[Bill] = []
        for r in await self._streams(context):
            due = _to_date(r["next_payment_date"])
            amount = Decimal(str(r["amount"] or 0))
            if due is None or amount <= 0:
                continue
            if start and due < start:
                continue
            if end and due > end:
                continue
            bills.append(
                Bill(
                    owner_user_id=context.user_id,
                    bill_id=r["stream_id"] or "",
                    name=clean_merchant_name(r["description"]) if r["description"] else "Recurring payment",
                    amount=Money(amount=amount, currency=CURRENCY),
                    due_date=due,
                    is_paid=False,
                )
            )
        return bills

    async def get_subscriptions(self, context: UserContext) -> list[Subscription]:
        # NB: the `subscriptions` table in this schema is the app's OWN billing
        # (plan_id, trial dates), not the user's Netflix. A user-facing
        # subscription is a recurring outflow on a fixed monthly cycle.
        subs: list[Subscription] = []
        for r in await self._streams(context):
            amount = Decimal(str(r["amount"] or 0))
            frequency = (r["frequency"] or "").lower()
            if amount <= 0 or "month" not in frequency:
                continue
            subs.append(
                Subscription(
                    owner_user_id=context.user_id,
                    subscription_id=r["stream_id"] or "",
                    name=clean_merchant_name(r["description"]) if r["description"] else "Subscription",
                    amount=Money(amount=amount, currency=CURRENCY),
                    billing_cycle="monthly",
                    next_charge_date=_to_date(r["next_payment_date"]),
                )
            )
        return subs

    async def get_recurring_transactions(self, context: UserContext) -> list[RecurringTransaction]:
        out: list[RecurringTransaction] = []
        for r in await self._streams(context):
            amount = Decimal(str(r["amount"] or 0))
            if amount <= 0:
                continue
            out.append(
                RecurringTransaction(
                    owner_user_id=context.user_id,
                    recurring_id=r["stream_id"] or "",
                    name=clean_merchant_name(r["description"]) if r["description"] else "Recurring payment",
                    amount=Money(amount=amount, currency=CURRENCY),
                    frequency=(r["frequency"] or "unknown").lower(),
                    category=_pretty_category(r["category"]),
                    next_expected_date=_to_date(r["next_payment_date"]),
                )
            )
        return out

    # ---------------------------------------------------- budgets / holdings

    async def get_budgets(
        self, context: UserContext, *, period_start: date, period_end: date
    ) -> list[BudgetCategory]:
        # There is no budgets table in this schema. An empty list makes the
        # assistant say budgets are unavailable, which is true. Deriving a
        # "budget" from past spending would put an invented target on screen
        # and attribute it to the user.
        return []

    async def get_investments(self, context: UserContext) -> list[InvestmentHolding]:
        # No holdings table: stock_watchlists is a watchlist, not a portfolio.
        return []

    async def get_liabilities(self, context: UserContext) -> list[Liability]:
        rows = await self._run(
            "SELECT account_id, name, current_balance, subtype FROM plaid_accounts "
            "WHERE user_id = ? AND type = 'credit'",
            (context.user_id,),
        )
        return [
            Liability(
                owner_user_id=context.user_id,
                liability_id=r["account_id"] or "",
                name=r["name"] or "Credit card",
                type="credit_card",
                balance=_money(r["current_balance"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------ summary / score

    async def get_financial_summary(
        self, context: UserContext, *, start: date, end: date
    ) -> FinancialSummary:
        transactions = await self.get_transactions(context, start=start, end=end)
        total_income = sum((income_amount(t) for t in income(transactions)), Decimal("0"))
        total_expenses = sum((expense_amount(t) for t in expenses(transactions)), Decimal("0"))
        return FinancialSummary(
            owner_user_id=context.user_id,
            period_start=start,
            period_end=end,
            total_income=Money(amount=total_income, currency=CURRENCY),
            total_expenses=Money(amount=total_expenses, currency=CURRENCY),
            net_cash_flow=Money(amount=total_income - total_expenses, currency=CURRENCY),
        )

    async def get_financial_score(self, context: UserContext) -> FinancialScore | None:
        rows = await self._run(
            "SELECT score, report_date, provider, score_model FROM credit_scores "
            "WHERE user_id = ? AND score IS NOT NULL ORDER BY report_date DESC LIMIT 1",
            (context.user_id,),
        )
        if not rows or rows[0]["score"] is None:
            return None
        r = rows[0]
        return FinancialScore(
            owner_user_id=context.user_id,
            score=int(r["score"]),
            score_range_min=300,
            score_range_max=850,
            factors=[f for f in (r["provider"], r["score_model"]) if f],
            as_of_date=_to_date(r["report_date"]),
        )
