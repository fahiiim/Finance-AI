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
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_txn_user_date ON plaid_transactions(user_id, transaction_date)",
    "CREATE INDEX IF NOT EXISTS ix_txn_user_deleted ON plaid_transactions(user_id, is_deleted)",
    "CREATE INDEX IF NOT EXISTS ix_acct_user ON plaid_accounts(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_stream_user ON plaid_recurring_streams(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_score_user ON credit_scores(user_id, report_date)",
    "CREATE INDEX IF NOT EXISTS ix_cat_id ON plaid_categories(id)",
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
