"""
Build a local SQLite copy of the MyFi dump for demos and validation.

Why this exists
---------------
The mock backend invents plausible numbers. That is fine for tests, but a
client demo is a claim about the real product, and a number that came from a
seeded RNG cannot back that claim. This turns the shared MySQL dump into a
small read-only SQLite file that the AI service can query directly, so what
appears on screen is what is actually in the database.

It reads the dump as a stream: the file is ~177 MB and there is no reason to
hold it in memory. Only the tables the service actually reads are copied --
everything else in the dump (sessions, tokens, notifications, market data) is
skipped, which keeps the demo database small and means credentials never get
copied anywhere.

    python scripts/build_demo_db.py "shaktiparastechn_myfi 5.sql"

Then point the service at it:

    BACKEND_INTEGRATION_MODE=sqlite
    DEMO_DB_PATH=data/demo.sqlite3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Only these tables are copied. Anything holding credentials or tokens
# (application_users.password, personal_access_tokens, sessions) is either
# skipped entirely or column-filtered below.
# Column types matter here. Every value arrives from the dump as text, and
# SQLite will happily store "0" in an untyped column -- after which
# `WHERE is_deleted = 0` matches nothing and every query silently returns an
# empty result. Declaring INTEGER/REAL lets SQLite coerce on insert, so
# comparisons behave like they do in MySQL.
WANTED: dict[str, dict[str, str]] = {
    "application_users": {
        "id": "INTEGER", "Name": "TEXT", "email": "TEXT",
        "country": "TEXT", "created_at": "TEXT",
    },
    "plaid_accounts": {
        "account_id": "TEXT", "name": "TEXT", "avaliable_balance": "REAL",
        "current_balance": "REAL", "mask": "TEXT", "type": "TEXT",
        "subtype": "TEXT", "user_id": "INTEGER",
    },
    "plaid_transactions": {
        # `id` and `is_plaid` are carried for the manually-entered rows:
        # those have no Plaid transaction_id, and without the primary key
        # there is nothing to identify them by.
        "id": "INTEGER", "is_plaid": "INTEGER",
        "transaction_id": "TEXT", "account_id": "TEXT", "name": "TEXT",
        "amount": "REAL", "transaction_date": "TEXT", "plaid_category_id": "INTEGER",
        "pending": "TEXT", "user_id": "INTEGER", "is_recurring": "INTEGER",
        "is_deleted": "INTEGER", "is_review_transfer": "INTEGER",
        "frequency": "TEXT", "due_date": "TEXT",
    },
    "plaid_categories": {
        "id": "INTEGER", "display_name": "TEXT",
        "primary_name": "TEXT", "detailed_name": "TEXT",
    },
    "plaid_recurring_streams": {
        "user_id": "INTEGER", "stream_id": "TEXT", "plaid_category_id": "INTEGER",
        "account_id": "TEXT", "amount": "REAL", "frequency": "TEXT",
        "last_payment_date": "TEXT", "next_payment_date": "TEXT",
        "active": "INTEGER", "description": "TEXT",
    },
    "credit_scores": {
        "user_id": "INTEGER", "report_date": "TEXT", "provider": "TEXT",
        "score": "INTEGER", "score_model": "TEXT", "score_type": "TEXT",
    },
    # Everything below was added to let the AI service answer investment,
    # liability, budget, and Allo Score questions from real data instead of
    # the stubbed-empty defaults. None of these tables hold credentials.
    "investment_items": {
        "id": "INTEGER", "user_id": "INTEGER", "item_id": "TEXT",
        "institution_id": "TEXT", "institution_name": "TEXT",
    },
    "investment_accounts": {
        "id": "INTEGER", "item_id": "INTEGER", "account_id": "TEXT",
        "name": "TEXT", "type": "TEXT", "subtype": "TEXT",
        "current_balance": "REAL", "available_balance": "REAL",
        "iso_currency_code": "TEXT", "is_delete": "INTEGER",
    },
    "investment_holdings": {
        "id": "INTEGER", "account_id": "INTEGER", "security_id": "TEXT",
        "quantity": "REAL", "institution_value": "REAL", "cost_basis": "REAL",
        "as_of_date": "TEXT",
    },
    "investment_securities": {
        "id": "INTEGER", "security_id": "TEXT", "name": "TEXT",
        "ticker_symbol": "TEXT", "type": "TEXT", "close_price": "REAL",
        "iso_currency_code": "TEXT",
    },
    "liability_accounts": {
        "id": "INTEGER", "user_id": "INTEGER", "account_id": "TEXT",
        "name": "TEXT", "type": "TEXT", "subtype": "TEXT",
        "balance": "REAL", "iso_currency_code": "TEXT",
    },
    "liability_credit": {
        "id": "INTEGER", "user_id": "INTEGER", "account_id": "TEXT",
        "aprs": "TEXT", "is_overdue": "INTEGER",
        "last_payment_amount": "REAL", "last_payment_date": "TEXT",
        "last_statement_balance": "REAL", "minimum_payment_amount": "REAL",
        "next_payment_due_date": "TEXT",
    },
    # Empty in this dump, but the service's query joins against them, so a
    # future dump that populates loans/mortgages works without a code change.
    "liability_loans": {
        "id": "INTEGER", "user_id": "INTEGER", "account_id": "TEXT",
        "loan_name": "TEXT", "current_balance": "REAL",
        "interest_rate_percentage": "REAL", "last_payment_date": "TEXT",
        "last_payment_amount": "REAL",
    },
    "liability_mortgages": {
        "id": "INTEGER", "user_id": "INTEGER", "account_id": "TEXT",
        "interest_rate": "REAL", "next_payment_due_date": "TEXT",
    },
    # account_type here is unambiguously a debt type (credit/mortgage/loan),
    # unlike liability_accounts, which also holds non-debt checking/savings
    # rows tied to the same liability-bearing Item.
    "liability_snapshots": {
        "id": "INTEGER", "user_id": "INTEGER", "account_id": "TEXT",
        "account_type": "TEXT", "balance": "REAL",
        "last_payment_amount": "REAL", "last_payment_date": "TEXT",
        "snapshot_date": "TEXT",
    },
    # Copied for inspection only -- category_id has nothing to resolve to
    # (the `categories` table in this dump is empty), so no code treats this
    # as usable. See docs/demo-database.md / the plan for this change.
    "budgets": {
        "id": "INTEGER", "category_id": "INTEGER", "user_id": "INTEGER",
        "amount": "REAL", "status": "INTEGER",
    },
    "save_goals": {
        "id": "INTEGER", "total_saving_amount": "REAL", "monthly_saving_amount": "REAL",
        "user_id": "INTEGER", "start_date": "TEXT", "end_date": "TEXT",
    },
    "allo_score_snapshots": {
        "id": "INTEGER", "user_id": "INTEGER",
        "overall_score": "INTEGER", "overall_tier": "TEXT", "overall_insight": "TEXT",
        "flow_score": "INTEGER", "flow_tier": "TEXT", "flow_insight": "TEXT",
        "stability_score": "INTEGER", "stability_tier": "TEXT", "stability_insight": "TEXT",
        "retirement_score": "INTEGER", "retirement_tier": "TEXT", "retirement_insight": "TEXT",
        "calculation_period_start": "TEXT", "calculation_period_end": "TEXT",
        # JSON blob -- parsed in Python (app/services/sqlite_backend_client.py),
        # never in SQL. Carries the backend's own retirement-readiness
        # computation (target_retirement_age, readiness_ratio, etc.).
        "raw_metrics": "TEXT",
    },
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_txn_user_date ON plaid_transactions(user_id, transaction_date)",
    "CREATE INDEX IF NOT EXISTS ix_txn_user_deleted ON plaid_transactions(user_id, is_deleted)",
    "CREATE INDEX IF NOT EXISTS ix_acct_user ON plaid_accounts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_stream_user ON plaid_recurring_streams(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_score_user ON credit_scores(user_id, report_date)",
    "CREATE INDEX IF NOT EXISTS ix_cat_id ON plaid_categories(id)",
    "CREATE INDEX IF NOT EXISTS ix_inv_items_user ON investment_items(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_inv_accounts_item ON investment_accounts(item_id)",
    "CREATE INDEX IF NOT EXISTS ix_inv_holdings_account ON investment_holdings(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_inv_securities_security_id ON investment_securities(security_id)",
    "CREATE INDEX IF NOT EXISTS ix_liab_accounts_user ON liability_accounts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_liab_credit_account ON liability_credit(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_liab_loans_account ON liability_loans(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_liab_mortgages_account ON liability_mortgages(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_budgets_user ON budgets(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_save_goals_user ON save_goals(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_liab_snapshots_user_date ON liability_snapshots(user_id, snapshot_date)",
    "CREATE INDEX IF NOT EXISTS ix_allo_score_user ON allo_score_snapshots(user_id, calculation_period_end)",
]


def _parse_values(chunk: str):
    """
    Yield row tuples from the VALUES clause of an INSERT.

    Written by hand rather than with a regex because the dump contains
    merchant names with commas, apostrophes and escaped quotes in them --
    "MCDONALD'S #4821, LOT 3" is one field, not three.
    """
    row: list[str | None] = []
    field: list[str] = []
    quoted = False
    in_string = False
    escaped = False

    for c in chunk:
        if in_string:
            if escaped:
                field.append(c)
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == "'":
                in_string = False
            else:
                field.append(c)
            continue

        if c == "'":
            in_string = True
            quoted = True
        elif c == "(":
            row, field, quoted = [], [], False
        elif c == ",":
            row.append(_finish(field, quoted))
            field, quoted = [], False
        elif c == ")":
            row.append(_finish(field, quoted))
            yield row
            row, field, quoted = [], [], False
        elif c not in " \n\r\t":
            field.append(c)


def _finish(field: list[str], quoted: bool) -> str | None:
    text = "".join(field)
    # An unquoted NULL is a real null; the string 'NULL' is the word.
    if not quoted and text.upper() == "NULL":
        return None
    return text


def _iter_rows(path: Path, table: str):
    prefix = f"INSERT INTO `{table}`"
    with path.open("r", encoding="utf-8", errors="replace") as f:
        buf: list[str] = []
        cols: list[str] = []
        capturing = False
        for line in f:
            if not capturing and line.startswith(prefix):
                capturing = True
                head, _, rest = line.partition(" VALUES ")
                cols = [c.strip(" `") for c in head[head.index("(") + 1 : head.rindex(")")].split(",")]
                buf = [rest]
            elif capturing:
                buf.append(line)
            if capturing and line.rstrip().endswith(";"):
                capturing = False
                for row in _parse_values("".join(buf)):
                    yield cols, row
                buf = []


def build(dump: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(out)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    for table, columns in WANTED.items():
        spec = ", ".join(f"{name} {sql_type}" for name, sql_type in columns.items())
        conn.execute(f"CREATE TABLE {table} ({spec})")

    totals: dict[str, int] = {}
    for table, columns in WANTED.items():
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} VALUES ({placeholders})"
        batch: list[tuple] = []
        count = 0
        for source_cols, row in _iter_rows(dump, table):
            record = dict(zip(source_cols, row, strict=False))
            batch.append(tuple(record.get(c) for c in columns))
            count += 1
            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                batch.clear()
        if batch:
            conn.executemany(sql, batch)
        totals[table] = count
        print(f"  {table:26} {count:>7} rows")

    for statement in INDEXES:
        conn.execute(statement)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"\n  {out}  ({size_mb:.1f} MB)")
    if not totals.get("plaid_transactions"):
        print("\n  WARNING: no transactions were copied - is this the right dump?", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump", type=Path, help="path to the MySQL dump (.sql)")
    ap.add_argument("--out", type=Path, default=Path("data/demo.sqlite3"))
    args = ap.parse_args()

    if not args.dump.exists():
        print(f"No such dump: {args.dump}", file=sys.stderr)
        return 1

    print(f"\nReading {args.dump.name} ({args.dump.stat().st_size / (1024*1024):.0f} MB)\n")
    build(args.dump, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
