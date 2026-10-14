"""
BackendAPIClient against the MyFi internal API's REAL response shapes.

The payloads below are synthetic but structurally identical to what
/MyFi/api/internal/* returns (verified live for one user): envelopes, money
objects, `amount` as an ABSOLUTE value beside `amount_signed`, paginated
transactions, and bills as one object rather than a list. They contain no
real customer data.

Each test pins one way the integration could silently produce a wrong number.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import BackendServiceError
from app.schemas.planner import ExecutionPlan, Intent, ResolvedDateRange
from app.services.backend_client import BackendAPIClient
from app.services.financial_service import FinancialService
from app.tools.bills import get_bills_tool
from app.tools.net_worth import get_net_worth_tool
from tests.conftest import make_user_context

USER = "267"


def money(value: str) -> dict:
    return {"amount": value, "currency": "USD"}


def envelope(data, **meta) -> dict:
    return {"status": 200, "message": "Data retrieved successfully.", "data": data, "meta": meta}


def tx(record_id, signed, when="2026-10-05", *, primary="FOOD_AND_DRINK", detailed=None,
       review=False, internal=False, txn_id="UNSET", name="Some Merchant", owner=USER) -> dict:
    value = Decimal(signed)
    return {
        "owner_user_id": owner,
        "record_id": str(record_id),
        "transaction_id": f"plaid-{record_id}" if txn_id == "UNSET" else txn_id,
        "external_or_manual_id": f"plaid-{record_id}" if txn_id == "UNSET" else f"manual-{record_id}",
        "account_id": "acct-1",
        "amount": money(str(abs(value))),
        "amount_signed": money(str(value)),
        "direction": "outflow" if value > 0 else "inflow",
        "date": when,
        "merchant_name": name,
        "description": name,
        "category": "Display Name",
        "primary_category": primary,
        "detailed_category": detailed or primary,
        "pending": False,
        "is_review_transfer": review,
        "is_internal_transfer": internal,
    }


class FakeRepo:
    """Stands in for HttpRepository; routes by path and records every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path, *, params=None):
        self.calls.append((path, dict(params or {})))
        value = self.routes[path]
        return value(params or {}) if callable(value) else value

    async def aclose(self) -> None:  # pragma: no cover
        return None


def make_client(routes: dict) -> tuple[BackendAPIClient, FakeRepo]:
    repo = FakeRepo(routes)
    return BackendAPIClient(repo), repo


CTX = make_user_context(user_id=USER)


# ----------------------------------------------------------------- envelope


@pytest.mark.asyncio
async def test_an_error_status_inside_a_200_is_an_error_not_an_empty_account():
    client, _ = make_client({
        "/internal/accounts": {"status": 404, "message": "User not found.", "data": None},
    })
    with pytest.raises(BackendServiceError):
        await client.get_accounts(CTX)


# ------------------------------------------------------------- transactions


@pytest.mark.asyncio
async def test_signs_come_from_amount_signed_so_income_is_not_spending():
    client, _ = make_client({"/internal/transactions": envelope([
        tx(1, "45.20"),                                   # purchase
        tx(2, "-1475.00", primary="INCOME", name="Payroll"),  # deposit
    ], pagination={"page": 1, "per_page": 500, "total": 2, "total_pages": 1})})

    rows = {t.transaction_id: t for t in await client.get_transactions(CTX)}

    assert rows["plaid-1"].amount == Decimal("45.20")
    assert rows["plaid-2"].amount == Decimal("-1475.00"), "an inflow must stay negative"


@pytest.mark.asyncio
async def test_a_review_flag_alone_does_not_remove_a_purchase():
    """Real payroll-tax debits and DoorDash orders carry the flag."""
    client, _ = make_client({"/internal/transactions": envelope([
        tx(1, "56.70", primary="FOOD_AND_DRINK", review=True, internal=True, name="DoorDash"),
        tx(2, "1000.00", primary="TRANSFER_OUT", review=True, internal=True, name="Card payment"),
        tx(3, "20.00", primary="TRANSFER_OUT", review=False, name="Unflagged transfer"),
    ], pagination={"total_pages": 1})})

    ids = {t.transaction_id for t in await client.get_transactions(CTX)}

    assert "plaid-1" in ids, "a flagged purchase is still a purchase"
    assert "plaid-2" not in ids, "flag AND transfer category is a genuine internal transfer"
    assert "plaid-3" in ids, "the category alone does not remove it either"


@pytest.mark.asyncio
async def test_manually_entered_transactions_are_kept_with_a_stable_id():
    client, _ = make_client({"/internal/transactions": envelope(
        [tx(77, "500.00", txn_id=None)], pagination={"total_pages": 1}
    )})

    [row] = await client.get_transactions(CTX)

    assert row.transaction_id == "manual-77"
    assert row.amount == Decimal("500.00")


@pytest.mark.asyncio
async def test_every_page_is_read_and_a_row_on_two_pages_is_counted_once():
    pages = {
        1: [tx(1, "10.00"), tx(2, "20.00")],
        2: [tx(2, "20.00"), tx(3, "30.00")],   # record 2 slid across the boundary
        3: [tx(4, "40.00")],
    }

    def route(params):
        return envelope(pages[params["page"]], pagination={"page": params["page"], "total_pages": 3})

    client, repo = make_client({"/internal/transactions": route})

    rows = await client.get_transactions(CTX, start=date(2026, 10, 1), end=date(2026, 10, 31))

    assert sorted(t.transaction_id for t in rows) == ["plaid-1", "plaid-2", "plaid-3", "plaid-4"]
    assert sum(t.amount for t in rows) == Decimal("100.00")
    assert sorted(p["page"] for _, p in repo.calls) == [1, 2, 3]
    # The window and the user are sent on every page, inclusive bounds as given.
    for _, params in repo.calls:
        assert params["user_id"] == USER
        assert params["start"] == "2026-10-01" and params["end"] == "2026-10-31"
        assert params["per_page"] == 500


@pytest.mark.asyncio
async def test_a_result_too_large_to_read_raises_instead_of_truncating():
    client, _ = make_client({"/internal/transactions": envelope(
        [tx(1, "1.00")], pagination={"page": 1, "total_pages": 10_000}
    )})

    with pytest.raises(BackendServiceError):
        await client.get_transactions(CTX)


@pytest.mark.asyncio
async def test_the_summary_uses_the_one_expense_definition_and_skips_transfers():
    client, _ = make_client({"/internal/transactions": envelope([
        tx(1, "100.00"),
        tx(2, "50.00"),
        tx(3, "-3000.00", primary="INCOME", name="Payroll"),
        tx(4, "900.00", primary="TRANSFER_OUT", review=True, internal=True, name="Card payment"),
    ], pagination={"total_pages": 1})})

    summary = await client.get_financial_summary(CTX, start=date(2026, 10, 1), end=date(2026, 10, 31))

    assert summary.total_expenses.amount == Decimal("150.00"), "the $900 card payment is not spending"
    assert summary.total_income.amount == Decimal("3000.00")
    assert summary.net_cash_flow.amount == Decimal("2850.00")


# --------------------------------------------------------------------- bills


def _bills_payload() -> dict:
    return envelope({
        "owner_user_id": USER,
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "selected_date": "2026-10-14",
        "totals": {},
        "completed_bills": [
            {"owner_user_id": USER, "bill_id": "c1", "name": "CHECKCARD 1007 TMOBILE AUTO P BELLEVUE WA",
             "amount": money("162.14"), "due_date": "2026-10-07"},
        ],
        "upcoming_bills": [
            {"owner_user_id": USER, "bill_id": "u1", "name": "Mortgage", "amount": money("1598.54"),
             "due_date": "2026-10-15"},
            {"owner_user_id": USER, "bill_id": "u2", "name": "Electric", "amount": money("240.10"),
             "due_date": "2026-10-28"},
        ],
        # The same bill is listed under due_bills when selected_date matches.
        "due_bills": [
            {"owner_user_id": USER, "bill_id": "u1", "name": "Mortgage", "amount": money("1598.54"),
             "due_date": "2026-10-15"},
        ],
    })


@pytest.mark.asyncio
async def test_bills_are_paid_or_unpaid_by_section_and_listed_once():
    client, _ = make_client({"/internal/bills": _bills_payload()})

    bills = {b.bill_id: b for b in await client.get_bills(CTX)}

    assert set(bills) == {"c1", "u1", "u2"}
    assert bills["c1"].is_paid is True
    assert bills["u1"].is_paid is False and bills["u2"].is_paid is False


@pytest.mark.asyncio
async def test_bills_are_filtered_to_the_requested_window_client_side():
    client, _ = make_client({"/internal/bills": _bills_payload()})

    remaining = await client.get_bills(CTX, start=date(2026, 10, 14), end=date(2026, 10, 31))

    assert [b.bill_id for b in remaining] == ["u1", "u2"]
    assert all(not b.is_paid for b in remaining)


@pytest.mark.asyncio
async def test_a_window_outside_the_current_month_is_reported_as_unavailable_not_empty():
    client, _ = make_client({"/internal/bills": _bills_payload()})
    fs = FinancialService(client)
    plan = ExecutionPlan(
        intent=Intent.BILL_QUERY,
        requires_financial_data=True,
        requires_calculation=False,
        date_range=ResolvedDateRange(start=date(2099, 1, 1), end=date(2099, 1, 31), label="custom"),
    )

    result = await get_bills_tool(fs, make_user_context(user_id=USER), plan)

    assert result["bill_count"] == 0
    assert "only available" in result["note"]


# ------------------------------------------------------- liabilities & others


@pytest.mark.asyncio
async def test_only_debts_are_liabilities_not_checking_or_brokerage_accounts():
    def liab(kind, balance):
        return {"owner_user_id": USER, "liability_id": kind, "name": kind, "type": kind,
                "balance": money(balance), "interest_rate": None, "minimum_payment": None}

    client, _ = make_client({"/internal/liabilities": envelope([
        liab("credit_card", "51150.94"), liab("home_equity", "41763.55"),
        liab("checking", "2862.82"), liab("brokerage", "9000.00"), liab("crypto_exchange", "10.00"),
    ])})

    kinds = {liability.type for liability in await client.get_liabilities(CTX)}

    assert kinds == {"credit_card", "home_equity"}


@pytest.mark.asyncio
async def test_subscriptions_drop_transfers_and_loan_payments_and_exact_repeats():
    def sub(sid, name, amount, cycle="monthly"):
        return {"owner_user_id": USER, "subscription_id": sid, "name": name, "amount": money(amount),
                "billing_cycle": cycle, "next_charge_date": None, "is_active": True}

    def rec(sid, category):
        return {"owner_user_id": USER, "recurring_id": sid, "name": "x", "amount": money("1.00"),
                "frequency": "monthly", "category": category, "next_expected_date": None}

    client, _ = make_client({
        "/internal/subscriptions": envelope([
            sub("a", "Amazon Prime", "14.99"),
            sub("b", "Amazon Prime", "14.99"),                       # same service re-streamed
            sub("c", "Online Banking payment to CRD 9216", "1999.84"),
            sub("d", "Transfer to savings", "500.00"),
            sub("e", "Netflix", "15.49"),
            sub("f", "Payroll", "2000.00", cycle="biweekly"),
            sub("g", "Mystery", "9.00", cycle="unknown"),
        ]),
        "/internal/recurring-transactions": envelope([
            rec("a", "ENTERTAINMENT"), rec("b", "ENTERTAINMENT"), rec("c", "LOAN_PAYMENTS"),
            rec("d", "TRANSFER_OUT"), rec("e", "ENTERTAINMENT"), rec("f", "INCOME"), rec("g", "GENERAL_SERVICES"),
        ]),
    })

    names = sorted(s.name for s in await client.get_subscriptions(CTX))

    assert names == ["Amazon Prime", "Netflix"]


@pytest.mark.asyncio
async def test_subscriptions_still_work_when_the_category_lookup_fails():
    class _Flaky(FakeRepo):
        async def get(self, path, *, params=None):
            if path == "/internal/recurring-transactions":
                raise BackendServiceError("down")
            return await super().get(path, params=params)

    repo = _Flaky({"/internal/subscriptions": envelope([{
        "owner_user_id": USER, "subscription_id": "e", "name": "Netflix", "amount": money("15.49"),
        "billing_cycle": "monthly", "next_charge_date": None, "is_active": True,
    }])})

    subs = await BackendAPIClient(repo).get_subscriptions(CTX)

    assert [s.name for s in subs] == ["Netflix"]


# ------------------------------------------------------------ scores & goals


def _allo(score=54, end="2026-10-13") -> dict:
    return {"owner_user_id": USER, "snapshot_id": "1", "overall_score": score, "overall_tier": "Needs Improvement",
            "overall_insight": "i", "flow_score": 46, "flow_tier": "Needs Improvement", "flow_insight": "f",
            "stability_score": 81, "stability_tier": "Excellent", "stability_insight": "s",
            "retirement_score": 20, "retirement_tier": "Poor", "retirement_insight": "r",
            "period_start": "2026-10-01", "period_end": end, "as_of_date": end}


@pytest.mark.asyncio
async def test_allo_score_and_history_are_mapped_newest_first():
    client, _ = make_client({"/internal/allo-score": envelope(
        {"current": _allo(54), "history": [_allo(60, "2026-08-31"), _allo(54, "2026-10-13"), _allo(57, "2026-09-30")]}
    )})

    current = await client.get_allo_score(CTX)
    history = await client.get_allo_score_history(CTX)

    assert current.overall_score == 54 and current.period_end == date(2026, 10, 13)
    assert [h.period_end for h in history] == [date(2026, 10, 13), date(2026, 9, 30), date(2026, 8, 31)]


@pytest.mark.asyncio
async def test_the_credit_score_is_the_bureau_score_not_the_allo_score():
    client, repo = make_client({"/internal/credit-score-history": envelope({
        "owner_user_id": USER,
        "current": {"owner_user_id": USER, "score_id": "58", "report_date": "2026-09-24",
                    "provider": "EFX", "score": 779, "score_model": "VANTAGE"},
        "history": [],
    })})

    score = await client.get_financial_score(CTX)

    assert score.score == 779 and (score.score_range_min, score.score_range_max) == (300, 850)
    assert score.as_of_date == date(2026, 9, 24)
    assert repo.calls[0][1]["limit"] == 1, "the full history payload is ~800 KB and takes 13-19 s"


@pytest.mark.asyncio
async def test_the_newest_savings_goal_is_used():
    def goal(gid, total):
        return {"owner_user_id": USER, "goal_id": gid, "total_goal": money(total), "monthly_goal": money("100.00"),
                "start_date": "2026-09-09", "end_date": "2027-09-09"}

    client, _ = make_client({"/internal/savings-goals": envelope(
        {"owner_user_id": USER, "total_goal": money("20000.00"), "goals": [goal("12", "5000.00"), goal("44", "20000.00")]}
    )})

    result = await client.get_savings_goal(CTX)

    assert result.goal_id == "44" and result.target_amount.amount == Decimal("20000.00")


@pytest.mark.asyncio
async def test_liability_history_sums_account_snapshots_by_date():
    def snap(sid, when, balance):
        return {"owner_user_id": USER, "snapshot_id": sid, "account_id": sid, "name": sid, "type": "credit_card",
                "snapshot_date": when, "balance": money(balance)}

    client, _ = make_client({"/internal/liability-history": envelope({
        "owner_user_id": USER,
        "snapshots": [snap("a", "2026-10-01", "100.00"), snap("b", "2026-10-01", "50.00"),
                      snap("a", "2026-09-01", "170.00")],
        "monthly_history": [],
    })})

    history = await client.get_liability_history(CTX)

    assert [(h.snapshot_date, h.balance.amount) for h in history] == [
        (date(2026, 10, 1), Decimal("150.00")),
        (date(2026, 9, 1), Decimal("170.00")),
    ]


@pytest.mark.asyncio
async def test_retirement_metrics_are_read_from_raw_metrics_with_profile_fallback():
    client, _ = make_client({"/internal/retirement-readiness": envelope({
        "owner_user_id": USER, "score": 20, "tier": "Poor",
        "profile": {"age": 34, "target_retirement_age": 70, "monthly_retirement_income_goal": money("5000.00")},
        "raw_metrics": {"years_remaining": 36, "retirement_target": 1500000, "future_assets": -716979.69,
                        "savings_rate": -0.061633, "readiness_ratio": -0.477986},
    })})

    r = await client.get_retirement_readiness(CTX)

    assert (r.age, r.target_retirement_age, r.years_remaining) == (34, 70, 36)
    assert r.future_assets.amount == Decimal("-716979.69"), "a negative projection is reported, not clamped"
    assert r.monthly_retirement_income_goal.amount == Decimal("5000.00")


# ----------------------------------------------------------------- net worth


@pytest.mark.asyncio
async def test_net_worth_reports_the_backends_figure_with_its_breakdown():
    client, _ = make_client({"/internal/net-worth": envelope({
        "owner_user_id": USER, "as_of_date": "2026-10-13",
        "net_worth": money("405836.91"), "total_assets": money("498751.40"),
        "total_liabilities": money("92914.49"),
        "asset_breakdown": {"business": money("432000.00"), "property": money("0.00")},
    })})

    result = await get_net_worth_tool(
        FinancialService(client), make_user_context(user_id=USER),
        ExecutionPlan(intent=Intent.NET_WORTH_QUERY, requires_financial_data=True, requires_calculation=True),
    )

    assert result["source"] == "backend"
    assert result["net_worth"] == "405836.91"
    assert result["asset_breakdown"]["business"] == "432000.00"


# ----------------------------------------------------------------- isolation


@pytest.mark.asyncio
async def test_another_users_rows_abort_the_request():
    from app.core.exceptions import CrossUserDataError

    client, _ = make_client({"/internal/transactions": envelope(
        [tx(1, "10.00"), tx(2, "20.00", owner="999")], pagination={"total_pages": 1}
    )})

    with pytest.raises(CrossUserDataError):
        await FinancialService(client).get_transactions(CTX)


# ------------------------------------------------- forward-looking bills


def test_this_month_reaches_forward_to_month_end_for_bills_only():
    from app.tools.bills import _forward_looking_end

    today = date(2026, 10, 13)
    # The resolver clips "this month" at today; for bills that hides what is due.
    assert _forward_looking_end(date(2026, 10, 1), today, "this_month", today) == date(2026, 10, 31)
    assert _forward_looking_end(date(2026, 10, 1), today, "2026-10", today) == date(2026, 10, 31)
    assert _forward_looking_end(date(2026, 10, 12), today, "this_week", today) == date(2026, 10, 18)
    # Left alone: "today", an explicit range, and a month that is already over.
    assert _forward_looking_end(today, today, "today", today) == today
    assert _forward_looking_end(date(2026, 10, 5), date(2026, 10, 9), "custom", today) == date(2026, 10, 9)
    assert _forward_looking_end(date(2026, 9, 1), date(2026, 9, 30), "2026-09", today) == date(2026, 9, 30)


@pytest.mark.asyncio
async def test_a_window_past_month_end_is_completed_from_selected_date():
    seen_days: list[str] = []

    def route(params):
        if "selected_date" not in params:
            return _bills_payload()
        seen_days.append(params["selected_date"])
        day = params["selected_date"]
        due = [{"owner_user_id": USER, "bill_id": "nov-rent", "name": "Rent", "amount": money("1500.00"),
                "due_date": day}] if day == "2026-11-01" else []
        return envelope({"owner_user_id": USER, "period_start": "2026-10-01", "period_end": "2026-10-31",
                         "completed_bills": [], "upcoming_bills": [], "due_bills": due})

    client, _ = make_client({"/internal/bills": route})

    bills = await client.get_bills(CTX, start=date(2026, 10, 25), end=date(2026, 11, 3))

    assert sorted(seen_days) == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert [(b.name, b.due_date) for b in bills] == [
        ("Electric", date(2026, 10, 28)),
        ("Rent", date(2026, 11, 1)),
    ]


@pytest.mark.asyncio
async def test_two_occurrences_of_one_stream_in_a_month_are_both_kept():
    payload = envelope({
        "owner_user_id": USER, "period_start": "2026-10-01", "period_end": "2026-10-31",
        "completed_bills": [],
        "upcoming_bills": [
            {"owner_user_id": USER, "bill_id": "stream-1", "name": "Payroll tax", "amount": money("100.00"),
             "due_date": "2026-10-16"},
            {"owner_user_id": USER, "bill_id": "stream-1", "name": "Payroll tax", "amount": money("100.00"),
             "due_date": "2026-10-30"},
        ],
        "due_bills": [],
    })
    client, _ = make_client({"/internal/bills": payload})

    bills = await client.get_bills(CTX)

    assert len(bills) == 2


# ---------------------------------------------------- tool-level regressions


@pytest.mark.asyncio
async def test_cash_is_not_inflated_by_credit_card_balances():
    """$66k in checking and $51k owed on cards once read as 'you have $117k'."""
    from app.tools.accounts import get_accounts_tool

    def acct(aid, kind, balance):
        return {"owner_user_id": USER, "account_id": aid, "name": aid, "type": kind,
                "balance": money(balance), "institution_name": "Bank", "is_active": True}

    client, _ = make_client({"/internal/accounts": envelope([
        acct("chk", "checking", "66420.34"), acct("card", "credit", "51217.57"), acct("asset", "manually", "320.00"),
    ])})

    result = await get_accounts_tool(
        FinancialService(client), make_user_context(user_id=USER),
        ExecutionPlan(intent=Intent.BALANCE_QUERY, requires_financial_data=True, requires_calculation=False),
    )

    assert result["total_cash"] == "66420.34"
    assert result["total_owed_on_credit_accounts"] == "51217.57"
    assert result["other_accounts_total"] == "320.00"
    assert "total_balance" not in result, "a single all-accounts total is the bug"


@pytest.mark.asyncio
async def test_savings_progress_is_the_backends_figure_not_a_rederivation():
    from app.tools.savings_goal import get_savings_goal_tool

    today = date.today().isoformat()
    client, repo = make_client({"/internal/savings-goals": envelope({
        "owner_user_id": USER,
        "total_saving": money("260.05"),
        "goals": [{"owner_user_id": USER, "goal_id": "44", "total_goal": money("20000.00"),
                   "monthly_goal": money("1666.67"), "start_date": "2026-09-09", "end_date": "2099-09-09"}],
    })})

    result = await get_savings_goal_tool(
        FinancialService(client), make_user_context(user_id=USER),
        ExecutionPlan(intent=Intent.SAVINGS_GOAL_QUERY, requires_financial_data=True, requires_calculation=True),
    )

    assert result["progress_saved"] == "260.05"
    assert result["progress_source"] == "backend"
    assert [p for p, _ in repo.calls] == ["/internal/savings-goals"], "no transaction fetch was needed"
    assert today  # keeps the date import honest across midnight


@pytest.mark.asyncio
async def test_ach_autopays_are_bills_not_subscriptions():
    """Payroll runs, payroll tax and utility autopays recur, but are not subscriptions."""
    def sub(sid, name, amount):
        return {"owner_user_id": USER, "subscription_id": sid, "name": name, "amount": money(amount),
                "billing_cycle": "monthly", "next_charge_date": None, "is_active": True}

    client, _ = make_client({
        "/internal/subscriptions": envelope([
            sub("a", "ACME PAYROLL DES:TAX 000001 ID:A1B2C3 INDN:EXAMPLE CO ID:XXXXX00000 CCD", "1367.45"),
            sub("b", "CITY POWER DES:NPC PYMT ID:XXXXXXXXXX00000 INDN:JANE DOE", "240.10"),
            sub("c", "OPENAI *CHATGPT SUBSCR", "20.00"),
        ]),
        "/internal/recurring-transactions": envelope([]),
    })

    names = [s.name for s in await client.get_subscriptions(CTX)]

    assert len(names) == 1 and "chatgpt" in names[0].lower()


# ------------------------------------------- QA workbook regressions (round 2)


def _manual(record_id, signed, direction, primary, name="typed in"):
    row = tx(record_id, signed, primary=primary, name=name)
    row.update(is_plaid=False, direction=direction, transaction_id=None,
               external_or_manual_id=f"manual-{record_id}")
    return row


@pytest.mark.asyncio
async def test_hand_typed_rows_follow_the_category_rule_not_the_sign():
    """
    A $96,358 'income' typed in by hand is stored POSITIVE, and a $100,000
    expense typed in by hand is stored NEGATIVE. Read by sign they became the
    user's largest purchase and their best savings month.
    """
    imported_refund = tx(3, "-12.99", primary="GENERAL_MERCHANDISE", name="Amazon refund")
    imported_refund["is_plaid"] = True
    client, _ = make_client({"/internal/transactions": envelope([
        _manual(1, "96358.00", "inflow", "INCOME", name="Addd"),
        _manual(2, "-100000.00", "outflow", "ENTERTAINMENT", name="spending efficiency"),
        imported_refund,
    ], pagination={"total_pages": 1})})

    rows = {t.merchant_name: t for t in await client.get_transactions(CTX)}

    assert rows["Addd"].amount == Decimal("-96358.00"), "typed-in income is money IN"
    assert rows["spending efficiency"].amount == Decimal("100000.00"), "typed-in expense is money OUT"
    assert rows["Amazon refund"].amount == Decimal("-12.99"), "an imported refund keeps Plaid's sign"


def test_rest_of_the_month_resolves_forward_to_month_end():
    from app.utils.dates import current_date_in, resolve_date_phrase

    today = current_date_in("America/Los_Angeles")
    for phrase in ("rest of the month", "the rest of this month", "remainder of the month",
                   "until the end of the month"):
        r = resolve_date_phrase(phrase, "America/Los_Angeles")
        assert r is not None, phrase
        assert r.start == today and r.end.month == today.month and r.label == "rest_of_month"
        assert (r.end.replace(day=28) + __import__("datetime").timedelta(days=4)).month != r.end.month or r.end.day >= 28


@pytest.mark.asyncio
async def test_a_future_period_is_not_answered_with_zero():
    from datetime import timedelta

    from app.orchestrator.intent_router import route_and_execute
    from app.utils.dates import current_date_in

    today = current_date_in("UTC")
    plan = ExecutionPlan(
        intent=Intent.SPENDING_ANALYSIS, requires_financial_data=True, requires_calculation=True,
        date_range=ResolvedDateRange(start=today + timedelta(days=4), end=today + timedelta(days=5), label="this_weekend"),
    )
    client, repo = make_client({"/internal/transactions": envelope([], pagination={"total_pages": 1})})

    result, tools = await route_and_execute(FinancialService(client), make_user_context(user_id=USER), plan)

    assert result["period_in_future"] is True
    assert tools == [] and repo.calls == [], "no data is fetched for a period that has not happened"


@pytest.mark.asyncio
async def test_a_filtered_income_question_never_answers_with_total_income():
    from app.tools.income import get_income_tool
    from app.utils.dates import current_date_in

    today = current_date_in("UTC")
    plan = ExecutionPlan(
        intent=Intent.INCOME_QUERY, requires_financial_data=True, requires_calculation=True,
        category_filter="dividend",
        date_range=ResolvedDateRange(start=today.replace(month=1, day=1), end=today, label="this_year"),
    )
    payroll = tx(1, "-5000.00", when=today.isoformat(), primary="INCOME", name="Payroll")
    client, _ = make_client({"/internal/transactions": envelope([payroll], pagination={"total_pages": 1})})

    result = await get_income_tool(FinancialService(client), make_user_context(user_id=USER), plan)

    assert result["transaction_count"] == 0 and "none are recorded" in result["note"]
    assert result["total_income"] != "5000.00"


@pytest.mark.asyncio
async def test_debt_progress_compares_against_the_snapshot_before_the_latest():
    from app.tools.liabilities import get_liabilities_tool

    def snap(sid, when, balance):
        return {"owner_user_id": USER, "snapshot_id": sid, "account_id": "a", "name": "Card", "type": "credit_card",
                "snapshot_date": when, "balance": money(balance)}

    client, _ = make_client({
        "/internal/liabilities": envelope([{"owner_user_id": USER, "liability_id": "a", "name": "Card",
                                            "type": "credit_card", "balance": money("92914.49")}]),
        "/internal/liability-history": envelope({"owner_user_id": USER, "monthly_history": [], "snapshots": [
            snap("2", "2026-10-01", "92914.49"), snap("1", "2026-09-01", "94564.98")]}),
    })

    result = await get_liabilities_tool(
        FinancialService(client), make_user_context(user_id=USER),
        ExecutionPlan(intent=Intent.LIABILITY_QUERY, requires_financial_data=True, requires_calculation=False),
    )

    assert result["trend"]["previous_snapshot_date"] == "2026-09-01"
    assert result["trend"]["change"] == "-1650.49", "debt went down by $1,650.49"
    assert "year_to_date_note" in result["trend"], "history does not reach January and says so"


@pytest.mark.asyncio
async def test_investment_answers_state_what_the_data_cannot_do():
    from app.tools.investments import get_investments_tool

    client, _ = make_client({
        "/internal/investments": envelope([{
            "owner_user_id": USER, "holding_id": "1", "account_id": "a", "symbol": "TSLA", "name": "Tesla",
            "quantity": "1.0000", "market_value": money("250.00"), "cost_basis": money("249.00")}]),
        "/internal/retirement-readiness": envelope({
            "owner_user_id": USER, "score": 20, "tier": "Poor", "profile": {},
            "raw_metrics": {"investment_contributions": 331.06, "investment_contribution_months": 5}}),
    })

    result = await get_investments_tool(
        FinancialService(client), make_user_context(user_id=USER),
        ExecutionPlan(intent=Intent.INVESTMENT_QUERY, requires_financial_data=True, requires_calculation=False),
    )

    assert result["contributions"]["total_contributed"] == "331.06"
    assert any("benchmark" in line.lower() for line in result["limitations"])
    assert any("cumulative" in line.lower() for line in result["limitations"])


@pytest.mark.asyncio
async def test_a_dropped_connection_on_a_read_is_retried_once_it_recovers():
    import httpx

    from app.repositories.base import HttpRepository

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("connection dropped", request=request)
        return httpx.Response(200, json={"status": 200, "data": [], "meta": {}})

    class _Scripted(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return handler(request)

    repo = HttpRepository(base_url="https://x/api", timeout_seconds=5, transport=_Scripted())

    assert await repo.get("/internal/accounts", params={"user_id": "1"}) == {"status": 200, "data": [], "meta": {}}
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_a_wide_window_is_fetched_one_calendar_month_per_request():
    """Latency is per request, so three parallel month requests beat two sequential pages."""
    def route(params):
        month = params["start"][:7]
        row = tx(month.replace("-", ""), "10.00", when=f"{month}-15")
        return envelope([row], pagination={"page": params["page"], "total_pages": 1})

    client, repo = make_client({"/internal/transactions": route})

    rows = await client.get_transactions(CTX, start=date(2026, 7, 10), end=date(2026, 10, 5))

    windows = sorted((p["start"], p["end"]) for _, p in repo.calls)
    assert windows == [
        ("2026-07-10", "2026-07-31"), ("2026-08-01", "2026-08-31"),
        ("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-05"),
    ]
    assert len(rows) == 4 and sum(t.amount for t in rows) == Decimal("40.00")


@pytest.mark.asyncio
async def test_main_job_is_a_source_not_a_filter():
    from app.tools.income import get_income_tool

    plan = ExecutionPlan(
        intent=Intent.INCOME_QUERY, requires_financial_data=True, requires_calculation=True,
        merchant_filter="main job",
        date_range=ResolvedDateRange(start=date(2026, 9, 1), end=date(2026, 9, 30), label="last_month"),
    )
    payroll = tx(1, "-7080.00", when="2026-09-18", primary="INCOME", name="AH4R LP")
    client, _ = make_client({"/internal/transactions": envelope([payroll], pagination={"total_pages": 1})})

    result = await get_income_tool(FinancialService(client), make_user_context(user_id=USER), plan)

    assert result["total_income"] == "7080.00"
    assert result["primary_source"]["total"] == "7080.00"


@pytest.mark.asyncio
async def test_bill_totals_are_computed_by_the_service_not_summed_by_the_model():
    from app.tools.bills import get_bills_tool

    client, _ = make_client({"/internal/bills": _bills_payload()})
    plan = ExecutionPlan(intent=Intent.BILL_QUERY, requires_financial_data=True, requires_calculation=False)

    result = await get_bills_tool(FinancialService(client), make_user_context(user_id=USER), plan)

    assert result["unpaid_total"] == "1838.64"      # 1598.54 + 240.10
    assert result["paid_total"] == "162.14"
    assert result["total"] == "2000.78"
    assert result["largest_bill"]["name"] == "Mortgage"


# ------------------------------------------------------------- isolation


@pytest.mark.asyncio
async def test_an_unlabeled_record_is_not_stamped_with_the_requested_user():
    """Stamping it ourselves would make the cross-user check compare a request with itself."""
    row = tx(1, "10.00")
    del row["owner_user_id"]
    client, _ = make_client({"/internal/transactions": envelope([row], pagination={"total_pages": 1})})

    [record] = await client.get_transactions(CTX)

    assert record.owner_user_id is None


@pytest.mark.asyncio
async def test_strict_ownership_rejects_an_unlabeled_record(monkeypatch):
    from app.core import data_isolation
    from app.core.exceptions import CrossUserDataError

    monkeypatch.setattr(data_isolation.settings, "strict_data_ownership", True)
    row = tx(1, "10.00")
    del row["owner_user_id"]
    client, _ = make_client({"/internal/transactions": envelope([row], pagination={"total_pages": 1})})

    with pytest.raises(CrossUserDataError):
        await FinancialService(client).get_transactions(CTX)


@pytest.mark.asyncio
async def test_a_profile_for_a_different_user_is_refused():
    from app.core.exceptions import CrossUserDataError

    client, _ = make_client({"/internal/users/profile": envelope(
        {"user_id": "999", "name": "Someone Else", "currency": "USD", "timezone": "UTC"}
    )})

    with pytest.raises(CrossUserDataError):
        await client.get_user_profile(CTX)


@pytest.mark.asyncio
async def test_every_request_carries_only_the_authenticated_users_id():
    """No code path may ask the backend about any id except the one in the signed token."""
    client, repo = make_client({
        "/internal/accounts": envelope([]), "/internal/bills": _bills_payload(),
        "/internal/subscriptions": envelope([]), "/internal/recurring-transactions": envelope([]),
        "/internal/investments": envelope([]), "/internal/liabilities": envelope([]),
        "/internal/transactions": envelope([], pagination={"total_pages": 1}),
    })
    ctx = make_user_context(user_id="42")

    await client.get_accounts(ctx)
    await client.get_bills(ctx)
    await client.get_subscriptions(ctx)
    await client.get_investments(ctx)
    await client.get_liabilities(ctx)
    await client.get_transactions(ctx, start=date(2026, 7, 1), end=date(2026, 10, 14))

    assert repo.calls, "nothing was requested"
    assert {params["user_id"] for _, params in repo.calls} == {"42"}
