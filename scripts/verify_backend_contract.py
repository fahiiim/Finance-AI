"""
Check a real backend's /internal/* endpoints against what the AI service expects.

WHY THIS EXISTS
    The AI service reads nothing but what the backend hands it. If an endpoint
    is missing a field, formats money as a float, or flips the sign of an
    amount, the assistant does not crash -- it answers confidently with a
    wrong number. That is the worst possible failure for a financial product,
    because nothing on screen looks broken.

    So the contract gets verified before users meet it, against the real
    endpoints, with the real data. Run this from the backend developer's
    machine or CI as soon as an endpoint is written.

USAGE
    python scripts/verify_backend_contract.py \\
        --base-url https://backend.internal \\
        --token "$BACKEND_API_SERVICE_TOKEN" \\
        --user-id 267

    Add --other-user-id to also check cross-user isolation, which is the one
    failure worth failing a deploy over:

        ... --user-id 267 --other-user-id 231

EXIT CODE
    0 when every required check passed, 1 otherwise -- so it can gate a
    pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.schemas.financial import (  # noqa: E402
    Account,
    Bill,
    BudgetCategory,
    FinancialScore,
    FinancialSummary,
    InvestmentHolding,
    Liability,
    RecurringTransaction,
    Subscription,
    Transaction,
)

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    colour = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", SKIP: "\033[90m"}[status]
    print(f"  {colour}{status:4}\033[0m  {name}" + (f"\n         {detail}" if detail else ""))


class Checker:
    def __init__(self, base_url: str, token: str, user_id: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        self.user_id = user_id

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, path: str, **params):
        params = {"user_id": self.user_id, **{k: v for k, v in params.items() if v is not None}}
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json() if response.content else None

    async def check_list(self, path: str, model, label: str, *, required: bool, **params) -> list | None:
        """Fetch a list endpoint and validate every row against its model."""
        try:
            data = await self.fetch(path, **params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            record(
                FAIL if required else WARN,
                f"{label} ({path})",
                f"HTTP {status}. " + (
                    "The endpoint is not implemented yet."
                    if status == 404
                    else "Check the service token and that user_id is accepted as a query parameter."
                    if status in (401, 403)
                    else "The endpoint errored."
                ),
            )
            return None
        except httpx.HTTPError as exc:
            record(FAIL if required else WARN, f"{label} ({path})", f"Could not reach it: {type(exc).__name__}")
            return None

        if data is None or not isinstance(data, list):
            record(FAIL, f"{label} ({path})", f"Expected a JSON array, got {type(data).__name__}.")
            return None

        try:
            rows = [model.model_validate(item) for item in data]
        except ValidationError as exc:
            first = exc.errors()[0]
            record(
                FAIL,
                f"{label} ({path})",
                f"Row does not match the expected shape: field "
                f"{'.'.join(str(p) for p in first['loc'])} -- {first['msg']}. "
                f"See app/schemas/financial.py::{model.__name__}.",
            )
            return None

        record(PASS, f"{label} ({path})", f"{len(rows)} row(s), all valid.")
        return rows


def check_ownership(rows: list, label: str, user_id: str) -> None:
    """owner_user_id is what lets STRICT_DATA_OWNERSHIP be turned on at all."""
    if not rows:
        record(SKIP, f"{label}: owner_user_id", "No rows to check.")
        return
    missing = [r for r in rows if getattr(r, "owner_user_id", None) is None]
    if missing:
        record(
            WARN,
            f"{label}: owner_user_id",
            f"{len(missing)}/{len(rows)} row(s) omit it. The cross-user containment check cannot "
            "verify these, and STRICT_DATA_OWNERSHIP=true would reject them. Populate it upstream.",
        )
        return
    foreign = [r for r in rows if str(r.owner_user_id) != str(user_id)]
    if foreign:
        record(FAIL, f"{label}: owner_user_id", f"{len(foreign)} row(s) belong to a DIFFERENT user.")
        return
    record(PASS, f"{label}: owner_user_id", "Present on every row and correctly scoped.")


def check_sign_convention(transactions: list[Transaction]) -> None:
    """
    POSITIVE must mean money OUT. This is the highest-stakes check here.

    Verified against 122,326 real rows: known purchases are 18,300 positive to
    167 negative, known income 6 positive to 7,557 negative. If the backend
    normalises signs the other way, income and spending swap places and every
    total, savings rate and cash-flow figure inverts -- silently, with no
    error anywhere. See app/utils/transactions.py.
    """
    if not transactions:
        record(SKIP, "Sign convention", "No transactions returned for this window.")
        return

    income_words = ("payroll", "salary", "direct dep", "deposit", "wages", "refund")
    spend_words = ("walmart", "amazon", "uber", "netflix", "starbucks", "target", "costco", "shell")

    def bucket(words):
        return [
            t for t in transactions
            if any(w in (t.merchant_name or "").lower() for w in words)
        ]

    likely_income, likely_spend = bucket(income_words), bucket(spend_words)
    positive = sum(1 for t in transactions if t.amount > 0)
    share = positive / len(transactions)

    if likely_spend and likely_income:
        spend_ok = sum(1 for t in likely_spend if t.amount > 0) / len(likely_spend)
        income_ok = sum(1 for t in likely_income if t.amount < 0) / len(likely_income)
        if spend_ok >= 0.8 and income_ok >= 0.8:
            record(PASS, "Sign convention", "Positive = money out, negative = money in, as expected.")
        else:
            record(
                FAIL,
                "Sign convention",
                f"Only {spend_ok:.0%} of recognisable purchases are positive and {income_ok:.0%} of "
                "recognisable income is negative. If the backend has normalised signs, income and "
                "spending will swap places in EVERY figure the assistant reports, with no error. "
                "Either serve Plaid's raw signs or tell us, so OUTFLOW_IS_POSITIVE can be flipped.",
            )
        return

    if share < 0.5:
        record(
            WARN,
            "Sign convention",
            f"Only {share:.0%} of rows are positive; spending normally dominates, so this may mean "
            "signs are inverted. Could not confirm from merchant names -- check manually.",
        )
    else:
        record(WARN, "Sign convention", f"{share:.0%} of rows positive. Plausible, but unconfirmed.")


def check_money_and_dates(rows: list, label: str) -> None:
    """Money must be an object with a string amount; dates must be YYYY-MM-DD."""
    if not rows:
        record(SKIP, f"{label}: money/date types", "No rows to check.")
        return
    for row in rows[:50]:
        for field in type(row).model_fields:
            value = getattr(row, field, None)
            if isinstance(value, Decimal) and value != value.quantize(value):
                record(WARN, f"{label}: money/date types", f"{field} lost precision -- send it as a string.")
                return
    record(PASS, f"{label}: money/date types", "Amounts parsed as exact decimals; dates parsed as dates.")


def check_subcategory(transactions: list[Transaction]) -> None:
    """The tier that makes 'how much on restaurants?' answerable as asked."""
    if not transactions:
        record(SKIP, "Transaction.subcategory", "No transactions to check.")
        return
    present = sum(1 for t in transactions if t.subcategory)
    if present == 0:
        record(
            WARN,
            "Transaction.subcategory",
            "No row carries one. The assistant will still answer, but 'how much on restaurants?' "
            "gets ALL food-and-drink spending with a note saying so. Send Plaid's detailed tier "
            "(FOOD_AND_DRINK_RESTAURANT) to answer it precisely -- see handover section 5.3.",
        )
    else:
        record(PASS, "Transaction.subcategory", f"Present on {present}/{len(transactions)} row(s).")


async def check_date_filter_is_inclusive(checker: Checker) -> None:
    """A one-day window is how a question about a specific date is asked."""
    try:
        rows = await checker.fetch("/internal/transactions", start="2026-01-01", end="2026-12-31")
    except httpx.HTTPError:
        record(SKIP, "Date filtering", "Transactions endpoint unavailable.")
        return
    if not rows:
        record(SKIP, "Date filtering", "No transactions in 2026 to test with.")
        return

    sample = Transaction.model_validate(rows[0])
    day = sample.date.isoformat()
    try:
        same_day = await checker.fetch("/internal/transactions", start=day, end=day)
    except httpx.HTTPError:
        record(SKIP, "Date filtering", "Single-day query failed.")
        return

    if same_day:
        record(PASS, "Date filtering", f"start=end={day} returns rows -- inclusive on both ends.")
    else:
        record(
            FAIL,
            "Date filtering",
            f"start=end={day} returned nothing although a transaction exists on that date. "
            "Both bounds must be INCLUSIVE, or every single-date question answers 'nothing'.",
        )


async def check_cross_user_isolation(checker: Checker, other_user_id: str) -> None:
    """The one failure worth failing a deploy over."""
    try:
        rows = await checker._client.get(
            "/internal/transactions", params={"user_id": other_user_id}
        )
        rows.raise_for_status()
        data = rows.json()
    except httpx.HTTPError as exc:
        record(PASS, "Cross-user isolation", f"Requesting another user's data was refused ({exc.__class__.__name__}).")
        return

    if not data:
        record(WARN, "Cross-user isolation", f"user_id={other_user_id} returned no rows; inconclusive.")
        return

    owners = {str(r.get("owner_user_id")) for r in data if isinstance(r, dict) and r.get("owner_user_id")}
    if owners and owners != {str(other_user_id)}:
        record(FAIL, "Cross-user isolation", f"Rows came back owned by {owners}.")
    else:
        record(
            WARN,
            "Cross-user isolation",
            "The endpoint served another user's rows purely because user_id said so. That is the "
            "expected design ONLY if the service token is trusted and never leaves your network. "
            "Confirm the token cannot reach a client device.",
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--user-id", required=True, help="A user with real data.")
    parser.add_argument("--other-user-id", default=None, help="A different user, to test isolation.")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    checker = Checker(args.base_url, args.token, args.user_id, args.timeout)
    end = date.today()
    start = end - timedelta(days=90)

    try:
        print(f"\nChecking {args.base_url} for user {args.user_id}\n")

        print("REQUIRED -- the assistant cannot function without these")
        transactions = await checker.check_list(
            "/internal/transactions", Transaction, "Transactions",
            required=True, start=start.isoformat(), end=end.isoformat(),
        )
        accounts = await checker.check_list("/internal/accounts", Account, "Accounts", required=True)

        try:
            summary_raw = await checker.fetch(
                "/internal/financial-summary", start=start.isoformat(), end=end.isoformat()
            )
            FinancialSummary.model_validate(summary_raw)
            record(PASS, "Financial summary (/internal/financial-summary)", "Valid.")
        except httpx.HTTPStatusError as exc:
            record(FAIL, "Financial summary (/internal/financial-summary)", f"HTTP {exc.response.status_code}.")
        except (ValidationError, httpx.HTTPError, TypeError) as exc:
            record(FAIL, "Financial summary (/internal/financial-summary)", str(exc)[:200])

        print("\nOPTIONAL -- a missing one is reported to the user as unavailable, not as zero")
        await checker.check_list("/internal/bills", Bill, "Bills", required=False,
                                 start=start.isoformat(), end=end.isoformat())
        await checker.check_list("/internal/budgets", BudgetCategory, "Budgets", required=False,
                                 start=start.isoformat(), end=end.isoformat())
        await checker.check_list("/internal/subscriptions", Subscription, "Subscriptions", required=False)
        await checker.check_list("/internal/recurring-transactions", RecurringTransaction,
                                 "Recurring transactions", required=False)
        await checker.check_list("/internal/investments", InvestmentHolding, "Investments", required=False)
        await checker.check_list("/internal/liabilities", Liability, "Liabilities", required=False)
        try:
            score_raw = await checker.fetch("/internal/financial-score")
            if score_raw:
                FinancialScore.model_validate(score_raw)
                record(PASS, "Financial score (/internal/financial-score)", "Valid.")
            else:
                record(SKIP, "Financial score (/internal/financial-score)", "Empty -- reported as unavailable.")
        except (ValidationError, httpx.HTTPError) as exc:
            record(WARN, "Financial score (/internal/financial-score)", str(exc)[:160])

        print("\nSEMANTICS -- these fail silently in production, so they are checked explicitly")
        if transactions is not None:
            check_sign_convention(transactions)
            check_ownership(transactions, "Transactions", args.user_id)
            check_money_and_dates(transactions, "Transactions")
            check_subcategory(transactions)
        if accounts is not None:
            check_ownership(accounts, "Accounts", args.user_id)
        await check_date_filter_is_inclusive(checker)

        if args.other_user_id:
            print("\nISOLATION")
            await check_cross_user_isolation(checker, args.other_user_id)
    finally:
        await checker.aclose()

    failures = [r for r in _results if r[0] == FAIL]
    warnings = [r for r in _results if r[0] == WARN]
    print(
        f"\n{len(_results)} check(s): "
        f"{sum(1 for r in _results if r[0] == PASS)} passed, {len(failures)} failed, {len(warnings)} warning(s)."
    )
    if failures:
        print("\nMust fix before launch:")
        for _, name, detail in failures:
            print(f"  - {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
