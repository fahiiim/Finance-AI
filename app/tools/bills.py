from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.dates import current_date_in, month_bounds


def _forward_looking_end(start: date | None, end: date | None, label: str | None, today: date) -> date | None:
    """
    Reach a bills window forward to the end of the period it names.

    The date resolver clips "this month" and "this week" at TODAY -- right
    for spending, which has not happened yet beyond that, and wrong for bills,
    whose whole point is what is still due. Left alone, "how many bills do I
    have remaining this month?" was answered over 1-13 October on the 13th: the
    19 bills already paid, none of the upcoming ones, and a confident "you have
    no bills remaining".

    Only a window that ends exactly today and names a current month/week is
    extended; an explicit range, a past month, or "today" is left as asked.
    """
    if start is None or end is None or end != today:
        return end

    is_this_month = label == "this_month" or (
        label is not None and label == f"{today.year}-{today.month:02d}"
    )
    if is_this_month and start == date(today.year, today.month, 1):
        return month_bounds(today.year, today.month)[1]

    if label == "this_week":
        return today + timedelta(days=6 - today.weekday())  # through Sunday
    return end


async def get_bills_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    start = plan.date_range.start if plan.date_range else None
    end = plan.date_range.end if plan.date_range else None
    label = plan.date_range.label if plan.date_range else None

    today = current_date_in(context.timezone)
    effective_end = _forward_looking_end(start, end, label, today)
    extended = effective_end != end
    end = effective_end

    bills = await financial_service.get_bills(context, start=start, end=end)
    result = {
        "bills": [
            {
                "name": b.name,
                "amount": str(b.amount.amount),
                "due_date": b.due_date.isoformat(),
                "is_paid": b.is_paid,
            }
            for b in bills
        ],
        "bill_count": len(bills),
        "unpaid_count": sum(1 for b in bills if not b.is_paid),
        # Totals are computed here. Left to the model, summing 32 amounts gave
        # three different answers across three runs ($25,330 / $27,226 /
        # $28,328 against a true $28,434.49).
        "unpaid_total": str(sum((b.amount.amount for b in bills if not b.is_paid), Decimal("0"))),
        "paid_total": str(sum((b.amount.amount for b in bills if b.is_paid), Decimal("0"))),
        "total": str(sum((b.amount.amount for b in bills), Decimal("0"))),
        "largest_bill": (
            {"name": max(bills, key=lambda b: b.amount.amount).name,
             "amount": str(max(bills, key=lambda b: b.amount.amount).amount.amount),
             "due_date": max(bills, key=lambda b: b.amount.amount).due_date.isoformat()}
            if bills else None
        ),
        "currency": context.currency,
    }
    if start is not None and end is not None:
        result["period"] = {"start": start.isoformat(), "end": end.isoformat()}
    if extended and end is not None:
        result["period_note"] = (
            f"Bills look ahead: this window runs through {end.isoformat()} so that bills still due "
            "this period are included. 'Remaining' means unpaid (is_paid=false)."
        )

    # Some sources only see part of the calendar (the live backend serves the
    # current month). An empty list for a window outside that is NOT "nothing
    # is due" -- say the period is unavailable, so the model cannot turn a
    # data gap into reassurance.
    coverage = financial_service.bills_coverage(context)
    if coverage is not None:
        covered_start, covered_end = coverage
        result["coverage"] = {"start": covered_start.isoformat(), "end": covered_end.isoformat()}
        outside = (start is not None and start < covered_start) or (end is not None and end > covered_end)
        if outside:
            result["note"] = (
                f"Bill data is only available for {covered_start.isoformat()} to "
                f"{covered_end.isoformat()}. The requested period reaches outside that, so the list "
                "above is incomplete for it. Say that bills outside this month are not available -- "
                "never say there are none."
            )
    return result
