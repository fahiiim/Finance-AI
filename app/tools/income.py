from __future__ import annotations

from decimal import Decimal

from app.core.exceptions import ValidationFailedError
from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.calculations import calculate_total_income
from app.utils.merchants import clean_merchant_name, merchant_key
from app.utils.money import quantize
from app.utils.transactions import income_amount

MAX_SOURCES_LISTED = 10

# Words a planner reaches for when the user asks about their regular pay. They
# describe WHICH source is meant, not a category or merchant to filter on --
# applied as a filter they match nothing and "how much did I earn from my main
# job?" came back "no income recorded".
_NOT_A_FILTER = frozenset({
    "main job", "job", "my job", "work", "salary", "paycheck", "paychecks", "pay", "wages", "wage",
    "employer", "income", "earnings", "payroll", "main income", "primary income", "main source",
})


async def get_income_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    if plan.date_range is None:
        raise ValidationFailedError("An income query requires a resolved date range.")

    transactions = await financial_service.get_income(
        context, start=plan.date_range.start, end=plan.date_range.end
    )
    # "How much dividend income?" is a filtered question. The plan carries the
    # filter, and ignoring it answered with the user's TOTAL income under the
    # label "dividends" ($351k reported for a user with none).
    needle = (plan.category_filter or plan.merchant_filter or "").strip().lower()
    if needle in _NOT_A_FILTER:
        needle = ""
    filter_note = None
    if needle:
        transactions = [
            t for t in transactions
            if needle in (t.category or "").lower()
            or needle in (t.subcategory or "").lower()
            or needle in (t.merchant_name or "").lower()
        ]
        if not transactions:
            filter_note = (
                f"No income transactions matching '{needle}' were found in this period. State that "
                "none are recorded -- do not substitute total income, and do not say it is $0 "
                "because the user earned none: it may simply not be recorded as a transaction."
            )
    total = calculate_total_income(transactions)

    # Grouped from the SAME list the total is summed from, so the breakdown
    # reconciles with the total exactly -- a source list that adds up to more
    # than the headline figure was an observed failure. Anything beyond the top
    # sources is folded into one "other" line instead of dropped.
    grouped: dict[str, dict] = {}
    for txn in transactions:
        display = clean_merchant_name(txn.merchant_name) or (txn.category or "Unlabelled income")
        entry = grouped.setdefault(merchant_key(display), {"name": display, "total": Decimal("0"), "count": 0})
        entry["total"] += income_amount(txn)
        entry["count"] += 1
    ranked = sorted(grouped.values(), key=lambda e: e["total"], reverse=True)

    listed = ranked[:MAX_SOURCES_LISTED]
    rest = ranked[MAX_SOURCES_LISTED:]
    sources = [
        {"source": e["name"], "total": str(quantize(e["total"])), "transactions": e["count"]} for e in listed
    ]
    if rest:
        sources.append(
            {
                "source": f"{len(rest)} other sources",
                "total": str(quantize(sum((e["total"] for e in rest), Decimal("0")))),
                "transactions": sum(e["count"] for e in rest),
            }
        )

    result: dict = {
        "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
        "total_income": str(total),
        "transaction_count": len(transactions),
        "by_source": sources,
        "currency": context.currency,
    }
    if needle:
        result["filter"] = needle
    if filter_note:
        result["note"] = filter_note
    if ranked:
        primary = ranked[0]
        result["primary_source"] = {
            "source": primary["name"],
            "total": str(quantize(primary["total"])),
            "share_of_income_percent": (
                str((primary["total"] / total * Decimal("100")).quantize(Decimal("0.1"))) if total else None
            ),
            "note": (
                "The largest single source in this period -- the best available stand-in for 'main job'. "
                "There is no employer field, so say it is the largest source, not a confirmed employer."
            ),
        }
    return result
