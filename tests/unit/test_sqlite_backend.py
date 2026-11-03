"""
Tests for the read-only SQLite backend client.

These run against a small fixture database built in the same shape as the
real dump, so they need no 177 MB file and no network. What they guard:

  - user scoping on every method, because this client is the one that talks
    to a database holding many users' real finances;
  - the sign convention, which the schema does not enforce;
  - that rows the database contains are not silently dropped -- a
    manually-entered transaction has no Plaid id, and skipping those made the
    reported totals disagree with the database by exactly the amounts a user
    had typed in.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from app.services.sqlite_backend_client import SqliteBackendClient
from app.utils.transactions import expense_amount, income_amount
from tests.conftest import make_user_context

SCHEMA = """
CREATE TABLE application_users (id INTEGER, Name TEXT, email TEXT, country TEXT, created_at TEXT);
CREATE TABLE plaid_accounts (
    account_id TEXT, name TEXT, avaliable_balance REAL, current_balance REAL,
    mask TEXT, type TEXT, subtype TEXT, user_id INTEGER);
CREATE TABLE plaid_transactions (
    id INTEGER, is_plaid INTEGER, transaction_id TEXT, account_id TEXT, name TEXT,
    amount REAL, transaction_date TEXT, plaid_category_id INTEGER, pending TEXT,
    user_id INTEGER, is_recurring INTEGER, is_deleted INTEGER, is_review_transfer INTEGER,
    frequency TEXT, due_date TEXT);
CREATE TABLE plaid_categories (id INTEGER, display_name TEXT, primary_name TEXT, detailed_name TEXT);
CREATE TABLE plaid_recurring_streams (
    user_id INTEGER, stream_id TEXT, plaid_category_id INTEGER, account_id TEXT,
    amount REAL, frequency TEXT, last_payment_date TEXT, next_payment_date TEXT,
    active INTEGER, description TEXT);
CREATE TABLE credit_scores (
    user_id INTEGER, report_date TEXT, provider TEXT, score INTEGER,
    score_model TEXT, score_type TEXT);
CREATE TABLE investment_items (id INTEGER, user_id INTEGER, item_id TEXT, institution_name TEXT);
CREATE TABLE investment_accounts (id INTEGER, item_id INTEGER, account_id TEXT, name TEXT);
CREATE TABLE investment_holdings (
    id INTEGER, account_id INTEGER, security_id TEXT, quantity REAL,
    institution_value REAL, cost_basis REAL);
CREATE TABLE investment_securities (id INTEGER, security_id TEXT, name TEXT, ticker_symbol TEXT);
CREATE TABLE liability_accounts (
    id INTEGER, user_id INTEGER, account_id TEXT, name TEXT, type TEXT,
    subtype TEXT, balance REAL);
CREATE TABLE liability_credit (
    id INTEGER, user_id INTEGER, account_id TEXT, minimum_payment_amount REAL,
    is_overdue INTEGER, next_payment_due_date TEXT, last_payment_date TEXT);
CREATE TABLE liability_loans (id INTEGER, user_id INTEGER, account_id TEXT, interest_rate_percentage REAL);
CREATE TABLE liability_mortgages (id INTEGER, user_id INTEGER, account_id TEXT, interest_rate REAL);
CREATE TABLE budgets (id INTEGER, category_id INTEGER, user_id INTEGER, amount REAL, status INTEGER);
CREATE TABLE allo_score_snapshots (
    id INTEGER, user_id INTEGER,
    overall_score INTEGER, overall_tier TEXT, overall_insight TEXT,
    flow_score INTEGER, flow_tier TEXT, flow_insight TEXT,
    stability_score INTEGER, stability_tier TEXT, stability_insight TEXT,
    retirement_score INTEGER, retirement_tier TEXT, retirement_insight TEXT,
    calculation_period_start TEXT, calculation_period_end TEXT, raw_metrics TEXT);
CREATE TABLE save_goals (
    id INTEGER, total_saving_amount REAL, monthly_saving_amount REAL,
    user_id INTEGER, start_date TEXT, end_date TEXT);
CREATE TABLE liability_snapshots (
    id INTEGER, user_id INTEGER, account_id TEXT, account_type TEXT,
    balance REAL, last_payment_amount REAL, last_payment_date TEXT,
    snapshot_date TEXT);
"""


def _txn(tid, uid, amount, day, *, category=1, deleted=0, transfer=0, name="MERCHANT", pk=None):
    return (pk or tid, 1, tid, "acct-1", name, amount, f"2026-07-{day:02d}", category,
            "0", uid, 0, deleted, transfer, None, None)


@pytest.fixture
def db(tmp_path):
    """
    Two users with deliberately different finances, plus the awkward rows the
    real database actually contains: a deleted row, an internal transfer, and
    a manual entry with no Plaid transaction id.
    """
    path = tmp_path / "fixture.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO application_users VALUES (?,?,?,?,?)",
        [(1, "Alice", "a@example.com", "US", None), (2, "Bob", "b@example.com", "US", None)],
    )
    conn.executemany(
        "INSERT INTO plaid_accounts VALUES (?,?,?,?,?,?,?,?)",
        [
            ("acct-1", "Alice Checking", 100.0, 250.0, "1111", "depository", "checking", 1),
            ("acct-2", "Alice Card", None, 900.0, "2222", "credit", "credit card", 1),
            ("acct-9", "Bob Checking", 10.0, 20.0, "9999", "depository", "checking", 2),
        ],
    )
    conn.executemany(
        "INSERT INTO plaid_categories VALUES (?,?,?,?)",
        [
            (1, "FOOD_AND_DRINK", "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
            (2, "TRANSFER_IN", "TRANSFER_IN", "TRANSFER_IN_DEPOSIT"),
        ],
    )
    conn.executemany(
        "INSERT INTO plaid_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _txn("t1", 1, 100.0, 5),                      # Alice: outflow
            _txn("t2", 1, 50.0, 6),                       # Alice: outflow
            _txn("t3", 1, -500.0, 7, category=2),         # Alice: inflow
            _txn("t4", 1, 999.0, 8, deleted=1),           # deleted: must not count
            # The review flag means "check whether this is an internal
            # transfer", not "this is one". Both readings are exercised:
            _txn("t5", 1, 777.0, 9, category=2, transfer=1),   # flagged AND TRANSFER_IN: excluded
            _txn("t8", 1, 60.0, 11, category=1, transfer=1),   # flagged but FOOD_AND_DRINK: real spending
            _txn(None, 1, 25.0, 10, name="a manual transaction", pk=6001),  # no Plaid id
            _txn("t7", 2, 8000.0, 5),                     # Bob: must never reach Alice
        ],
    )
    conn.executemany(
        "INSERT INTO plaid_recurring_streams VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "s1", 1, "acct-1", 15.0, "MONTHLY", "2026-07-01", "2026-08-01", 1, "Netflix"),
            (1, "s2", 1, "acct-1", -2000.0, "MONTHLY", "2026-07-01", "2026-08-01", 1, "Payroll"),
            (1, "s3", 1, "acct-1", 40.0, "ANNUALLY", "2026-01-01", "2027-01-01", 1, "Domain"),
            (2, "s9", 1, "acct-9", 99.0, "MONTHLY", "2026-07-01", "2026-08-01", 1, "Bob Only"),
        ],
    )
    conn.executemany(
        "INSERT INTO credit_scores VALUES (?,?,?,?,?,?)",
        [(1, "2026-01-01", "Equifax", 700, "VantageScore", None),
         (1, "2026-07-01", "Equifax", 742, "VantageScore", None)],
    )
    conn.execute("INSERT INTO investment_items VALUES (500, 1, 'item-1', 'Test Brokerage')")
    conn.execute("INSERT INTO investment_accounts VALUES (600, 500, 'inv-acct-1', 'Brokerage')")
    conn.execute(
        "INSERT INTO investment_holdings VALUES (700, 600, 'sec-1', 10.5, 1050.0, 900.0)"
    )
    conn.execute("INSERT INTO investment_securities VALUES (1, 'sec-1', 'Test Fund', 'TST')")
    conn.executemany(
        "INSERT INTO liability_accounts VALUES (?,?,?,?,?,?,?)",
        [
            # A real liability: a credit card.
            (800, 1, "liab-2", "Alice Visa", "credit", "credit card", 900.0),
            # NOT a liability -- a checking account on the same liability-
            # bearing Item. Must be excluded, not reported as debt.
            (801, 1, "liab-3", "Alice Checking (liability item)", "depository", "checking", 250.0),
        ],
    )
    conn.execute("INSERT INTO liability_credit VALUES (900, 1, 'liab-2', 45.0, 1, '2026-08-05', '2026-07-01')")
    conn.execute("INSERT INTO save_goals VALUES (1, 5000.0, 500.0, 1, '2026-01-01', '2026-12-31')")
    conn.executemany(
        "INSERT INTO liability_snapshots VALUES (?,?,?,?,?,?,?,?)",
        [
            # Two accounts snapshotted on the same date -- history should
            # report the SUMMED total for that date, not one row per account.
            (1, 1, 'liab-2', 'credit', 950.0, 40.0, '2026-06-01', '2026-07-01'),
            (2, 1, 'liab-other', 'loan', 50.0, None, None, '2026-07-01'),
            (3, 1, 'liab-2', 'credit', 1200.0, 40.0, '2025-12-15', '2026-01-01'),
        ],
    )
    conn.executemany(
        "INSERT INTO allo_score_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, 1, 50, "Needs Improvement", "early", 40, "Weak", None, 55, "Moderate", None,
             45, "At Risk", None, "2025-07-01", "2026-01-01", '{"age": 30}'),
            (2, 1, 65, "Moderate", "later, so this one wins", 60, "Moderate", None, 70, "Strong", None,
             50, "Moderate", None, "2026-01-01", "2026-07-01",
             '{"age": 34, "target_retirement_age": 64, "years_remaining": 30, '
             '"retirement_target": 900000, "future_assets": 450000, '
             '"monthly_retirement_income_goal": 3000, "annual_contribution": 12000, '
             '"savings_rate": 0.05, "investment_rate": 0.04, "readiness_ratio": 0.5}'),
            # A user whose snapshot carries malformed JSON: must yield None, not raise.
            (3, 3, 50, "Moderate", None, 50, "Moderate", None, 50, "Moderate", None,
             50, "Moderate", None, "2026-01-01", "2026-07-01", "{not valid json"),
        ],
    )
    conn.commit()
    conn.close()
    return SqliteBackendClient(path)


JULY = {"start": date(2026, 7, 1), "end": date(2026, 7, 31)}


@pytest.mark.asyncio
async def test_a_user_sees_only_their_own_transactions(db):
    alice = await db.get_transactions(make_user_context(user_id="1"), **JULY)
    assert {t.transaction_id for t in alice} == {"t1", "t2", "t3", "t8", "manual-6001"}
    assert all(t.owner_user_id == "1" for t in alice)

    bob = await db.get_transactions(make_user_context(user_id="2"), **JULY)
    assert {t.transaction_id for t in bob} == {"t7"}


@pytest.mark.asyncio
async def test_a_manual_transaction_is_counted(db):
    """
    No Plaid id is not the same as no transaction. Skipping these made the
    totals disagree with the database by exactly what the user had typed in.
    """
    expenses = await db.get_expenses(make_user_context(user_id="1"), **JULY)
    total = sum(expense_amount(t) for t in expenses)
    assert total == Decimal("235.0"), "100 + 50 + the 25 entered by hand + the 60 flagged for review"
    assert any(t.transaction_id == "manual-6001" for t in expenses)


@pytest.mark.asyncio
async def test_deleted_rows_and_internal_transfers_are_excluded(db):
    """
    The review flag is a question; the category answers it.

    Excluding every flagged row dropped 1,341 ordinary purchases from the real
    dump -- 438 of them food and drink -- and understated one demo user's food
    spending by 45%. Named merchants disappeared with them: KFC and Cheesecake
    Summerlin were in the database and reported as "no transactions found".
    """
    transactions = await db.get_transactions(make_user_context(user_id="1"), **JULY)
    ids = {t.transaction_id for t in transactions}
    assert "t4" not in ids, "a deleted row was counted"
    assert "t5" not in ids, "a flagged row categorised TRANSFER_IN was counted as spending"
    assert "t8" in ids, "a flagged row categorised FOOD_AND_DRINK is a purchase, not a transfer"


@pytest.mark.asyncio
async def test_income_and_expenses_follow_the_plaid_sign_convention(db):
    context = make_user_context(user_id="1")
    income = await db.get_income(context, **JULY)
    expenses = await db.get_expenses(context, **JULY)

    assert [t.transaction_id for t in income] == ["t3"]
    assert sum(income_amount(t) for t in income) == Decimal("500.0")
    assert all(t.amount > 0 for t in expenses), "an outflow is positive in Plaid data"


@pytest.mark.asyncio
async def test_the_summary_reports_magnitudes(db):
    summary = await db.get_financial_summary(make_user_context(user_id="1"), **JULY)
    assert summary.total_income.amount == Decimal("500.0")
    assert summary.total_expenses.amount == Decimal("235.0")
    assert summary.net_cash_flow.amount == Decimal("265.0")
    assert summary.owner_user_id == "1"


@pytest.mark.asyncio
async def test_categories_are_readable(db):
    transactions = await db.get_transactions(make_user_context(user_id="1"), **JULY)
    assert {t.category for t in transactions} == {"Food And Drink", "Transfer In"}


@pytest.mark.asyncio
async def test_a_category_filter_matches_what_the_user_was_shown(db):
    matched = await db.get_transactions(make_user_context(user_id="1"), category="Food And Drink", **JULY)
    assert {t.transaction_id for t in matched} == {"t1", "t2", "t8", "manual-6001"}


@pytest.mark.asyncio
async def test_accounts_and_liabilities_are_scoped_and_typed(db):
    accounts = await db.get_accounts(make_user_context(user_id="1"))
    assert {a.account_id for a in accounts} == {"acct-1", "acct-2"}
    assert {a.type for a in accounts} == {"checking", "credit"}

    liabilities = await db.get_liabilities(make_user_context(user_id="1"))
    assert [liability.liability_id for liability in liabilities] == ["liab-2"]
    assert liabilities[0].type == "credit_card"
    assert liabilities[0].minimum_payment.amount == Decimal("45.0")
    assert liabilities[0].is_overdue is True
    assert liabilities[0].next_payment_due_date == date(2026, 8, 5)

    assert await db.get_accounts(make_user_context(user_id="2")) != accounts
    assert await db.get_liabilities(make_user_context(user_id="2")) == []


@pytest.mark.asyncio
async def test_a_liability_item_checking_account_is_not_reported_as_debt(db):
    """
    liability_accounts holds every Plaid account on a liability-bearing Item,
    not just the debts -- a checking account living in that same table must
    not show up as a liability.
    """
    liabilities = await db.get_liabilities(make_user_context(user_id="1"))
    assert "liab-3" not in {liability.liability_id for liability in liabilities}


@pytest.mark.asyncio
async def test_investments_are_scoped_through_the_three_table_join(db):
    holdings = await db.get_investments(make_user_context(user_id="1"))
    assert len(holdings) == 1
    holding = holdings[0]
    assert holding.account_id == "inv-acct-1"
    assert holding.symbol == "TST"
    assert holding.name == "Test Fund"
    assert holding.quantity == Decimal("10.5")
    assert holding.market_value.amount == Decimal("1050.0")
    assert holding.cost_basis.amount == Decimal("900.0")
    assert holding.owner_user_id == "1"

    assert await db.get_investments(make_user_context(user_id="2")) == []


@pytest.mark.asyncio
async def test_a_stored_savings_goal_is_read(db):
    goal = await db.get_savings_goal(make_user_context(user_id="1"))
    assert goal is not None
    assert goal.target_amount.amount == Decimal("5000.0")
    assert goal.monthly_target.amount == Decimal("500.0")
    assert goal.start_date == date(2026, 1, 1)
    assert goal.end_date == date(2026, 12, 31)

    assert await db.get_savings_goal(make_user_context(user_id="2")) is None


@pytest.mark.asyncio
async def test_financial_score_history_is_ordered_newest_first(db):
    history = await db.get_financial_score_history(make_user_context(user_id="1"))
    assert [h.score for h in history] == [742, 700]
    assert await db.get_financial_score_history(make_user_context(user_id="2")) == []


@pytest.mark.asyncio
async def test_allo_score_history_is_ordered_newest_first(db):
    history = await db.get_allo_score_history(make_user_context(user_id="1"))
    assert [h.overall_score for h in history] == [65, 50]


@pytest.mark.asyncio
async def test_liability_history_is_grouped_and_summed_by_date(db):
    history = await db.get_liability_history(make_user_context(user_id="1"))
    assert [(h.snapshot_date, h.balance.amount) for h in history] == [
        (date(2026, 7, 1), Decimal("1000.0")),  # 950 + 50, same date, summed
        (date(2026, 1, 1), Decimal("1200.0")),
    ]
    assert await db.get_liability_history(make_user_context(user_id="2")) == []


@pytest.mark.asyncio
async def test_retirement_readiness_is_parsed_from_the_latest_snapshot(db):
    readiness = await db.get_retirement_readiness(make_user_context(user_id="1"))
    assert readiness is not None
    assert readiness.target_retirement_age == 64
    assert readiness.years_remaining == 30
    assert readiness.retirement_target.amount == Decimal("900000")
    assert readiness.future_assets.amount == Decimal("450000")
    assert readiness.readiness_ratio == Decimal("0.5")
    assert readiness.owner_user_id == "1"


@pytest.mark.asyncio
async def test_retirement_readiness_is_none_without_a_snapshot_or_with_bad_json(db):
    assert await db.get_retirement_readiness(make_user_context(user_id="2")) is None
    assert await db.get_retirement_readiness(make_user_context(user_id="3")) is None


@pytest.mark.asyncio
async def test_allo_score_is_separate_from_credit_score(db):
    """
    Two different, real scoring systems. The latest Allo Score snapshot wins,
    the same way the latest credit score does, and the two never collide.
    """
    context = make_user_context(user_id="1")
    allo = await db.get_allo_score(context)
    credit = await db.get_financial_score(context)

    assert allo is not None and credit is not None
    assert allo.overall_score == 65
    assert allo.overall_insight == "later, so this one wins"
    assert credit.score == 742
    assert allo.overall_score != credit.score

    assert await db.get_allo_score(make_user_context(user_id="2")) is None


@pytest.mark.asyncio
async def test_only_monthly_outflows_are_treated_as_subscriptions(db):
    subs = await db.get_subscriptions(make_user_context(user_id="1"))
    names = {s.name for s in subs}
    assert names == {"Netflix"}, "a paycheck is not a subscription, nor is an annual charge"


@pytest.mark.asyncio
async def test_bills_come_from_recurring_outflows_only(db):
    bills = await db.get_bills(make_user_context(user_id="1"))
    assert {b.name for b in bills} == {"Netflix", "Domain"}
    assert all(b.amount.amount > 0 for b in bills)


@pytest.mark.asyncio
async def test_the_latest_credit_score_wins(db):
    score = await db.get_financial_score(make_user_context(user_id="1"))
    assert score is not None and score.score == 742
    assert score.as_of_date == date(2026, 7, 1)

    assert await db.get_financial_score(make_user_context(user_id="2")) is None


@pytest.mark.asyncio
async def test_absent_data_is_reported_as_absent_not_invented(db):
    """
    Budgets stay unanswerable regardless of who asks: the real dump's
    `categories` table (which would name budgets.category_id) is empty, so
    there is no honest name or spent_amount to report. A user with no
    investment holdings on file gets [], not an invented empty portfolio.
    """
    context = make_user_context(user_id="1")
    assert await db.get_budgets(context, period_start=JULY["start"], period_end=JULY["end"]) == []
    assert await db.get_investments(make_user_context(user_id="2")) == []


def test_a_missing_database_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_demo_db"):
        SqliteBackendClient(tmp_path / "nope.sqlite3")


@pytest.mark.asyncio
async def test_the_connection_is_read_only(db):
    """A demo must not be able to alter the data it is demonstrating."""
    conn = sqlite3.connect(db._uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM plaid_transactions")
    finally:
        conn.close()
