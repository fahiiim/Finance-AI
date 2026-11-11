"""
Translation of the MyFi internal API's JSON into this service's domain schemas.

Kept apart from the HTTP client on purpose: these are pure functions, so every
rule below can be unit-tested against a recorded payload with no network, and
the client class stays a thin "fetch, then map" layer.

THE LIVE CONTRACT (verified against /MyFi/api/internal/* for a real user)
    * Every response is an envelope: {"status", "message", "data", "meta"}.
    * Money is {"amount": "<string>", "currency": "USD"}.
    * Income vs expense classification follows the Allo application's
      `primary_category` rule: `INCOME` and `TRANSFER_IN` are income (stored
      as negative in the domain model per app/utils/transactions.py); all
      other categories including `TRANSFER_OUT` are expenses (stored positive).
      The unsigned `amount` magnitude is used when `primary_category` is
      supplied, matching Allo's backend summaries; `amount_signed` / `direction`
      remain the fallback when `primary_category` is absent.
    * The backend filters `is_deleted = 0` server-side; Python does not apply
      additional exclusion filtering on `is_review_transfer` or `is_internal_transfer`.

Every mapper also accepts the flat shape described in
docs/handover-backend.md (bare list, `amount` as a plain string), so the client
keeps working if the backend is ever pointed at a conforming implementation.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.exceptions import BackendServiceError
from app.schemas.financial import (
    Account,
    AlloScore,
    Bill,
    BudgetCategory,
    FinancialScore,
    InvestmentHolding,
    Liability,
    LiabilitySnapshot,
    Money,
    NetWorth,
    RecurringTransaction,
    RetirementReadiness,
    SavingsGoal,
    Subscription,
    Transaction,
)
from app.utils.merchants import clean_merchant_name

CURRENCY = "USD"

# A flagged row is excluded from spending/income only when its category agrees
# it is a transfer. The flag alone is a question, not an answer: on the live
# data `is_internal_transfer` is also set on payroll-tax debits, DoorDash
# orders and insurance premiums. On the production data this was first measured on, 2,252
# rows carried the review flag and only 911 were transfers; dropping every
# flagged row understated one user's food spending by 45%.
TRANSFER_CATEGORIES = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})

# Liability rows come back for EVERY account at a liability-bearing institution
# - checking, brokerage, crypto - not just debts. Only these are debts.
_DEBT_TYPE_MARKERS = (
    "credit", "loan", "mortgage", "home_equity", "heloc", "line_of_credit",
    "student", "auto", "debt", "overdraft",
)


# ------------------------------------------------------------------ primitives


def envelope(payload: Any) -> tuple[Any, dict]:
    """
    Split a response into (data, meta).

    A non-2xx `status` inside a 200 response is raised, not returned: this API
    reports "User not found" and "Internal service token is not configured"
    through the envelope, and treating `data: null` as "no records" would
    present an outage as an empty account.
    """
    if isinstance(payload, dict) and "status" in payload and "data" in payload:
        status = payload.get("status")
        if isinstance(status, int) and status >= 400:
            raise BackendServiceError(
                "The backend financial data service returned an error.",
                details={"status_code": status},
            )
        meta = payload.get("meta")
        return payload["data"], meta if isinstance(meta, dict) else {}
    return payload, {}


def unwrap(payload: Any) -> Any:
    return envelope(payload)[0]


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def money(value: Any) -> Money | None:
    """A {"amount","currency"} object, or a bare number/string, as Money."""
    if value is None:
        return None
    if isinstance(value, dict):
        amount = to_decimal(value.get("amount"))
        if amount is None:
            return None
        return Money(amount=amount, currency=value.get("currency") or CURRENCY)
    amount = to_decimal(value)
    return Money(amount=amount, currency=CURRENCY) if amount is not None else None


def require_money(value: Any) -> Money:
    return money(value) or Money(amount=Decimal("0"), currency=CURRENCY)


def pretty_category(raw: str | None) -> str | None:
    """FOOD_AND_DRINK -> Food And Drink. Plaid's constants are not for reading."""
    if not raw:
        return None
    return raw.replace("_", " ").title()


def pretty_subcategory(detailed: str | None, primary: str | None) -> str | None:
    """FOOD_AND_DRINK_RESTAURANT under FOOD_AND_DRINK -> 'Restaurant'."""
    if not detailed:
        return None
    trimmed = detailed
    if primary and detailed.startswith(primary):
        trimmed = detailed[len(primary):]
    trimmed = trimmed.strip("_")
    if not trimmed:
        return None
    return trimmed.replace("_", " ").title()


def _owner(item: dict, fallback: str | None = None) -> str | None:
    """
    The owner the BACKEND declared for this record, or None.

    Never the requested user's id as a stand-in. Stamping a record the backend
    did not label with the id we asked for makes the containment check in
    app/core/data_isolation.py pass vacuously -- it would be comparing our own
    request against itself. An unlabeled record stays unlabeled, so it is
    counted (and, with STRICT_DATA_OWNERSHIP=true, rejected).

    `fallback` is accepted for call-site compatibility and deliberately
    ignored.
    """
    owner = item.get("owner_user_id")
    return str(owner) if owner is not None else None


# ---------------------------------------------------------------- transactions


def is_excluded_transfer(item: dict) -> bool:
    """Review-flagged AND categorised as a transfer. See TRANSFER_CATEGORIES."""
    if not item.get("is_review_transfer"):
        return False
    primary = (item.get("primary_category") or "").upper()
    return primary in TRANSFER_CATEGORIES


_INCOME_PRIMARY_CATEGORIES = frozenset({"INCOME", "TRANSFER_IN"})


def _signed_amount(item: dict) -> tuple[Decimal, str]:
    signed = money(item.get("amount_signed"))
    magnitude = money(item.get("amount"))
    direction = item.get("direction")
    primary = (item.get("primary_category") or "").strip().upper()

    if primary:
        # Canonical Allo classification: INCOME and TRANSFER_IN are inflows
        # (negative in our domain sign convention); all other primary categories
        # including TRANSFER_OUT are outflows/expenses (positive). Use unsigned
        # `amount` magnitude when present so totals match Allo's dashboard.
        base = magnitude if magnitude is not None else signed
        if base is None:
            return Decimal("0"), CURRENCY
        sign = -1 if primary in _INCOME_PRIMARY_CATEGORIES else 1
        return abs(base.amount) * sign, base.currency

    if signed is not None:
        amount, currency = signed.amount, signed.currency
        if item.get("is_plaid") is False and direction in ("inflow", "outflow"):
            sign = -1 if direction == "inflow" else 1
            return abs(amount) * sign, currency
        return amount, currency

    flat = item.get("amount")
    if magnitude is None:
        return Decimal("0"), CURRENCY
    if isinstance(flat, dict):
        # An absolute amount with no sign: recover it from `direction`, the
        # only other signal. Outflow is positive, per app/utils/transactions.py.
        sign = -1 if direction == "inflow" else 1
        return abs(magnitude.amount) * sign, magnitude.currency
    # Handover shape: a plain signed string, passed through unchanged.
    return magnitude.amount, magnitude.currency


def map_transaction(item: dict, owner_fallback: str | None) -> Transaction | None:
    when = to_date(item.get("date"))
    if when is None:
        return None
    amount, currency = _signed_amount(item)

    # Manually entered rows have no Plaid id. Dropping them understated one
    # user's July spending by exactly what they had typed in, so the backend's
    # own surrogate (`external_or_manual_id`, else the primary key) identifies
    # them instead.
    identifier = (
        item.get("transaction_id")
        or item.get("external_or_manual_id")
        or (f"manual-{item['record_id']}" if item.get("record_id") is not None else None)
        or f"manual-{when.isoformat()}-{amount}"
    )
    primary = item.get("primary_category")
    category = pretty_category(primary) if primary else item.get("category")

    return Transaction(
        owner_user_id=_owner(item, owner_fallback),
        transaction_id=str(identifier),
        account_id=str(item.get("account_id") or "manual"),
        amount=amount,
        currency=currency,
        date=when,
        merchant_name=item.get("merchant_name") or item.get("description"),
        description=item.get("description"),
        category=category,
        subcategory=pretty_subcategory(item.get("detailed_category"), primary),
        pending=bool(item.get("pending")),
    )


# -------------------------------------------------------------------- accounts


def map_account(item: dict, owner_fallback: str | None) -> Account | None:
    account_id = item.get("account_id")
    if not account_id:
        return None
    return Account(
        owner_user_id=_owner(item, owner_fallback),
        account_id=str(account_id),
        name=item.get("name") or "Account",
        type=item.get("type") or "other",
        balance=require_money(item.get("balance")),
        institution_name=item.get("institution_name"),
        is_active=bool(item.get("is_active", True)),
    )


# ----------------------------------------------------------------------- bills


def map_bills(data: Any, owner_fallback: str | None) -> list[Bill]:
    """
    The bills payload is not a list: it is one object holding the month's
    completed and upcoming bills plus totals. Completed -> paid, upcoming and
    due-on-date -> unpaid. A bill appears once even if the backend lists it
    under both `upcoming_bills` and `due_bills`.
    """
    groups: list[tuple[list, bool | None]]
    if isinstance(data, list):  # handover shape
        groups = [(data, None)]
    else:
        body = as_dict(data)
        groups = [
            (as_list(body.get("completed_bills")), True),
            (as_list(body.get("upcoming_bills")), False),
            (as_list(body.get("due_bills")), False),
        ]

    owner = owner_fallback
    if not isinstance(data, list):
        owner = _owner(as_dict(data), owner_fallback)

    # Keyed on id AND due date: a biweekly stream reuses its stream id for
    # each occurrence, and collapsing those would drop a real bill.
    seen: set[tuple[str, date]] = set()
    bills: list[Bill] = []
    for items, paid in groups:
        for item in items:
            item = as_dict(item)
            due = to_date(item.get("due_date"))
            amount = money(item.get("amount"))
            bill_id = str(item.get("bill_id") or item.get("stream_id") or "")
            if due is None or amount is None or (bill_id and (bill_id, due) in seen):
                continue
            if bill_id:
                seen.add((bill_id, due))
            bills.append(
                Bill(
                    owner_user_id=_owner(item, owner),
                    bill_id=bill_id,
                    name=clean_merchant_name(item.get("name")),
                    amount=amount,
                    due_date=due,
                    is_paid=bool(item.get("is_paid", False)) if paid is None else paid,
                )
            )
    return bills


# --------------------------------------------------------------------- budgets


def map_budgets(data: Any, owner_fallback: str | None) -> list[BudgetCategory]:
    if isinstance(data, list):  # handover shape
        items, owner = data, owner_fallback
    else:
        body = as_dict(data)
        items, owner = as_list(body.get("budgets")), _owner(body, owner_fallback)

    out: list[BudgetCategory] = []
    for item in items:
        item = as_dict(item)
        budgeted = money(item.get("budgeted_amount"))
        if budgeted is None:
            continue
        out.append(
            BudgetCategory(
                owner_user_id=_owner(item, owner),
                category=pretty_category(item.get("primary_category") or item.get("category"))
                or "Uncategorised",
                budgeted_amount=budgeted,
                spent_amount=require_money(item.get("spent_amount")),
            )
        )
    return out


# -------------------------------------------------- recurring / subscriptions


# Recurring streams in these categories are real recurring payments but not
# something a person means by "my subscriptions": moving money between their
# own accounts, paying down a loan or card, bank fees and taxes. On the live
# data these were 25 of 84 monthly streams, including a $1,999 card payment.
NON_SUBSCRIPTION_CATEGORIES = frozenset({
    "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "BANK_FEES",
    "GOVERNMENT_AND_NON_PROFIT", "INCOME",
})


# ACH debits carry the originator's descriptor after "DES:" -- payroll runs,
# payroll taxes, utility and insurance autopays. They recur, but they are bills
# and business flows, not subscriptions: on the live data this one rule removed
# every Gusto payroll line from a list of 50.
_ACH_DESCRIPTOR = re.compile(r"\bDES:", re.IGNORECASE)


def map_subscriptions(
    data: Any, owner_fallback: str | None, categories: dict[str, str] | None = None
) -> list[Subscription]:
    """
    Monthly outflows that are not transfers, debt service, fees or taxes.

    The backend returns every recurring stream here (loan payments, transfers,
    biweekly payroll), and the subscriptions payload carries no category.
    `categories` maps stream id -> raw Plaid category, taken from the
    recurring-transactions endpoint, which serves the same stream ids; with
    it, non-subscription categories are dropped. Without it the monthly-cycle
    rule alone applies (the original, pre-API behaviour).

    The same service re-billed under a new stream id appears more than once
    (three identical Amazon Prime rows); exact name+amount repeats are
    collapsed so a monthly total is not inflated by them.
    """
    categories = categories or {}
    out: list[Subscription] = []
    seen: set[tuple[str, str]] = set()
    for item in as_list(data):
        item = as_dict(item)
        amount = money(item.get("amount"))
        cycle = str(item.get("billing_cycle") or "").lower()
        if amount is None or amount.amount <= 0 or "month" not in cycle:
            continue
        if item.get("is_active") is False:
            continue
        stream_id = str(item.get("subscription_id") or "")
        if (categories.get(stream_id) or "").upper() in NON_SUBSCRIPTION_CATEGORIES:
            continue
        if _ACH_DESCRIPTOR.search(str(item.get("name") or "")):
            continue
        key = (clean_merchant_name(item.get("name")).lower(), str(amount.amount))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Subscription(
                owner_user_id=_owner(item, owner_fallback),
                subscription_id=str(item.get("subscription_id") or ""),
                name=clean_merchant_name(item.get("name")),
                amount=amount,
                billing_cycle="monthly",
                next_charge_date=to_date(item.get("next_charge_date")),
                is_active=True,
            )
        )
    return out


def map_recurring(data: Any, owner_fallback: str | None) -> list[RecurringTransaction]:
    out: list[RecurringTransaction] = []
    for item in as_list(data):
        item = as_dict(item)
        amount = money(item.get("amount"))
        if amount is None or amount.amount <= 0:
            continue
        out.append(
            RecurringTransaction(
                owner_user_id=_owner(item, owner_fallback),
                recurring_id=str(item.get("recurring_id") or ""),
                name=clean_merchant_name(item.get("name")),
                amount=amount,
                frequency=str(item.get("frequency") or "unknown").lower(),
                category=pretty_category(item.get("category")),
                next_expected_date=to_date(item.get("next_expected_date")),
            )
        )
    return out


# ------------------------------------------------------- investments / debts


def map_investments(data: Any, owner_fallback: str | None) -> list[InvestmentHolding]:
    out: list[InvestmentHolding] = []
    for item in as_list(data):
        item = as_dict(item)
        value = money(item.get("market_value"))
        if value is None:
            continue
        out.append(
            InvestmentHolding(
                owner_user_id=_owner(item, owner_fallback),
                holding_id=str(item.get("holding_id") or ""),
                account_id=str(item.get("account_id") or ""),
                symbol=item.get("symbol"),
                name=item.get("name") or "Unnamed holding",
                quantity=to_decimal(item.get("quantity")) or Decimal("0"),
                market_value=value,
                cost_basis=money(item.get("cost_basis")),
            )
        )
    return out


def is_debt_type(raw_type: str | None) -> bool:
    lowered = (raw_type or "").lower()
    return any(marker in lowered for marker in _DEBT_TYPE_MARKERS)


def map_liabilities(data: Any, owner_fallback: str | None) -> list[Liability]:
    out: list[Liability] = []
    for item in as_list(data):
        item = as_dict(item)
        if not is_debt_type(item.get("type")):
            continue
        balance = money(item.get("balance"))
        if balance is None:
            continue
        out.append(
            Liability(
                owner_user_id=_owner(item, owner_fallback),
                liability_id=str(item.get("liability_id") or ""),
                name=item.get("name") or "Liability",
                type=str(item.get("type")),
                balance=balance,
                interest_rate=to_decimal(item.get("interest_rate")),
                minimum_payment=money(item.get("minimum_payment")),
                is_overdue=item.get("is_overdue"),
                next_payment_due_date=to_date(item.get("next_payment_due_date")),
                last_payment_date=to_date(item.get("last_payment_date")),
            )
        )
    return out


# ----------------------------------------------------------------------- scores


def map_allo_score(item: Any, owner_fallback: str | None) -> AlloScore | None:
    item = as_dict(item)
    if item.get("overall_score") is None:
        return None

    def score(name: str) -> int:
        value = item.get(name)
        return int(value) if value is not None else 0

    as_of = to_date(item.get("as_of_date"))
    start = to_date(item.get("period_start")) or as_of
    end = to_date(item.get("period_end")) or as_of
    if start is None or end is None:
        return None
    return AlloScore(
        owner_user_id=_owner(item, owner_fallback),
        overall_score=score("overall_score"),
        overall_tier=item.get("overall_tier") or "",
        overall_insight=item.get("overall_insight"),
        flow_score=score("flow_score"),
        flow_tier=item.get("flow_tier") or "",
        flow_insight=item.get("flow_insight"),
        stability_score=score("stability_score"),
        stability_tier=item.get("stability_tier") or "",
        stability_insight=item.get("stability_insight"),
        retirement_score=score("retirement_score"),
        retirement_tier=item.get("retirement_tier") or "",
        retirement_insight=item.get("retirement_insight"),
        period_start=start,
        period_end=end,
    )


def map_credit_score(item: Any, owner_fallback: str | None) -> FinancialScore | None:
    item = as_dict(item)
    raw = item.get("score")
    if raw is None:
        return None
    return FinancialScore(
        owner_user_id=_owner(item, owner_fallback),
        score=int(raw),
        score_range_min=300,
        score_range_max=850,
        factors=[str(f) for f in (item.get("provider"), item.get("score_model")) if f],
        as_of_date=to_date(item.get("report_date")),
    )


# ---------------------------------------------------- savings / debt history


def map_savings_goal(data: Any, owner_fallback: str | None) -> SavingsGoal | None:
    """The newest stored goal. The list is the raw `save_goals` rows."""
    body = as_dict(data)
    goals = [as_dict(g) for g in as_list(body.get("goals"))]
    owner = _owner(body, owner_fallback)

    if goals:
        # Highest id is the most recently created goal.
        newest = max(goals, key=lambda g: int(str(g.get("goal_id") or 0) or 0))
        target = money(newest.get("total_goal"))
        if target is None:
            return None
        return SavingsGoal(
            owner_user_id=_owner(newest, owner),
            goal_id=str(newest.get("goal_id") or ""),
            target_amount=target,
            monthly_target=money(newest.get("monthly_goal")),
            start_date=to_date(newest.get("start_date")),
            end_date=to_date(newest.get("end_date")),
            saved_amount=money(body.get("total_saving")),
        )

    # No raw rows, but the summary still states a goal.
    target = money(body.get("total_goal"))
    if target is None or target.amount <= 0:
        return None
    return SavingsGoal(
        owner_user_id=owner,
        goal_id="current",
        target_amount=target,
        monthly_target=money(body.get("monthly_goal")),
        start_date=to_date(body.get("goal_start_date")),
        end_date=to_date(body.get("goal_end_date")),
        saved_amount=money(body.get("total_saving")),
    )


def map_liability_history(data: Any, owner_fallback: str | None) -> list[LiabilitySnapshot]:
    """
    Total debt per snapshot date, newest first.

    Snapshots are per account; the trend question is about the TOTAL, so they
    are summed by date (the grouping the debt trend has always used). Monthly
    totals are the fallback when no per-account snapshots are served.
    """
    body = as_dict(data)
    owner = _owner(body, owner_fallback)

    totals: dict[date, Decimal] = {}
    currency = CURRENCY
    for snap in as_list(body.get("snapshots")):
        snap = as_dict(snap)
        when = to_date(snap.get("snapshot_date"))
        balance = money(snap.get("balance"))
        if when is None or balance is None:
            continue
        currency = balance.currency
        totals[when] = totals.get(when, Decimal("0")) + balance.amount

    if not totals:
        for row in as_list(body.get("monthly_history")):
            row = as_dict(row)
            when = to_date(f"{row.get('month')}-01") if row.get("month") else None
            balance = money(row.get("total_balance"))
            if when is None or balance is None:
                continue
            currency = balance.currency
            totals[when] = balance.amount

    return [
        LiabilitySnapshot(
            owner_user_id=owner,
            account_id="all",
            account_type="total",
            balance=Money(amount=total, currency=currency),
            snapshot_date=when,
        )
        for when, total in sorted(totals.items(), reverse=True)
    ]


def map_retirement(data: Any, owner_fallback: str | None) -> RetirementReadiness | None:
    body = as_dict(data)
    metrics = as_dict(body.get("raw_metrics"))
    profile = as_dict(body.get("profile"))
    if not metrics and not profile:
        return None

    def m_money(name: str) -> Money | None:
        value = to_decimal(metrics.get(name))
        return Money(amount=value, currency=CURRENCY) if value is not None else None

    def m_int(name: str, *fallbacks: Any) -> int | None:
        for value in (metrics.get(name), *fallbacks):
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    goal = m_money("monthly_retirement_income_goal") or money(profile.get("monthly_retirement_income_goal"))
    return RetirementReadiness(
        owner_user_id=_owner(body, owner_fallback),
        age=m_int("age", profile.get("age")),
        target_retirement_age=m_int("target_retirement_age", profile.get("target_retirement_age")),
        years_remaining=m_int("years_remaining"),
        retirement_target=m_money("retirement_target"),
        future_assets=m_money("future_assets"),
        total_savings=m_money("total_savings"),
        total_assets=m_money("assets"),
        total_cash=m_money("total_cash"),
        total_debt=m_money("total_debt"),
        investment_contributions=m_money("investment_contributions"),
        investment_contribution_months=m_int("investment_contribution_months"),
        monthly_retirement_income_goal=goal,
        annual_contribution=m_money("annual_contribution"),
        savings_rate=to_decimal(metrics.get("savings_rate")),
        investment_rate=to_decimal(metrics.get("investment_rate")),
        readiness_ratio=to_decimal(metrics.get("readiness_ratio")),
    )


def map_net_worth(data: Any, owner_fallback: str | None) -> NetWorth | None:
    body = as_dict(data)
    total = money(body.get("net_worth"))
    assets = money(body.get("total_assets"))
    liabilities = money(body.get("total_liabilities"))
    if total is None or assets is None or liabilities is None:
        return None
    breakdown = {
        str(name): value
        for name, raw in as_dict(body.get("asset_breakdown")).items()
        if (value := money(raw)) is not None
    }
    history = []
    for point in as_list(body.get("graph_data")):
        point = as_dict(point)
        value = money(point.get("net_worth"))
        point_assets = money(point.get("total_assets"))
        point_liabilities = money(point.get("total_liabilities"))
        if point.get("month") and value is not None:
            history.append({
                "month": str(point["month"]),
                "net_worth": str(value.amount),
                "total_assets": str(point_assets.amount) if point_assets else None,
                "total_liabilities": str(point_liabilities.amount) if point_liabilities else None,
            })
    return NetWorth(
        owner_user_id=_owner(body, owner_fallback),
        net_worth=total,
        total_assets=assets,
        total_liabilities=liabilities,
        as_of_date=to_date(body.get("as_of_date")),
        asset_breakdown=breakdown,
        history=history,
    )
