"""
Financial Tool Router.

Maps each resolved Intent to the specific combination of typed tools /
deterministic calculations it needs. Handlers are explicit per intent
(rather than a generic "call whatever's in required_tools") because
different intents compose data + calculations differently — e.g.
PERIOD_COMPARISON needs two separate transaction fetches plus a comparison
calculation, which a generic loop can't express safely.

Every handler returns (result_dict, tools_used) so the caller can report
accurate tools_used metadata without guessing.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from app.core.exceptions import (
    BackendServiceError,
    BackendTimeoutError,
    CrossUserDataError,
    FinancialDataUnavailableError,
    ToolExecutionError,
    ValidationFailedError,
)
from app.core.logging import get_logger, log_with_fields
from app.core.security import UserContext
from app.schemas.planner import ExecutionPlan, Intent
from app.services.financial_service import FinancialService
from app.tools.accounts import get_accounts_tool
from app.tools.analytics import compare_periods, detect_spending_spike
from app.tools.bills import get_bills_tool
from app.tools.budgets import get_budgets_tool
from app.tools.calculations import (
    calculate_cash_flow,
    calculate_category_total,
    calculate_debt_ratio,
    calculate_percentage_change,
    calculate_savings_rate,
    calculate_total_spending,
)
from app.tools.expenses import get_expenses_tool
from app.tools.filters import apply_filters
from app.tools.financial_scores import get_financial_score_tool
from app.tools.forecasting import forecast_spending
from app.tools.income import get_income_tool
from app.tools.investments import get_investments_tool
from app.tools.liabilities import get_liabilities_tool
from app.tools.recurring import get_recurring_transactions_tool
from app.tools.subscriptions import get_subscriptions_tool
from app.tools.transactions import get_transactions_tool
from app.utils.dates import month_bounds, previous_calendar_period
from app.utils.merchants import top_merchants
from app.utils.money import quantize

logger = get_logger(__name__)

ToolResult = tuple[dict, list[str]]

# How far back to look before telling a user they spent nothing at a merchant.
# Six months: long enough to catch "you shopped there in the spring", and half
# the payload of the year this started at. Against the production backend the
# window is raw transactions over HTTP -- a real user's year is 2,344 rows and
# 584 KB -- so the number is a cost, not just a preference. The lookup is
# best-effort, so shortening it degrades an extra sentence, never the answer.
MERCHANT_LOOKBACK_DAYS = 180

# Months of completed history behind a savings streak. A year is enough to
# show a habit and to place the current month against a seasonal one.
SAVINGS_HISTORY_MONTHS = 12


async def _require_date_range(plan: ExecutionPlan):
    if plan.date_range is None:
        raise ValidationFailedError("This request requires a resolved date range.")
    return plan.date_range


async def _handle_transaction_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_transactions_tool(fs, ctx, plan), ["get_transactions"]


async def _handle_spending_analysis(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_expenses_tool(fs, ctx, plan), ["get_expenses", "calculate_total_spending"]


async def _handle_income_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_income_tool(fs, ctx, plan), ["get_income", "calculate_total_income"]


async def _handle_balance_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_accounts_tool(fs, ctx, plan), ["get_accounts"]


async def _handle_bill_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_bills_tool(fs, ctx, plan), ["get_bills"]


async def _handle_budget_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_budgets_tool(fs, ctx, plan), ["get_budgets", "calculate_budget_usage"]


async def _handle_subscription_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_subscriptions_tool(fs, ctx, plan), ["get_subscriptions", "calculate_recurring_cost"]


async def _handle_recurring_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_recurring_transactions_tool(fs, ctx, plan), [
        "get_recurring_transactions",
        "calculate_recurring_cost",
    ]


async def _handle_category_analysis(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    result = await get_expenses_tool(fs, ctx, plan)
    tools = ["get_expenses", "calculate_category_total"]
    if result["by_category"]:
        top_category = max(result["by_category"].items(), key=lambda kv: Decimal(kv[1]))
        result["top_category"] = {"category": top_category[0], "total": top_category[1]}
    return result, tools


async def _handle_merchant_analysis(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    date_range = await _require_date_range(plan)
    transactions = await fs.get_transactions(ctx, start=date_range.start, end=date_range.end)

    # No merchant named is not an error. It used to raise, which reached the
    # user as a red "422 validation_failed" box mid-conversation. If the
    # planner put the phrase in the category slot instead, apply_filters finds
    # it; if there is genuinely no subject, the period's biggest merchants are
    # a better answer than a crash, and they name things the user can ask
    # about next.
    if not plan.merchant_filter and not plan.category_filter:
        return (
            {
                "period": {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
                "no_merchant_named": True,
                "top_merchants": top_merchants(transactions),
                "note": (
                    "The user asked about merchant spending without naming one. Show where their "
                    "money actually went over this period and ask which merchant they meant."
                ),
                "currency": ctx.currency,
            },
            ["get_transactions"],
        )

    resolved = apply_filters(
        transactions, category_filter=plan.category_filter, merchant_filter=plan.merchant_filter
    )
    matched = resolved.transactions
    total = calculate_total_spending(matched)

    result: dict = {
        "merchant": ", ".join(resolved.merchants) if resolved.merchants else plan.merchant_filter,
        "period": {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
        "total_spent": str(total),
        "transaction_count": len(matched),
        "currency": ctx.currency,
    }

    if matched:
        dates = sorted(t.date for t in matched)
        result["first_transaction"] = dates[0].isoformat()
        result["last_transaction"] = dates[-1].isoformat()
        result["average_transaction"] = str(quantize(total / Decimal(len(matched))))
        # Per-shop rows when one query matched several ("amazon" reaches
        # Amazon Prime and Amazon Retail). One merged total would hide that
        # the figure spans more than the user probably meant.
        if len(resolved.merchants) > 1:
            result["by_merchant"] = top_merchants(matched)
    elif plan.merchant_filter:
        # Nothing in the requested window. Before reporting zero, find out
        # whether the merchant exists at all -- "you spent nothing at Costco
        # in August" and "you have never shopped at Costco" are different
        # answers, and the user cannot tell them apart from the same $0.00.
        #
        # ENRICHMENT, NOT THE ANSWER, so it is bounded, best-effort, and
        # narrow. Fetching the whole lookback window and filtering here cost
        # 276 KB against the production backend for a question already
        # answered, so the merchant name is pushed down to the backend and it
        # returns only the matching rows -- a few hundred bytes.
        #
        # The backend's filter is a plain substring match, weaker than the
        # matcher in app/utils/merchants.py, so it can MISS a merchant spelled
        # differently. That is why this branch only ever ADDS a fact: finding
        # rows proves the merchant exists, and finding none proves nothing and
        # is reported as nothing. A negative claim ("you have never shopped
        # there") is never made from the weaker matcher.
        lookback_start = date_range.start - timedelta(days=MERCHANT_LOOKBACK_DAYS)
        try:
            history = await fs.get_transactions(
                ctx, start=lookback_start, end=date_range.end, merchant=plan.merchant_filter
            )
        except (BackendTimeoutError, BackendServiceError, FinancialDataUnavailableError) as exc:
            log_with_fields(
                logger, 30, "merchant_history_lookup_skipped",
                session_id=ctx.session_id, error_type=type(exc).__name__,
            )
        else:
            seen = apply_filters(history, merchant_filter=plan.merchant_filter)
            if seen.transactions:
                last = max(t.date for t in seen.transactions)
                result["last_seen_outside_period"] = last.isoformat()
                result["searched_back_to"] = lookback_start.isoformat()
                result["note"] = (
                    f"Nothing at this merchant during the period asked about, but they did spend "
                    f"there before it -- most recently on {last.isoformat()}. Report the zero for "
                    "the period AND mention the earlier activity, so the user knows the merchant "
                    "was found."
                )

    result.update(resolved.as_dict())
    return result, ["get_transactions", "calculate_total_spending"]


async def _handle_spending_spike(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    date_range = await _require_date_range(plan)
    transactions = await fs.get_transactions(ctx, start=date_range.start, end=date_range.end)
    analysis = detect_spending_spike(transactions)
    return (
        {
            "period": {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
            "mean_daily_spending": str(analysis.mean_daily_spending),
            "stddev_daily_spending": str(analysis.stddev_daily_spending),
            "spikes": [
                {"date": s.day.isoformat(), "total": str(s.total), "stddev_multiple": str(s.stddev_multiple)}
                for s in analysis.spikes
            ],
            "methodology": (
                "A day is flagged when spending exceeds 1.5 standard deviations above the "
                "period's daily mean and is at least 50 in the account currency."
            ),
            "currency": ctx.currency,
        },
        ["get_transactions", "detect_spending_spike"],
    )


async def _handle_period_comparison(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    date_range = await _require_date_range(plan)
    # The user's own baseline wins when they named one ("compare that to last
    # month"). Otherwise derive it: same calendar days, not merely the same
    # number of days -- August 1-22 compares against July 1-22. See
    # previous_calendar_period().
    previous_range = plan.baseline_range or previous_calendar_period(date_range)
    current_txns = await fs.get_transactions(ctx, start=date_range.start, end=date_range.end)
    previous_txns = await fs.get_transactions(ctx, start=previous_range.start, end=previous_range.end)

    # "Compare my GROCERY spending this month to last" must compare groceries,
    # not everything. Narrow both sides before totalling -- headline totals
    # computed over all categories would answer a question nobody asked, and
    # the per-category block below would quietly contradict them.
    # Resolved through apply_filters, exactly as every other category filter
    # is, so "compare my restaurant spending to last month" narrows both
    # sides. A raw label comparison narrowed neither and silently compared
    # everything -- a real comparison of the wrong two numbers.
    #
    # Both periods resolve against the CURRENT period's vocabulary, so a
    # category missing from one side is still measured as a genuine zero
    # rather than resolving to a different label there.
    category = plan.category_filter
    comparison_notes: list[str] = []
    matched_merchants: list[str] = []
    if category is not None or plan.merchant_filter is not None:
        current_resolved = apply_filters(
            current_txns, category_filter=category, merchant_filter=plan.merchant_filter
        )
        previous_resolved = apply_filters(
            previous_txns,
            category_filter=current_resolved.category or category,
            merchant_filter=plan.merchant_filter,
        )
        current_txns = current_resolved.transactions
        previous_txns = previous_resolved.transactions
        category = current_resolved.category or category
        matched_merchants = current_resolved.merchants
        comparison_notes = current_resolved.notes

    comparison = compare_periods(current_txns, previous_txns)

    # Per-category totals for BOTH periods. Without these the assistant offers
    # a breakdown ("I can show which categories changed") and then has to admit
    # it has no such data — an offer it cannot honour is worse than no offer.
    def _by_category(txns: list) -> dict[str, str]:
        cats = {t.category for t in txns if t.category}
        return {c: str(calculate_category_total(txns, c)) for c in sorted(cats)}

    current_by_category = _by_category(current_txns)
    previous_by_category = _by_category(previous_txns)

    changes = {}
    for cat in sorted(set(current_by_category) | set(previous_by_category)):
        now = Decimal(current_by_category.get(cat, "0"))
        before = Decimal(previous_by_category.get(cat, "0"))
        changes[cat] = {
            "current": str(now),
            "previous": str(before),
            "difference": str(now - before),
            "percentage_change": (
                str(calculate_percentage_change(now, before))
                if calculate_percentage_change(now, before) is not None
                else None
            ),
        }

    return (
        {
            "current_period": {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
            "previous_period": {"start": previous_range.start.isoformat(), "end": previous_range.end.isoformat()},
            "current_total": str(comparison.current_total),
            "previous_total": str(comparison.previous_total),
            "difference": str(comparison.difference),
            "percentage_change": (
                str(comparison.percentage_change) if comparison.percentage_change is not None else None
            ),
            "by_category": changes,
            "category": category,
            "scope": (
                f"expenses at {', '.join(matched_merchants)} only"
                if matched_merchants
                else f"expenses in category '{category}' only"
                if category
                else "all expenses"
            ),
            "filter_notes": comparison_notes or None,
            "currency": ctx.currency,
        },
        ["get_transactions", "compare_periods", "calculate_category_total", "calculate_percentage_change"],
    )


def _month_starts(months: int, before: date) -> list[date]:
    """The first day of each of the `months` complete months before `before`."""
    starts: list[date] = []
    cursor = before
    for _ in range(months):
        cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
        starts.append(cursor)
    return list(reversed(starts))


async def _monthly_net_history(fs: FinancialService, ctx: UserContext, months: int) -> list[dict]:
    """
    Net income minus spending for each of the last `months` COMPLETE months.

    Complete only: the current month is a fifth of the way through when asked
    on the 6th, and a part-month sitting in a series of whole ones looks like
    a collapse in spending rather than a calendar artefact.

    AGGREGATES, NOT ROWS. The first version of this pulled a year of raw
    transactions in one call to bucket them here. Against SQLite that is
    instant; against the production HTTP backend it was a 584 KB response
    (2,344 rows for one real user, 2,530 for the heaviest) on EVERY savings
    question, with a 10-second client timeout. get_financial_summary already
    returns exactly the two numbers this needs, so twelve of those -- issued
    CONCURRENTLY, so it costs one round trip rather than twelve -- replace it
    with a few hundred bytes.

    Best-effort by design: a month that fails to load is reported as having no
    data rather than failing the whole answer, because the savings rate the
    user actually asked for does not depend on any of this.
    """
    today = date.today()
    first_of_this_month = date(today.year, today.month, 1)
    starts = _month_starts(months, first_of_this_month)

    async def one_month(start: date) -> dict:
        _, end = month_bounds(start.year, start.month)
        summary = await fs.get_financial_summary(ctx, start=start, end=end)
        income_total = summary.total_income.amount if summary.total_income else Decimal("0")
        expense_total = summary.total_expenses.amount if summary.total_expenses else Decimal("0")
        return {
            "month": start.strftime("%Y-%m"),
            "income": str(income_total),
            "expenses": str(expense_total),
            "net": str(calculate_cash_flow(income_total, expense_total)),
            # No money in and none out is indistinguishable from a month the
            # backend has nothing for, and the streak treats both the same
            # way: as an absence of evidence, never as a month of saving.
            "has_data": bool(income_total or expense_total),
        }

    results = await asyncio.gather(*(one_month(start) for start in starts), return_exceptions=True)

    history: list[dict] = []
    for start, result in zip(starts, results, strict=True):
        if isinstance(result, BaseException):
            log_with_fields(
                logger, 30, "savings_history_month_unavailable",
                month=start.strftime("%Y-%m"), error_type=type(result).__name__,
            )
            history.append(
                {"month": start.strftime("%Y-%m"), "income": None, "expenses": None,
                 "net": "0", "has_data": False}
            )
        else:
            history.append(result)
    return history


def _savings_streak(history: list[dict]) -> dict:
    """
    How many complete months in a row, ending most recently, ran a surplus.

    A month with no transactions BREAKS the streak rather than continuing it.
    An empty month is missing data, and counting it as a saving month would
    build a streak out of the times we could not see anything -- the most
    flattering possible reading of the least reliable data.
    """
    streak = 0
    broke_on = None
    for month in reversed(history):
        if not month["has_data"]:
            broke_on = f"{month['month']} (no transactions on record)"
            break
        if Decimal(month["net"]) <= 0:
            broke_on = f"{month['month']} (spent more than came in)"
            break
        streak += 1

    return {
        "months_in_surplus_in_a_row": streak,
        "streak_ended_at": broke_on,
        "counted_from": "the most recent COMPLETE month backwards; the current partial month is excluded",
        "monthly_history": history,
    }


async def _handle_savings_analysis(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    date_range = await _require_date_range(plan)
    summary = await fs.get_financial_summary(ctx, start=date_range.start, end=date_range.end)
    income = summary.total_income.amount if summary.total_income else Decimal("0")
    expenses = summary.total_expenses.amount if summary.total_expenses else Decimal("0")
    rate = calculate_savings_rate(income, expenses)

    history = await _monthly_net_history(fs, ctx, SAVINGS_HISTORY_MONTHS)
    return (
        {
            "period": {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
            "total_income": str(income),
            "total_expenses": str(expenses),
            # The rate answers "what proportion"; people ask "how much".
            # Reporting only a percentage forced the model to derive the
            # amount in prose, which is arithmetic done outside the
            # calculation engine -- exactly what this service exists to avoid.
            "net_saved": str(calculate_cash_flow(income, expenses)),
            "savings_rate_percent": str(rate) if rate is not None else None,
            "savings_rate_note": (
                None
                if rate is not None
                else "No income was recorded in this period, so a savings rate cannot be computed. "
                "That is undefined, not 0%."
            ),
            "streak": _savings_streak(history),
            "currency": ctx.currency,
        },
        [
            "get_financial_summary",
            "get_transactions",
            "calculate_savings_rate",
            "calculate_cash_flow",
        ],
    )


async def _handle_cash_flow_analysis(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    date_range = await _require_date_range(plan)
    summary = await fs.get_financial_summary(ctx, start=date_range.start, end=date_range.end)
    income = summary.total_income.amount if summary.total_income else Decimal("0")
    expenses = summary.total_expenses.amount if summary.total_expenses else Decimal("0")
    cash_flow = calculate_cash_flow(income, expenses)
    return (
        {
            "period": {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()},
            "total_income": str(income),
            "total_expenses": str(expenses),
            "net_cash_flow": str(cash_flow),
            "currency": ctx.currency,
        },
        ["get_financial_summary", "calculate_cash_flow"],
    )


async def _handle_affordability_analysis(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    # The planner puts the sum the user named in amount_in_question. Older
    # plans overloaded max_amount for this, so accept that as a fallback
    # rather than 422-ing a question we can clearly answer.
    # Three sources, in order of authority:
    #   1. a price the user stated -- always wins;
    #   2. an older plan that overloaded max_amount;
    #   3. a typical market price the planner supplied for a named item.
    # The third is an ESTIMATE and is flagged as one all the way to the
    # answer. Without it, "should I buy a 4K OLED TV?" could only be met with
    # "tell me the price", which is not what a finance buddy does.
    amount = plan.amount_in_question if plan.amount_in_question is not None else plan.max_amount
    amount_source = "stated" if amount is not None else None

    if amount is None and plan.estimated_price is not None:
        amount = plan.estimated_price
        amount_source = "estimated"

    accounts = await fs.get_accounts(ctx)

    # No linked accounts is not a zero balance. Treating it as one answered
    # "can I afford this?" with a confident "no" for anyone who had not
    # connected their bank yet -- the balance was unknown, not empty, and
    # nothing in the answer said so.
    if not accounts:
        return (
            {
                "available_balance": None,
                "verdict": None,
                "note": (
                    "This user has no linked accounts, so their balance is UNKNOWN -- not zero. "
                    "Say that no connected account was found and that affordability cannot be "
                    "checked until one is linked. Do NOT say the purchase is unaffordable."
                ),
                "currency": ctx.currency,
            },
            ["get_accounts"],
        )

    checking = next((a for a in accounts if a.type == "checking"), accounts[0])
    available_balance = checking.balance.amount

    today = date.today()
    upcoming_bills = await fs.get_bills(ctx, start=today, end=today + timedelta(days=14))
    upcoming_total = sum((b.amount.amount for b in upcoming_bills if not b.is_paid), Decimal("0"))

    result: dict[str, str | None] = {
        "available_balance": str(available_balance),
        "upcoming_unpaid_bills_14_days": str(upcoming_total),
        "uncommitted_balance": str(available_balance - upcoming_total),
        "currency": ctx.currency,
    }

    if amount is None:
        result["amount_in_question"] = None
        result["note"] = (
            "Neither the user nor the planner supplied a price, so no verdict can be given. "
            "Report the uncommitted balance above and ask what the item costs."
        )
        return result, ["get_accounts", "get_bills", "calculate_cash_flow"]

    amount_requested = Decimal(str(amount))
    uncommitted = available_balance - upcoming_total
    remaining = uncommitted - amount_requested

    result["amount_in_question"] = str(amount_requested)
    result["amount_source"] = amount_source
    result["projected_remaining_after_spend"] = str(remaining)

    # The verdict is computed here, not written by the model. "Comfortable"
    # means the purchase leaves at least a quarter of the uncommitted balance
    # behind; clearing to almost exactly zero is affordable in arithmetic and
    # reckless in practice, and the answer should not call those the same
    # thing.
    if remaining < 0:
        verdict = "not_affordable"
    elif uncommitted > 0 and remaining < uncommitted / 4:
        verdict = "tight"
    else:
        verdict = "comfortable"
    result["verdict"] = verdict

    if amount_source == "estimated":
        result["estimated_price_basis"] = plan.estimated_price_basis or "a typical market price"
        result["note"] = (
            "The price is an ESTIMATE the assistant supplied, not a figure from the user or their "
            "data. Say so plainly and give the verdict. Allow for a correction with a short "
            "parenthetical inside the sentence -- \"(correct me if yours differs)\" -- never with a "
            "trailing offer such as \"let me know\". Every other figure here is from their accounts."
        )
    return result, ["get_accounts", "get_bills", "calculate_cash_flow"]


async def _handle_forecast_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    today = date.today()
    lookback_start = today - timedelta(days=29)
    forecast_days = 30
    if plan.date_range:
        forecast_days = max((plan.date_range.end - plan.date_range.start).days + 1, 1)

    lookback_txns = await fs.get_transactions(ctx, start=lookback_start, end=today)
    result = forecast_spending(
        lookback_txns, lookback_start=lookback_start, lookback_end=today, forecast_days=forecast_days
    )
    if result is None:
        return {"availability": "missing", "reason": "no_lookback_data"}, ["get_transactions"]

    return (
        {
            "is_estimate": True,
            "forecast_days": result.forecast_days,
            "projected_total": str(result.projected_total),
            "based_on_average_daily_spending": str(result.based_on_average_daily_spending),
            "lookback_days": result.lookback_days,
            "methodology_note": result.methodology_note,
            "currency": ctx.currency,
        },
        ["get_transactions", "forecast_spending"],
    )


async def _handle_investment_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_investments_tool(fs, ctx, plan), ["get_investments"]


async def _handle_liability_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_liabilities_tool(fs, ctx, plan), ["get_liabilities"]


async def _handle_financial_score_query(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    return await get_financial_score_tool(fs, ctx, plan), ["get_financial_score"]


async def _handle_financial_calculation(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    # Best-effort generic fallback for ad-hoc calculation requests that don't
    # map to a more specific intent. Uses whatever date range was resolved;
    # if none, falls back to total spending being unavailable rather than
    # guessing a window.
    if plan.date_range is None:
        return {"availability": "missing", "reason": "no_date_range_resolved"}, []
    transactions = await fs.get_transactions(ctx, start=plan.date_range.start, end=plan.date_range.end)
    total = calculate_total_spending(transactions)
    return (
        {
            "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
            "total_outflow": str(total),
            "currency": ctx.currency,
        },
        ["get_transactions", "calculate_total_spending"],
    )


async def _handle_overall_financial_health(fs: FinancialService, ctx: UserContext, plan: ExecutionPlan) -> ToolResult:
    today = date.today()
    period_start = date(today.year, today.month, 1)
    summary = await fs.get_financial_summary(ctx, start=period_start, end=today)
    liabilities = await fs.get_liabilities(ctx)
    score = await fs.get_financial_score(ctx)

    income = summary.total_income.amount if summary.total_income else Decimal("0")
    expenses = summary.total_expenses.amount if summary.total_expenses else Decimal("0")
    # Only liabilities that actually state a minimum payment can support a
    # debt-to-income ratio. This backend supplies none, so the sum was always
    # zero and the ratio always "0.0%" -- reported next to $42,035 of credit
    # card balances. A ratio computed from no data is not a low ratio.
    payments_known = [li.minimum_payment.amount for li in liabilities if li.minimum_payment]
    monthly_debt_payments = sum(payments_known, Decimal("0"))
    debt_ratio = (
        calculate_debt_ratio(monthly_debt_payments, income) if payments_known else None
    )

    return (
        {
            "period": {"start": period_start.isoformat(), "end": today.isoformat()},
            "total_income": str(income),
            "total_expenses": str(expenses),
            "net_cash_flow": str(calculate_cash_flow(income, expenses)),
            "savings_rate_percent": (
                str(r) if (r := calculate_savings_rate(income, expenses)) is not None else None
            ),
            "total_liabilities": (
                str(sum((li.balance.amount for li in liabilities), Decimal("0"))) if liabilities else None
            ),
            "liabilities_available": bool(liabilities),
            "debt_to_income_percent": str(debt_ratio) if debt_ratio is not None else None,
            "debt_to_income_note": (
                None
                if debt_ratio is not None
                else "No minimum payment amounts are on file, so a debt-to-income ratio cannot be "
                "computed. Do NOT present this as a low or healthy ratio -- say it is unavailable "
                "and describe the outstanding balances instead."
            ),
            "financial_score": score.score if score else None,
            "currency": ctx.currency,
        },
        ["get_financial_summary", "get_liabilities", "get_financial_score", "calculate_savings_rate", "calculate_debt_ratio"],
    )


_HANDLERS = {
    Intent.TRANSACTION_QUERY: _handle_transaction_query,
    Intent.SPENDING_ANALYSIS: _handle_spending_analysis,
    Intent.INCOME_QUERY: _handle_income_query,
    Intent.BALANCE_QUERY: _handle_balance_query,
    Intent.BILL_QUERY: _handle_bill_query,
    Intent.BUDGET_QUERY: _handle_budget_query,
    Intent.SUBSCRIPTION_QUERY: _handle_subscription_query,
    Intent.RECURRING_EXPENSE_QUERY: _handle_recurring_query,
    Intent.CATEGORY_ANALYSIS: _handle_category_analysis,
    Intent.MERCHANT_ANALYSIS: _handle_merchant_analysis,
    Intent.SPENDING_SPIKE: _handle_spending_spike,
    Intent.PERIOD_COMPARISON: _handle_period_comparison,
    Intent.SAVINGS_ANALYSIS: _handle_savings_analysis,
    Intent.CASH_FLOW_ANALYSIS: _handle_cash_flow_analysis,
    Intent.AFFORDABILITY_ANALYSIS: _handle_affordability_analysis,
    Intent.FORECAST_QUERY: _handle_forecast_query,
    Intent.INVESTMENT_QUERY: _handle_investment_query,
    Intent.LIABILITY_QUERY: _handle_liability_query,
    Intent.FINANCIAL_SCORE_QUERY: _handle_financial_score_query,
    Intent.FINANCIAL_CALCULATION: _handle_financial_calculation,
    Intent.OVERALL_FINANCIAL_HEALTH: _handle_overall_financial_health,
}


async def route_and_execute(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> ToolResult:
    """
    Executes the tool(s) for plan.intent, scoped to `context` throughout.
    Returns ({}, []) for intents that need no financial data.
    """
    if not plan.requires_financial_data:
        return {}, []

    handler = _HANDLERS.get(plan.intent)
    if handler is None:
        return {}, []

    try:
        return await handler(financial_service, context, plan)
    except ValidationFailedError:
        raise
    except (BackendTimeoutError, BackendServiceError, FinancialDataUnavailableError):
        # The backend being slow or down is the most likely runtime failure in
        # production, and it is a CONVERSATIONAL outcome, not a crash. Wrapped
        # in ToolExecutionError it reached the user mid-chat as a red "500
        # tool_execution_error" box -- the same fault already fixed once for
        # ValidationFailedError. The orchestrator catches these and says, in
        # words, that the data could not be reached right now.
        raise
    except CrossUserDataError:
        # Never degrade this one into an answer. Another user's records
        # reaching this service is a containment breach: the request must
        # abort, not continue with a partial reply.
        raise
    except Exception as exc:  # noqa: BLE001 - convert anything unexpected into our error type
        raise ToolExecutionError(f"Failed to execute tools for intent {plan.intent.value}.") from exc
