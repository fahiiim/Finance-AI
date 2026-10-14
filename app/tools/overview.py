"""
Broad recap: "catch me up", "summarize my finances", "why isn't my net worth
growing?".

Each section is fetched by the same tool that answers that topic on its own,
concurrently, and any section that fails or has no data is reported as
unavailable instead of failing the recap -- a one-minute summary that only
covered income and spending was the complaint.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from app.core.logging import get_logger, log_with_fields
from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.tools.allo_score import get_allo_score_tool
from app.tools.financial_scores import get_financial_score_tool
from app.tools.investments import get_investments_tool
from app.tools.liabilities import get_liabilities_tool
from app.tools.net_worth import get_net_worth_tool
from app.utils.dates import current_date_in, month_bounds
from app.utils.money import quantize

logger = get_logger(__name__)


async def _guard(name: str, coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - one failed section must not sink the recap
        log_with_fields(logger, 30, "overview_section_unavailable", section=name, error_type=type(exc).__name__)
        return {"availability": DataAvailability.MISSING.value, "note": "This section could not be loaded."}


async def _cash_flow(fs: FinancialService, ctx: UserContext, start: date, end: date) -> dict:
    summary = await fs.get_financial_summary(ctx, start=start, end=end)
    income = summary.total_income.amount if summary.total_income else Decimal("0")
    spend = summary.total_expenses.amount if summary.total_expenses else Decimal("0")
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "income": str(quantize(income)),
        "expenses": str(quantize(spend)),
        "net": str(quantize(income - spend)),
        "has_data": bool(income or spend),
    }


async def _bills(fs: FinancialService, ctx: UserContext, today: date) -> dict:
    bills = [b for b in await fs.get_bills(ctx, start=today, end=today + timedelta(days=14)) if not b.is_paid]
    return {
        "count": len(bills),
        "total": str(quantize(sum((b.amount.amount for b in bills), Decimal("0")))),
        "next_due": [{"name": b.name, "amount": str(b.amount.amount), "due": b.due_date.isoformat()} for b in bills[:5]],
    }


def _trim_score(result: dict, keys: tuple[str, ...]) -> dict:
    if result.get("availability") != DataAvailability.AVAILABLE.value:
        return {"availability": result.get("availability", DataAvailability.MISSING.value)}
    return {"availability": DataAvailability.AVAILABLE.value, **{k: result[k] for k in keys if k in result}}


async def get_overview_tool(financial_service: FinancialService, context: UserContext, plan: ExecutionPlan) -> dict:
    today = current_date_in(context.timezone)
    first_this_month = date(today.year, today.month, 1)
    last_month_start = date(first_this_month.year - 1, 12, 1) if first_this_month.month == 1 else date(
        first_this_month.year, first_this_month.month - 1, 1
    )
    _, last_month_end = month_bounds(last_month_start.year, last_month_start.month)

    (this_month, last_month, net_worth, debt, bills, credit, allo, investments) = await asyncio.gather(
        _guard("cash_flow_this_month", _cash_flow(financial_service, context, first_this_month, today)),
        _guard("cash_flow_last_month", _cash_flow(financial_service, context, last_month_start, last_month_end)),
        _guard("net_worth", get_net_worth_tool(financial_service, context, plan)),
        _guard("debt", get_liabilities_tool(financial_service, context, plan)),
        _guard("bills", _bills(financial_service, context, today)),
        _guard("credit_score", get_financial_score_tool(financial_service, context, plan)),
        _guard("allo_score", get_allo_score_tool(financial_service, context, plan)),
        _guard("investments", get_investments_tool(financial_service, context, plan)),
    )

    return {
        "as_of": today.isoformat(),
        "cash_flow_this_month_so_far": this_month,
        "cash_flow_last_complete_month": last_month,
        "net_worth": net_worth,
        "debt": {
            k: v
            for k, v in debt.items()
            if k in ("availability", "total_balance", "liability_count", "trend", "note")
        },
        "bills_due_next_14_days": bills,
        "credit_score": _trim_score(credit, ("score", "range", "as_of_date", "previous", "change")),
        "allo_score": _trim_score(allo, ("overall", "period_end", "previous", "change")),
        "investments": {
            k: v
            for k, v in investments.items()
            if k in ("availability", "total_market_value", "holding_count", "total_unrealized_gain_loss", "note")
        },
        "currency": context.currency,
        "note": (
            "Cover the most material points across ALL sections that have data, in priority order, and name "
            "any section that is unavailable instead of skipping it silently. For a question about why net "
            "worth is not growing: net worth history is not stored (only today's balances), so explain from "
            "what IS here -- cash flow, the debt trend, and score changes -- and say plainly that asset-value "
            "history is not recorded. Do not invent a cause."
        ),
    }
