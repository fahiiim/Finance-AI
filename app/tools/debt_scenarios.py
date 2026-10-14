"""
Debt what-if tool: extra monthly payment, paying off the smallest debt, and
late or at-risk payments.

Interest rates are mostly missing from the data (credit-card APRs are empty
in this database), so payoff timing is computed WITHOUT interest and says so.
That makes every months-to-payoff figure an optimistic floor -- the tool
returns the assumption explicitly so the answer cannot present it as exact.
"""

from __future__ import annotations

import math
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.dates import current_date_in
from app.utils.liquidity import liquid_cash_total
from app.utils.money import quantize

DUE_SOON_DAYS = 7


def _months(balance: Decimal, monthly: Decimal) -> int | None:
    if monthly <= 0:
        return None
    return math.ceil(balance / monthly)


async def get_debt_scenario_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    liabilities = await financial_service.get_liabilities(context)
    if not liabilities:
        return {
            "availability": DataAvailability.MISSING.value,
            "note": (
                "No debts or credit accounts are on file. This means NOT CONNECTED, not debt-free; "
                "say the data is not available."
            ),
        }

    today = current_date_in(context.timezone)
    total_debt = sum((li.balance.amount for li in liabilities), Decimal("0"))
    with_payment = [li for li in liabilities if li.minimum_payment and li.minimum_payment.amount > 0]
    without_payment = [li for li in liabilities if li not in with_payment]
    total_minimum = sum((li.minimum_payment.amount for li in with_payment), Decimal("0"))

    result: dict = {
        "availability": DataAvailability.AVAILABLE.value,
        "total_debt": str(quantize(total_debt)),
        "total_minimum_monthly_payments": str(quantize(total_minimum)),
        "debts_without_a_known_minimum_payment": [li.name for li in without_payment],
        "currency": context.currency,
        "assumptions": (
            "Payoff timing IGNORES interest (rates are not on file for most debts), so it is an optimistic "
            "floor, not a schedule -- say so. Debts with no recorded minimum payment are left out of the "
            "payment math and named above."
        ),
    }

    # --- extra monthly payment -------------------------------------------------
    extra = plan.amount_in_question
    covered_balance = sum((li.balance.amount for li in with_payment), Decimal("0"))
    if extra is not None and with_payment:
        extra_amount = Decimal(str(extra))
        baseline = _months(covered_balance, total_minimum)
        with_extra = _months(covered_balance, total_minimum + extra_amount)
        result["extra_monthly_payment"] = {
            "extra": str(extra_amount),
            "balance_covered": str(quantize(covered_balance)),
            "months_at_minimums_only": baseline,
            "months_with_extra": with_extra,
            "months_saved": (baseline - with_extra) if baseline is not None and with_extra is not None else None,
        }

    # --- pay off the smallest debt ---------------------------------------------
    smallest = min((li for li in liabilities if li.balance.amount > 0), key=lambda li: li.balance.amount, default=None)
    if smallest is not None:
        accounts = await financial_service.get_accounts(context)
        cash = liquid_cash_total(accounts)
        freed = smallest.minimum_payment.amount if smallest.minimum_payment else None
        result["smallest_debt"] = {
            "name": smallest.name,
            "type": smallest.type,
            "balance": str(quantize(smallest.balance.amount)),
            "minimum_payment_freed_per_month": str(freed) if freed is not None else None,
            "liquid_cash_now": str(quantize(cash)),
            "liquid_cash_after_paying_it_off": str(quantize(cash - smallest.balance.amount)),
            "can_pay_from_cash_today": cash >= smallest.balance.amount,
            "total_debt_after": str(quantize(total_debt - smallest.balance.amount)),
            "debt_count_after": len(liabilities) - 1,
        }

    # --- late or at-risk payments ----------------------------------------------
    status = []
    for li in liabilities:
        if li.is_overdue is None and li.next_payment_due_date is None:
            continue
        days_to_due = (li.next_payment_due_date - today).days if li.next_payment_due_date else None
        status.append(
            {
                "name": li.name,
                "flagged_overdue": li.is_overdue,
                "next_payment_due_date": li.next_payment_due_date.isoformat() if li.next_payment_due_date else None,
                "days_until_due": days_to_due,
                "due_within_a_week": days_to_due is not None and 0 <= days_to_due <= DUE_SOON_DAYS,
                "due_date_already_passed": days_to_due is not None and days_to_due < 0,
                "last_payment_date": li.last_payment_date.isoformat() if li.last_payment_date else None,
                "minimum_payment": str(li.minimum_payment.amount) if li.minimum_payment else None,
            }
        )
    result["payment_status"] = status
    result["payment_status_note"] = (
        "flagged_overdue is the source's own flag. A due date that has already passed WITHOUT that flag "
        "usually means the due date has not been refreshed -- say it cannot be confirmed rather than "
        "calling the payment late. Debts missing from payment_status have no payment-status data."
    )
    return result
