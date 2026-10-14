"""
Check a live MyFi backend against what the AI service depends on.

WHY THIS EXISTS
    The AI service reads nothing but what the backend hands it. If an endpoint
    changes a field name, flips the sign of an amount, or silently stops
    paginating, the assistant does not crash -- it answers confidently with a
    wrong number. So the contract is verified against the real endpoints, with
    real data, before users meet it. Run this after every backend deploy.

    It drives the same BackendAPIClient the service uses, so a PASS here means
    the service's own mapping accepted the live payloads -- not merely that
    the endpoints returned 200.

USAGE
    python scripts/verify_backend_contract.py \\
        --base-url https://host/MyFi/api \\
        --token "$BACKEND_API_SERVICE_TOKEN" \\
        --user-id 267 [--other-user-id 231]

    --base-url / --token default to BACKEND_API_BASE_URL and
    BACKEND_API_SERVICE_TOKEN from the environment / .env.

EXIT CODE
    0 when every required check passed, 1 otherwise -- usable as a CI gate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.security import UserContext  # noqa: E402
from app.repositories.base import HttpRepository  # noqa: E402
from app.services.backend_client import BackendAPIClient  # noqa: E402

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    colour = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", SKIP: "\033[90m"}[status]
    print(f"  {colour}{status:4}\033[0m  {name}" + (f"\n         {detail}" if detail else ""))


def _context(user_id: str) -> UserContext:
    return UserContext(user_id=user_id, session_id="contract-check", timezone="UTC", currency="USD")


def _owners(rows) -> set[str]:
    return {str(r.owner_user_id) for r in rows if getattr(r, "owner_user_id", None) is not None}


async def call(label: str, coro, *, required: bool):
    started = time.perf_counter()
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 - every failure is a finding
        record(FAIL if required else WARN, label, f"{type(exc).__name__}: {str(exc)[:160]}")
        return None
    seconds = time.perf_counter() - started
    size = len(result) if isinstance(result, list) else ("1 record" if result is not None else "empty")
    slow = " -- SLOW" if seconds > 10 else ""
    record(WARN if slow else PASS, label, f"{size} in {seconds:.1f}s{slow}")
    return result


async def run(client: BackendAPIClient, user_id: str, other_user_id: str | None) -> None:
    ctx = _context(user_id)
    today = date.today()
    start, end = today - timedelta(days=90), today

    print("REQUIRED -- the assistant cannot answer without these")
    transactions = await call(
        "Transactions (90 days, all pages)", client.get_transactions(ctx, start=start, end=end), required=True
    )
    accounts = await call("Accounts", client.get_accounts(ctx), required=True)
    summary = await call(
        "Financial summary", client.get_financial_summary(ctx, start=today.replace(day=1), end=today), required=True
    )

    print("\nFEATURES -- an unavailable one is reported to the user as unavailable")
    bills = await call("Bills (current month)", client.get_bills(ctx), required=False)
    budgets = await call(
        "Budgets", client.get_budgets(ctx, period_start=today.replace(day=1), period_end=today), required=False
    )
    subscriptions = await call("Subscriptions", client.get_subscriptions(ctx), required=False)
    recurring = await call("Recurring transactions", client.get_recurring_transactions(ctx), required=False)
    investments = await call("Investments", client.get_investments(ctx), required=False)
    liabilities = await call("Liabilities", client.get_liabilities(ctx), required=False)
    await call("Net worth", client.get_net_worth(ctx), required=False)
    await call("Credit score", client.get_financial_score(ctx), required=False)
    await call("Credit score history", client.get_financial_score_history(ctx), required=False)
    await call("Allo score", client.get_allo_score(ctx), required=False)
    await call("Allo score history", client.get_allo_score_history(ctx), required=False)
    await call("Savings goal", client.get_savings_goal(ctx), required=False)
    await call("Liability history", client.get_liability_history(ctx), required=False)
    await call("Retirement readiness", client.get_retirement_readiness(ctx), required=False)
    profile = await call("User profile", client.get_user_profile(ctx), required=False)

    print("\nSEMANTICS -- these fail silently in production, so they are checked explicitly")

    owned = [
        ("Transactions", transactions), ("Accounts", accounts), ("Bills", bills), ("Budgets", budgets),
        ("Subscriptions", subscriptions), ("Recurring", recurring), ("Investments", investments),
        ("Liabilities", liabilities),
    ]
    for label, rows in owned:
        if not rows:
            record(SKIP, f"{label}: owner_user_id", "no rows")
            continue
        unlabeled = sum(1 for r in rows if getattr(r, "owner_user_id", None) is None)
        foreign = _owners(rows) - {str(user_id)}
        if foreign:
            record(FAIL, f"{label}: owner_user_id", f"rows belong to a DIFFERENT user: {sorted(foreign)}")
        elif unlabeled:
            record(WARN, f"{label}: owner_user_id", f"{unlabeled}/{len(rows)} rows carry no owner")
        else:
            record(PASS, f"{label}: owner_user_id", "present on every row and matches the requested user")

    if transactions:
        positive = sum(1 for t in transactions if t.amount > 0)
        negative = sum(1 for t in transactions if t.amount < 0)
        share = positive / max(positive + negative, 1)
        # Spending dominates a normal account; income should be the minority.
        record(
            PASS if share >= 0.5 else WARN,
            "Sign convention (positive = money out)",
            f"{positive} positive / {negative} negative ({share:.0%} outflow)",
        )

        manual = [t for t in transactions if t.transaction_id.startswith("manual-")]
        record(PASS, "Manually entered transactions", f"{len(manual)} included")

        sample = transactions[0].date
        one_day = await client.get_transactions(ctx, start=sample, end=sample)
        record(
            PASS if one_day else FAIL,
            "Date filter is inclusive on both ends",
            f"start=end={sample} returned {len(one_day)} row(s)",
        )

        ids = Counter(t.transaction_id for t in transactions)
        dupes = {i: n for i, n in ids.items() if n > 1}
        record(PASS if not dupes else FAIL, "No duplicate rows across pages", f"{len(dupes)} duplicated id(s)")

    if summary is not None:
        ok = summary.total_expenses.amount >= 0 and summary.total_income.amount >= 0
        record(
            PASS if ok else FAIL,
            "Summary totals are magnitudes",
            f"income {summary.total_income.amount}, expenses {summary.total_expenses.amount}",
        )
    if profile is not None:
        record(PASS if profile.get("timezone") else WARN, "Profile carries a timezone", str(profile.get("timezone")))

    if other_user_id:
        print("\nISOLATION")
        other = await client.get_transactions(_context(other_user_id), start=start, end=end)
        foreign = _owners(other) - {str(other_user_id)}
        if foreign:
            record(
                FAIL, "Cross-user isolation",
                f"asking for user {other_user_id} returned rows owned by {sorted(foreign)}",
            )
        elif user_id in _owners(other):
            record(FAIL, "Cross-user isolation", "another user's request returned this user's rows")
        else:
            record(
                PASS, "Cross-user isolation",
                f"user {other_user_id} received only their own rows ({len(other)})",
            )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=settings.backend_api_base_url)
    parser.add_argument("--token", default=settings.backend_api_service_token)
    parser.add_argument("--user-id", required=True, help="A user with real data.")
    parser.add_argument("--other-user-id", default=None, help="A different user, to test isolation.")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if not args.base_url or not args.token:
        print("Set --base-url and --token (or BACKEND_API_BASE_URL / BACKEND_API_SERVICE_TOKEN).")
        return 2

    repo = HttpRepository(
        base_url=args.base_url,
        timeout_seconds=args.timeout,
        headers={"Authorization": f"Bearer {args.token}", "Accept": "application/json"},
    )
    client = BackendAPIClient(repo)
    print(f"\nChecking {args.base_url} for user {args.user_id}\n")
    try:
        await run(client, args.user_id, args.other_user_id)
    finally:
        await client.aclose()

    failures = [r for r in _results if r[0] == FAIL]
    print(
        f"\n{len(_results)} check(s): {sum(1 for r in _results if r[0] == PASS)} passed, "
        f"{len(failures)} failed, {sum(1 for r in _results if r[0] == WARN)} warning(s)."
    )
    for _, name, detail in failures:
        print(f"  - {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
