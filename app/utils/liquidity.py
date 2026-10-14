"""
What counts as spendable cash.

Checking and savings are liquid. Credit cards are debt, and brokerage,
crypto and loan accounts are not cash you can spend this week -- counting
them is how earlier answers either overstated cash or, by using a single
account, understated it ("the $3,346 figure is only one account balance").
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.financial import Account

# Categories that only ever describe money coming IN. An expense-signed row
# in one of these is almost certainly a deposit keyed in backwards, so
# ESTIMATES (typical spending, trimmable spending) leave it out. Reported
# totals are never adjusted for it.
INFLOW_CATEGORIES = frozenset(
    {"income", "tax refunds", "tax refund", "transfer in", "dividends", "interest income", "interest earned"}
)

LIQUID_ACCOUNT_TYPES =frozenset({"checking", "savings", "cash", "cash management", "money market"})


def liquid_accounts(accounts: list[Account]) -> list[Account]:
    return [a for a in accounts if (a.type or "").lower() in LIQUID_ACCOUNT_TYPES]


def liquid_cash_total(accounts: list[Account]) -> Decimal:
    return sum((a.balance.amount for a in liquid_accounts(accounts)), Decimal("0"))
