from __future__ import annotations

from decimal import Decimal

from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.liquidity import LIQUID_ACCOUNT_TYPES
from app.utils.money import require_same_currency

# What a credit-type account's balance MEANS: money owed, not money held.
_DEBT_ACCOUNT_TYPES = frozenset({"credit", "loan"})


def _kind(account_type: str | None) -> str:
    lowered = (account_type or "").lower()
    if lowered in LIQUID_ACCOUNT_TYPES:
        return "cash"
    if lowered in _DEBT_ACCOUNT_TYPES:
        return "debt"
    return "other"


async def get_accounts_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    """
    Balances, split by what they are.

    This used to return ONE `total_balance` summing every account. On a real
    user with $66,420 in checking and $51,217 owed on two cards, that read
    "you have $117,957" -- the debt added to the cash. A credit account's
    balance is what is owed, so the three kinds are totalled separately and
    only `total_cash` answers "how much money do I have".
    """
    accounts = await financial_service.get_accounts(context)
    require_same_currency([a.balance.currency for a in accounts])

    totals = {"cash": Decimal("0"), "debt": Decimal("0"), "other": Decimal("0")}
    for a in accounts:
        totals[_kind(a.type)] += a.balance.amount

    result: dict = {
        "accounts": [
            {
                "name": a.name,
                "type": a.type,
                "kind": _kind(a.type),
                "balance": str(a.balance.amount),
                "currency": a.balance.currency,
            }
            for a in accounts
        ],
        "account_count": len(accounts),
        "total_cash": str(totals["cash"]),
        "total_owed_on_credit_accounts": str(totals["debt"]),
        "other_accounts_total": str(totals["other"]),
        "currency": context.currency,
        "note": (
            "total_cash is the money in checking and savings -- the answer to 'how much money do I "
            "have'. Credit-account balances are money OWED, never added to cash. 'other' accounts "
            "(manually tracked assets, brokerage) are reported separately and are not spendable cash."
        ),
    }
    return result
