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
    assert [liability.liability_id for liability in liabilities] == ["acct-2"]
    assert liabilities[0].type == "credit_card"

    assert await db.get_accounts(make_user_context(user_id="2")) != accounts


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
    """No budgets or holdings table exists; inventing either would be worse."""
    context = make_user_context(user_id="1")
    assert await db.get_budgets(context, period_start=JULY["start"], period_end=JULY["end"]) == []
    assert await db.get_investments(context) == []


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
