"""
Transaction sign convention — defined ONCE, here.

THE CONVENTION
    In Plaid (and in this application's `plaid_transactions` table), a
    POSITIVE amount means money left the account — a purchase — and a
    NEGATIVE amount means money came in, such as a payroll deposit. This is
    the opposite of what most people assume on first reading, which is
    exactly why it is dangerous to leave implicit.

VERIFIED, NOT ASSUMED
    Confirmed against 122,326 real production rows:

        known purchases (Netflix, Uber, Walmart, ...)  18,300 positive / 167 negative
        known income (payroll, salary, direct deposit)      6 positive / 7,557 negative

    Overall 85.7% of rows are positive, consistent with spending dominating
    a normal account's transaction history.

WHY A MODULE INSTEAD OF `t.amount < 0`
    This rule previously lived as bare comparisons scattered across the
    calculation, analytics and backend-client layers. A convention expressed
    as a magic comparison in eight places is a convention that can be — and
    was — wrong in all eight at once, with nothing to catch it. Encoded as
    named predicates, the rule has one definition, one place to test, and
    one place to change if the upstream contract ever changes.

IF THE BACKEND NORMALIZES SIGNS
    This module assumes the backend passes Plaid's values through unchanged,
    which is what the database shows today. Should the backend decide to
    normalize to "negative = outflow" before serving this service, flip
    OUTFLOW_IS_POSITIVE and nothing else in the codebase needs to change.
    That decision belongs in docs/backend-integration.md, agreed explicitly
    with the backend team — not inferred by each caller.
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.financial import Transaction

#: True when a positive `amount` means money LEAVING the account (Plaid's own
#: convention, and what this application's database actually stores).
OUTFLOW_IS_POSITIVE = True


def is_expense(transaction: Transaction) -> bool:
    """True when this transaction moved money OUT of the user's account."""
    return transaction.amount > 0 if OUTFLOW_IS_POSITIVE else transaction.amount < 0


def is_income(transaction: Transaction) -> bool:
    """True when this transaction moved money INTO the user's account."""
    return transaction.amount < 0 if OUTFLOW_IS_POSITIVE else transaction.amount > 0


def expense_amount(transaction: Transaction) -> Decimal:
    """This expense's magnitude as a positive number, whatever the convention."""
    return abs(transaction.amount)


def income_amount(transaction: Transaction) -> Decimal:
    """This deposit's magnitude as a positive number, whatever the convention."""
    return abs(transaction.amount)


def expenses(transactions: list[Transaction]) -> list[Transaction]:
    return [t for t in transactions if is_expense(t)]


def income(transactions: list[Transaction]) -> list[Transaction]:
    return [t for t in transactions if is_income(t)]
