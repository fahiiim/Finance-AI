"""
Cash position tool: everything that depends on "how much spendable cash do I
really have, and what is already spoken for".

One deterministic computation answers safe-to-spend, financial cushion,
emergency-fund coverage, runway in days, cash left after bills, month-end
sufficiency and guilt-free spending. Every figure starts from TOTAL liquid
cash across all checking and savings accounts (app/utils/liquidity.py), never
one account, and every assumption (the buffer, the horizon, what counts as
essential) is returned alongside the number so the answer can state it.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import date, timedelta
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.dates import current_date_in, month_bounds
from app.utils.liquidity import INFLOW_CATEGORIES, liquid_accounts, liquid_cash_total
from app.utils.money import quantize
from app.utils.transactions import expense_amount, expenses, income_amount
from app.utils.transactions import income as income_of

HISTORY_MONTHS = 3
AVG_DAYS_PER_MONTH = Decimal("30.44")
# One week of typical spending is held back before calling money "safe".
BUFFER_DAYS = 7
# Plaid categories (as shown to users) that are hard to skip. An ASSUMPTION,
# returned in the result -- there is no essential/discretionary flag in the data.
ESSENTIAL_CATEGORIES = frozenset({"Rent And Utilities", "Loan Payments", "Medical", "Transportation"})
EMERGENCY_FUND_TARGET_MONTHS = (3, 6)


def _months_before(first_of_month: date, months: int) -> list[date]:
    starts: list[date] = []
    cursor = first_of_month
    for _ in range(months):
        cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
        starts.append(cursor)
    return list(reversed(starts))


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _per_day(monthly: Decimal | None) -> Decimal | None:
    return quantize(monthly / AVG_DAYS_PER_MONTH) if monthly is not None else None


def _ratio(numerator: Decimal, denominator: Decimal | None, places: str = "0.1") -> str | None:
    if not denominator or denominator <= 0:
        return None
    return str((numerator / denominator).quantize(Decimal(places)))


async def get_cash_position_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    accounts = await financial_service.get_accounts(context)
    liquid = liquid_accounts(accounts)
    if not liquid:
        return {
            "availability": DataAvailability.MISSING.value,
            "note": (
                "No checking or savings accounts are linked, so spendable cash is UNKNOWN -- not zero. "
                "Say no connected cash account was found; do not give a safe-to-spend figure."
            ),
        }

    today = current_date_in(context.timezone)
    liquid_total = liquid_cash_total(accounts)
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    days_left = (month_end - today).days

    # The four-week horizon crosses month end, which costs the backend one
    # lookup per day beyond it (~4 s each). It does not depend on anything
    # below, so it runs while the history is being fetched instead of after it.
    horizon_task = asyncio.ensure_future(
        financial_service.get_bills(context, start=today, end=today + timedelta(days=27))
    )

    # Retrieved even if never awaited (a user with no spending history skips
    # the horizon), so an unused failure cannot surface as a stray warning.
    horizon_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    bills_month = [b for b in await financial_service.get_bills(context, start=today, end=month_end) if not b.is_paid]
    bills_14 = [b for b in bills_month if b.due_date <= today + timedelta(days=14)]
    bills_to_month_end = sum((b.amount.amount for b in bills_month), Decimal("0"))
    bills_14_total = sum((b.amount.amount for b in bills_14), Decimal("0"))

    first_this_month = date(today.year, today.month, 1)
    starts = _months_before(first_this_month, HISTORY_MONTHS)

    window_start = starts[0]
    _, window_end = month_bounds(starts[-1].year, starts[-1].month)
    history_txns = None
    try:
        history_txns = await financial_service.get_transactions(context, start=window_start, end=window_end)
    except Exception:  # noqa: BLE001 - fall back to per-month summaries below
        history_txns = None

    complete = []
    if history_txns is not None:
        # One fetch serves every month: three separate summaries each re-read
        # the same transactions (and the essential-spend estimate below read
        # them a fourth time), which on a slow backend was most of the wait.
        for start in starts:
            _, end = month_bounds(start.year, start.month)
            month_txns = [t for t in history_txns if start <= t.date <= end]
            income = sum((income_amount(t) for t in income_of(month_txns)), Decimal("0"))
            spend = sum((expense_amount(t) for t in expenses(month_txns)), Decimal("0"))
            if income or spend:
                complete.append((income, spend))
    else:
        async def one_month(start: date):
            _, end = month_bounds(start.year, start.month)
            return await financial_service.get_financial_summary(context, start=start, end=end)

        summaries = await asyncio.gather(*(one_month(s) for s in starts), return_exceptions=True)
        for s in summaries:
            if isinstance(s, BaseException):
                continue
            income = s.total_income.amount if s.total_income else Decimal("0")
            spend = s.total_expenses.amount if s.total_expenses else Decimal("0")
            if income or spend:
                complete.append((income, spend))

    avg_monthly_expenses = (
        quantize(sum((e for _, e in complete), Decimal("0")) / len(complete)) if complete else None
    )
    avg_monthly_income = (
        quantize(sum((i for i, _ in complete), Decimal("0")) / len(complete)) if complete else None
    )

    # Typical spending and the essential share come from the same complete
    # months of transactions. Rows CATEGORISED as Income but stored with the
    # expense sign (a manually entered deposit keyed in backwards -- one
    # $500,000 test row turned a month of ~$7,000 into ~$500,000) cannot be
    # real spending, so they are left out of this ESTIMATE and the amount left
    # out is returned. Reported totals elsewhere are never adjusted.
    essential_monthly: Decimal | None = None
    excluded_income_labelled = Decimal("0")
    if complete:
        try:
            txns = history_txns
            if txns is None:
                txns = await financial_service.get_transactions(context, start=window_start, end=window_end)
            outflows = expenses(txns)
            excluded_income_labelled = sum(
                (expense_amount(t) for t in outflows if (t.category or "").lower() in INFLOW_CATEGORIES),
                Decimal("0"),
            )
            # MEDIAN of the complete months, not the mean. One typed-in
            # $100,000 expense in a three-month window made "typical" spending
            # $2,200 a day and a cash runway of 30 days; the middle month is
            # what a person means by "normal".
            by_month_spend: dict[tuple[int, int], Decimal] = {}
            by_month_essential: dict[tuple[int, int], Decimal] = {}
            for t in outflows:
                if (t.category or "").lower() in INFLOW_CATEGORIES:
                    continue
                key = (t.date.year, t.date.month)
                by_month_spend[key] = by_month_spend.get(key, Decimal("0")) + expense_amount(t)
                if t.category in ESSENTIAL_CATEGORIES:
                    by_month_essential[key] = by_month_essential.get(key, Decimal("0")) + expense_amount(t)
            data_months = {(t.date.year, t.date.month) for t in txns}
            keys = [(m.year, m.month) for m in starts if (m.year, m.month) in data_months]
            if keys:
                avg_monthly_expenses = quantize(_median([by_month_spend.get(k, Decimal("0")) for k in keys]))
                essential_monthly = quantize(_median([by_month_essential.get(k, Decimal("0")) for k in keys]))
        except Exception:  # best-effort: fall back to the summary-based average above
            essential_monthly = None
    avg_daily = _per_day(avg_monthly_expenses)

    buffer_amount = quantize(avg_daily * BUFFER_DAYS) if avg_daily is not None else Decimal("0")

    cash_after_bills = liquid_total - bills_to_month_end
    result: dict = {
        "availability": DataAvailability.AVAILABLE.value,
        "liquid_cash_total": str(quantize(liquid_total)),
        "liquid_accounts": [
            {"name": a.name, "type": a.type, "balance": str(a.balance.amount)} for a in liquid
        ],
        "as_of": today.isoformat(),
        "month_end": month_end.isoformat(),
        "days_left_in_month": days_left,
        "upcoming_unpaid_bills_next_14_days": str(quantize(bills_14_total)),
        "upcoming_unpaid_bills_to_month_end": str(quantize(bills_to_month_end)),
        "unpaid_bill_count_to_month_end": len(bills_month),
        "cash_after_bills_to_month_end": str(quantize(cash_after_bills)),
        "financial_cushion": str(quantize(liquid_total - bills_14_total - buffer_amount)),
        "safe_to_spend_to_month_end": str(quantize(cash_after_bills - buffer_amount)),
        "buffer_held_back": str(buffer_amount),
        "history_months_used": len(complete),
        "average_monthly_income": str(avg_monthly_income) if avg_monthly_income is not None else None,
        "average_monthly_expenses": str(avg_monthly_expenses) if avg_monthly_expenses is not None else None,
        "average_daily_spending": str(avg_daily) if avg_daily is not None else None,
        "essential_monthly_expenses_estimate": str(essential_monthly) if essential_monthly is not None else None,
        "essential_categories_assumed": sorted(ESSENTIAL_CATEGORIES),
        "excluded_from_typical_spending": (
            {
                "amount": str(quantize(excluded_income_labelled)),
                "reason": (
                    "expense-signed rows in inflow-only categories (Income, Tax Refunds, ...) -- "
                    "likely deposits entered with the wrong sign"
                ),
            }
            if excluded_income_labelled
            else None
        ),
        "currency": context.currency,
        "assumptions": (
            f"Spendable cash is the sum of ALL checking and savings accounts. A buffer of {BUFFER_DAYS} days of "
            f"typical spending is held back before money is called safe. Typical spending is the average of the "
            f"last {HISTORY_MONTHS} complete months with data. Month-end projections assume NO further income "
            "arrives -- say so. Which categories are essential is an assumption, not a field in the data."
        ),
    }

    if avg_daily:
        projected = liquid_total - bills_to_month_end - quantize(avg_daily * days_left)
        result["projected_cash_at_month_end_assuming_no_income"] = str(quantize(projected))
        result["runway_days_at_typical_spending"] = _ratio(liquid_total, avg_daily, "1")
    if essential_monthly:
        result["runway_days_essential_only"] = _ratio(liquid_total, _per_day(essential_monthly), "1")
        result["emergency_fund_months_essential_only"] = _ratio(liquid_total, essential_monthly)
    if avg_monthly_expenses:
        months = (liquid_total / avg_monthly_expenses).quantize(Decimal("0.1"))
        result["emergency_fund_months_all_spending"] = str(months)
        low, high = EMERGENCY_FUND_TARGET_MONTHS
        result["emergency_fund_status"] = (
            f"under_{low}_months" if months < low else f"{low}_to_{high}_months" if months < high else f"{high}_plus_months"
        )

    # Next four weeks, week by week: known unpaid bills plus a week of typical
    # spending. "Which week will be hardest?" is the one with the most going
    # out. Income is not projected (pay dates are not in the data), so this is
    # an outflow view and says so.
    if avg_daily is not None:
        horizon = [b for b in await horizon_task if not b.is_paid]
        weeks = []
        for i in range(4):
            w_start = today + timedelta(days=7 * i)
            w_end = w_start + timedelta(days=6)
            due = [b for b in horizon if w_start <= b.due_date <= w_end]
            bills_total = sum((b.amount.amount for b in due), Decimal("0"))
            typical = quantize(avg_daily * 7)
            weeks.append(
                {
                    "week_start": w_start.isoformat(),
                    "week_end": w_end.isoformat(),
                    "bills_due": str(quantize(bills_total)),
                    "bill_names": [b.name for b in due][:5],
                    "typical_spending": str(typical),
                    "expected_outflow": str(quantize(bills_total + typical)),
                }
            )
        result["next_four_weeks_outflow"] = weeks
        result["hardest_upcoming_week"] = max(weeks, key=lambda w: Decimal(str(w["expected_outflow"])))
        result["next_four_weeks_note"] = (
            "Outflow only: unpaid bills due each week plus typical spending. Income is not projected because "
            "pay dates are not in the data. If every week shows the same typical spending, the hardest week is "
            "simply the one with the most bills due -- say so."
        )

    # Debt context, so "should I use my savings to pay down debt?" is judged
    # against real balances and payments rather than cash alone.
    try:
        debts = await financial_service.get_liabilities(context)
        if debts:
            result["debt_context"] = {
                "total_debt": str(quantize(sum((d.balance.amount for d in debts), Decimal("0")))),
                "total_minimum_monthly_payments": str(
                    quantize(sum((d.minimum_payment.amount for d in debts if d.minimum_payment), Decimal("0")))
                ),
                "debt_count": len(debts),
            }
    except Exception:  # noqa: BLE001 - optional context
        pass

    if not complete:
        result["history_note"] = (
            "No complete month of transactions is on record, so typical spending, runway and coverage "
            "cannot be computed. Report the cash and bills figures and say the rest needs spending history."
        )
    return result
