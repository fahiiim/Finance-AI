"""
Backend client abstraction.

This is the ONLY way the AI service reaches financial data. The
orchestrator/tools never know whether data came from the client's existing
backend API or a read-only database replica — see docs/backend-integration.md
for the integration contract and docs/assumptions.md for what's assumed
about the (not-yet-provided) existing backend/schema.

Every method takes UserContext explicitly and every concrete implementation
MUST scope its query/request using context.user_id — see
financial-ai-data-security skill. No method here accepts a bare user_id.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache

from app.core.exceptions import BackendServiceError
from app.core.security import UserContext
from app.schemas.financial import (
    Account,
    AlloScore,
    Bill,
    BudgetCategory,
    FinancialScore,
    FinancialSummary,
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
from app.services.live_api_mapping import (
    as_dict,
    as_list,
    envelope,
    map_account,
    map_allo_score,
    map_bills,
    map_budgets,
    map_credit_score,
    map_investments,
    map_liabilities,
    map_liability_history,
    map_net_worth,
    map_recurring,
    map_retirement,
    map_savings_goal,
    map_subscriptions,
    map_transaction,
    to_date,
    unwrap,
)
from app.utils.dates import current_date_in, month_bounds
from app.utils.transactions import expense_amount, expenses, income, income_amount


class BackendClient(ABC):
    """Abstract financial data access. See concrete implementations below."""

    @abstractmethod
    async def get_user_profile(self, context: UserContext) -> dict: ...

    @abstractmethod
    async def get_accounts(self, context: UserContext) -> list[Account]: ...

    @abstractmethod
    async def get_transactions(
        self,
        context: UserContext,
        *,
        start: date | None = None,
        end: date | None = None,
        category: str | None = None,
        merchant: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
    ) -> list[Transaction]: ...

    @abstractmethod
    async def get_income(self, context: UserContext, *, start: date, end: date) -> list[Transaction]: ...

    @abstractmethod
    async def get_expenses(self, context: UserContext, *, start: date, end: date) -> list[Transaction]: ...

    @abstractmethod
    async def get_bills(
        self, context: UserContext, *, start: date | None = None, end: date | None = None
    ) -> list[Bill]: ...

    @abstractmethod
    async def get_budgets(self, context: UserContext, *, period_start: date, period_end: date) -> list[BudgetCategory]: ...

    @abstractmethod
    async def get_subscriptions(self, context: UserContext) -> list[Subscription]: ...

    @abstractmethod
    async def get_recurring_transactions(self, context: UserContext) -> list[RecurringTransaction]: ...

    @abstractmethod
    async def get_investments(self, context: UserContext) -> list[InvestmentHolding]: ...

    @abstractmethod
    async def get_liabilities(self, context: UserContext) -> list[Liability]: ...

    @abstractmethod
    async def get_financial_summary(self, context: UserContext, *, start: date, end: date) -> FinancialSummary: ...

    @abstractmethod
    async def get_financial_score(self, context: UserContext) -> FinancialScore | None: ...

    async def get_allo_score(self, context: UserContext) -> AlloScore | None:
        """
        The app's own 0-100 financial-health score (see AlloScore) --
        deliberately separate from get_financial_score's credit-bureau score.

        Concrete default, NOT abstract: `backend_api` mode has no agreed
        endpoint for this yet (docs/handover-backend.md's one documented
        financial-score endpoint already serves get_financial_score).
        Forcing every subclass to implement this immediately would mean
        inventing an endpoint path production doesn't have. Subclasses that
        DO have a real source (the live API) override this.
        """
        return None

    async def get_savings_goal(self, context: UserContext) -> SavingsGoal | None:
        """
        The user's stored savings goal, if any -- concrete default `None` for
        the same reason as get_allo_score: no documented backend_api endpoint
        exists for this yet. Subclasses with a real source override it.
        """
        return None

    async def get_financial_score_history(self, context: UserContext) -> list[FinancialScore]:
        """Past credit-bureau scores, newest first. Concrete default []: no
        documented backend_api endpoint for history exists yet."""
        return []

    async def get_allo_score_history(self, context: UserContext) -> list[AlloScore]:
        """Past Allo Score snapshots, newest first. Concrete default []: no
        documented backend_api endpoint for history exists yet."""
        return []

    async def get_liability_history(self, context: UserContext) -> list[LiabilitySnapshot]:
        """Past total-debt snapshots, newest first. Concrete default []: no
        documented backend_api endpoint for history exists yet."""
        return []

    async def get_retirement_readiness(self, context: UserContext) -> RetirementReadiness | None:
        """Backend-computed retirement-readiness figures, if any. Concrete
        default None: no documented backend_api endpoint for this yet."""
        return None

    async def get_net_worth(self, context: UserContext) -> NetWorth | None:
        """The backend's own net-worth figure (what the app displays), if it
        serves one. Concrete default None: a source without one
        falls back to net_worth.py deriving it from accounts, holdings and debts."""
        return None

    def bills_coverage(self, context: UserContext) -> tuple[date, date] | None:
        """
        The date span get_bills can actually answer for, or None when any
        window can be. A source that can only see part of the calendar must
        say so: an empty list for an unreachable window is indistinguishable
        from "nothing is due", and reads as good news the data cannot support.
        """
        return None

    async def aclose(self) -> None:
        """
        Release any long-lived resources (connection pools, sockets).

        Default is a no-op so in-memory implementations need not care; the
        HTTP-backed client overrides it. Called once on application shutdown.
        """
        return None


class BackendAPIClient(BackendClient):
    """
    Production integration: the MyFi backend's internal API, which reads the
    live database. Selected with BACKEND_INTEGRATION_MODE=backend_api.

    Contract (docs/backend-integration.md, verified against the live service):
      * Authorization: Bearer <BACKEND_API_SERVICE_TOKEN> -- a STATIC
        service-to-service token. It is NOT the short-lived chat JWT from
        /get/token, which the internal API rejects with 401.
      * `user_id` is a required query parameter on every call. The token
        identifies this service, not the user, so the backend cannot verify
        the id for us -- which is why every response is re-checked against
        context.user_id by app/core/data_isolation.py (each record carries
        owner_user_id).
      * Responses are {"status","message","data","meta"} envelopes. All
        shape translation lives in app/services/live_api_mapping.py.

    Transactions are the only paginated endpoint. A page that is not the last
    is never dropped silently: a truncated list would produce a total that is
    wrong with no error anywhere, so an over-long result raises instead.
    """

    _PAGE_SIZE = 500  # the backend's maximum per_page
    # 60 pages x 500 = 30,000 rows. A six-month window for a very active user
    # is a few thousand; reaching this means a runaway query, not a customer.
    _MAX_PAGES = 60
    _PAGE_CONCURRENCY = 4
    # Each day's bills lookup costs the backend ~4 s regardless of load, and 14
    # in parallel finished in the same time as one (measured), so these are
    # fanned out wide: 31 sequential days would be two minutes.
    _BILLS_DAY_CONCURRENCY = 16

    def __init__(self, repository) -> None:  # repository: HttpRepository
        self._repo = repository

    # ----------------------------------------------------------------- helpers

    async def _get(self, path: str, context: UserContext, **params):
        query = {"user_id": context.user_id}
        query.update({k: v for k, v in params.items() if v is not None})
        return unwrap(await self._repo.get(path, params=query))

    # ----------------------------------------------------------------- profile

    async def get_user_profile(self, context: UserContext) -> dict:
        data = as_dict(await self._get("/internal/users/profile", context))
        # The profile is not an OwnedRecord, so the generic ownership barrier
        # does not see it. Check it here: asking for user 5 and being handed
        # user 9's name and timezone is the same breach as a foreign row.
        declared = data.get("user_id")
        if declared is not None and str(declared) != str(context.user_id):
            from app.core.exceptions import CrossUserDataError

            raise CrossUserDataError("Data integrity check failed for this request.")
        # The email is deliberately dropped: nothing downstream needs it, and a
        # dict that reaches prompt assembly should not carry contact details.
        return {
            "user_id": str(data.get("user_id") or context.user_id),
            "name": data.get("name"),
            "currency": data.get("currency") or context.currency,
            "timezone": data.get("timezone") or context.timezone,
        }

    # ---------------------------------------------------------------- accounts

    async def get_accounts(self, context: UserContext) -> list[Account]:
        data = await self._get("/internal/accounts", context)
        rows = (map_account(as_dict(i), context.user_id) for i in as_list(data))
        return [a for a in rows if a is not None]

    # ------------------------------------------------------------ transactions

    async def get_transactions(
        self,
        context: UserContext,
        *,
        start: date | None = None,
        end: date | None = None,
        category: str | None = None,
        merchant: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
    ) -> list[Transaction]:
        params = {
            "user_id": context.user_id,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "merchant": merchant,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "per_page": self._PAGE_SIZE,
        }
        params = {k: v for k, v in params.items() if v is not None}

        rows = await self._fetch_window("/internal/transactions", params, start, end)

        transactions: list[Transaction] = []
        seen: set[str] = set()
        for item in rows:
            item = as_dict(item)
            # The backend already filters is_deleted = 0 (manually excluded rows);
            # do not filter on is_review_transfer or is_internal_transfer.
            # Pages are fetched while the user may be adding transactions, so
            # a row can legitimately appear on two adjacent pages.
            record_id = str(item.get("record_id") or "")
            if record_id:
                if record_id in seen:
                    continue
                seen.add(record_id)
            mapped = map_transaction(item, context.user_id)
            if mapped is not None:
                transactions.append(mapped)

        # Matched after prettifying so the filter compares what the user was
        # shown ("Food And Drink"), so the filter compares what the user is shown.
        if category:
            wanted = category.lower()
            transactions = [t for t in transactions if (t.category or "").lower() == wanted]

        transactions.sort(key=lambda t: t.date, reverse=True)
        return transactions

    # Windows longer than this are fetched one calendar month per request, in
    # parallel. The backend's latency is per request, not per row, and a page
    # cannot be requested before page 1 reveals how many there are -- so a
    # three-month window took two sequential round trips of 6-12 s each where
    # three parallel month requests take one.
    _SPLIT_ABOVE_DAYS = 35
    _MAX_PARALLEL_WINDOWS = 6

    async def _fetch_window(self, path: str, params: dict, start: date | None, end: date | None) -> list:
        if start is None or end is None or (end - start).days <= self._SPLIT_ABOVE_DAYS:
            return await self._fetch_all_pages(path, params)

        chunks: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            chunks.append((cursor, min(month_end, end)))
            cursor = month_end + timedelta(days=1)

        gate = asyncio.Semaphore(self._MAX_PARALLEL_WINDOWS)

        async def fetch(lo: date, hi: date) -> list:
            async with gate:
                return await self._fetch_all_pages(
                    path, {**params, "start": lo.isoformat(), "end": hi.isoformat()}
                )

        pages = await asyncio.gather(*(fetch(lo, hi) for lo, hi in chunks))
        return [row for page in pages for row in page]

    async def _fetch_all_pages(self, path: str, params: dict) -> list:
        first = await self._repo.get(path, params={**params, "page": 1})
        rows, meta = envelope(first)
        rows = list(as_list(rows))

        pagination = as_dict(meta.get("pagination"))
        total_pages = int(pagination.get("total_pages") or 1)
        if total_pages <= 1:
            return rows
        if total_pages > self._MAX_PAGES:
            raise BackendServiceError(
                "The backend returned more transactions than can be read safely for this request.",
                details={"total_pages": total_pages},
            )

        gate = asyncio.Semaphore(self._PAGE_CONCURRENCY)

        async def fetch(page: int) -> list:
            async with gate:
                payload = await self._repo.get(path, params={**params, "page": page})
            return as_list(unwrap(payload))

        for page_rows in await asyncio.gather(*(fetch(p) for p in range(2, total_pages + 1))):
            rows.extend(page_rows)
        return rows

    async def get_income(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        txns = await self.get_transactions(context, start=start, end=end)
        return income(txns)

    async def get_expenses(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        txns = await self.get_transactions(context, start=start, end=end)
        return expenses(txns)

    # ------------------------------------------------------ bills / budgets etc

    # How far past the end of the current month bills are looked up, a day at
    # a time. Covers the 14- and 28-day windows the affordability and
    # liquidity tools use when they straddle a month boundary.
    _BILLS_LOOKAHEAD_DAYS = 31

    async def get_bills(
        self, context: UserContext, *, start: date | None = None, end: date | None = None
    ) -> list[Bill]:
        """
        The backend's bills payload describes the CURRENT month: start/end are
        ignored server-side (a September and a November window both returned
        October). Two consequences handled here:

        * the window is applied client-side;
        * a window that runs past month end is completed from the same
          endpoint's `selected_date` parameter, which returns the bills due on
          one specific day -- including days in the next month.

        A window reaching BEFORE the current month cannot be answered; the
        bills tool reports that via bills_coverage() instead of "none due".
        """
        data = await self._get("/internal/bills", context)
        bills = map_bills(data, context.user_id)

        period_end = to_date(as_dict(data).get("period_end"))
        if end is not None and period_end is not None and end > period_end:
            bills.extend(await self._bills_due_after(context, period_end, end))

        if start is not None:
            bills = [b for b in bills if b.due_date >= start]
        if end is not None:
            bills = [b for b in bills if b.due_date <= end]

        unique: dict[tuple[str, date], Bill] = {}
        for bill in bills:
            unique.setdefault((bill.bill_id, bill.due_date), bill)
        return sorted(unique.values(), key=lambda b: b.due_date)

    async def _bills_due_after(self, context: UserContext, period_end: date, end: date) -> list[Bill]:
        last = min(end, period_end + timedelta(days=self._BILLS_LOOKAHEAD_DAYS))
        days = [period_end + timedelta(days=n) for n in range(1, (last - period_end).days + 1)]
        gate = asyncio.Semaphore(self._BILLS_DAY_CONCURRENCY)

        async def due_on(day: date) -> list[Bill]:
            async with gate:
                data = as_dict(await self._get("/internal/bills", context, selected_date=day.isoformat()))
            # Only this day's own entries: the rest of the payload is the
            # current month again, already read above.
            return map_bills({"owner_user_id": data.get("owner_user_id"), "due_bills": data.get("due_bills")},
                             context.user_id)

        found: list[Bill] = []
        for batch in await asyncio.gather(*(due_on(d) for d in days)):
            found.extend(b for b in batch if b.due_date in days)
        return found

    def bills_coverage(self, context: UserContext) -> tuple[date, date] | None:
        today = current_date_in(context.timezone)
        month_start, month_end = month_bounds(today.year, today.month)
        return month_start, month_end + timedelta(days=self._BILLS_LOOKAHEAD_DAYS)

    async def get_budgets(self, context: UserContext, *, period_start: date, period_end: date) -> list[BudgetCategory]:
        data = await self._get(
            "/internal/budgets", context, start=period_start.isoformat(), end=period_end.isoformat()
        )
        return map_budgets(data, context.user_id)

    async def get_subscriptions(self, context: UserContext) -> list[Subscription]:
        fetched: list[object] = list(
            await asyncio.gather(
                self._get("/internal/subscriptions", context),
                self._get("/internal/recurring-transactions", context),
                return_exceptions=True,
            )
        )
        subscriptions_payload, recurring_payload = fetched[0], fetched[1]
        if isinstance(subscriptions_payload, BaseException):
            raise subscriptions_payload
        # The recurring list only supplies categories. If it is unavailable the
        # subscriptions are still answerable, just less precisely filtered.
        categories: dict[str, str] = {}
        if not isinstance(recurring_payload, BaseException):
            categories = {
                str(as_dict(r).get("recurring_id")): str(as_dict(r).get("category") or "")
                for r in as_list(recurring_payload)
            }
        return map_subscriptions(subscriptions_payload, context.user_id, categories)

    async def get_recurring_transactions(self, context: UserContext) -> list[RecurringTransaction]:
        return map_recurring(await self._get("/internal/recurring-transactions", context), context.user_id)

    async def get_investments(self, context: UserContext) -> list[InvestmentHolding]:
        return map_investments(await self._get("/internal/investments", context), context.user_id)

    async def get_liabilities(self, context: UserContext) -> list[Liability]:
        return map_liabilities(await self._get("/internal/liabilities", context), context.user_id)

    # ------------------------------------------------------- summary & scores

    async def get_financial_summary(self, context: UserContext, *, start: date, end: date) -> FinancialSummary:
        """
        Derived from the paginated transactions dataset using the Allo
        application's primary_category classification (INCOME + TRANSFER_IN =
        income; all other categories including TRANSFER_OUT = expenses).
        """
        transactions = await self.get_transactions(context, start=start, end=end)
        total_income = sum((income_amount(t) for t in income(transactions)), Decimal("0"))
        total_expenses = sum((expense_amount(t) for t in expenses(transactions)), Decimal("0"))
        currency = transactions[0].currency if transactions else "USD"
        return FinancialSummary(
            owner_user_id=context.user_id,
            period_start=start,
            period_end=end,
            total_income=Money(amount=total_income, currency=currency),
            total_expenses=Money(amount=total_expenses, currency=currency),
            net_cash_flow=Money(amount=total_income - total_expenses, currency=currency),
        )

    async def get_financial_score(self, context: UserContext) -> FinancialScore | None:
        # NB: the backend's /financial-score is the ALLO score, not a credit
        # score. The credit-bureau score lives under /credit-score-history.
        # limit=1: each history row embeds the bureau's full report payload
        # (~100 KB), so the default 24 rows took 13-19 s for "what is my
        # credit score?" when only `current` is needed.
        data = as_dict(await self._get("/internal/credit-score-history", context, limit=1))
        return map_credit_score(data.get("current"), context.user_id)

    async def get_financial_score_history(self, context: UserContext) -> list[FinancialScore]:
        # Three readings answer "up or down recently" and show a direction; each
        # row embeds the bureau's full report (~100 KB), so six took 13-19 s.
        data = as_dict(await self._get("/internal/credit-score-history", context, limit=3))
        rows = [map_credit_score(r, context.user_id) for r in as_list(data.get("history"))]
        scores = [r for r in rows if r is not None]
        if not scores:
            current = map_credit_score(data.get("current"), context.user_id)
            scores = [current] if current else []
        return sorted(scores, key=lambda s: s.as_of_date or date.min, reverse=True)

    async def get_allo_score(self, context: UserContext) -> AlloScore | None:
        data = as_dict(await self._get("/internal/allo-score", context))
        return map_allo_score(data.get("current"), context.user_id)

    async def get_allo_score_history(self, context: UserContext) -> list[AlloScore]:
        data = as_dict(await self._get("/internal/allo-score", context, limit=60))
        rows = [map_allo_score(r, context.user_id) for r in as_list(data.get("history"))]
        scores = [r for r in rows if r is not None]
        return sorted(scores, key=lambda s: s.period_end, reverse=True)

    async def get_savings_goal(self, context: UserContext) -> SavingsGoal | None:
        return map_savings_goal(await self._get("/internal/savings-goals", context), context.user_id)

    async def get_liability_history(self, context: UserContext) -> list[LiabilitySnapshot]:
        data = await self._get("/internal/liability-history", context, limit=120)
        return map_liability_history(data, context.user_id)

    async def get_retirement_readiness(self, context: UserContext) -> RetirementReadiness | None:
        return map_retirement(await self._get("/internal/retirement-readiness", context), context.user_id)

    async def get_net_worth(self, context: UserContext) -> NetWorth | None:
        return map_net_worth(await self._get("/internal/net-worth", context), context.user_id)

    async def aclose(self) -> None:
        await self._repo.aclose()


@lru_cache(maxsize=1)
def get_backend_client() -> BackendClient:
    """
    Factory selecting the configured integration mode. This is the single
    place the rest of the application needs to touch to switch modes — see
    settings.backend_integration_mode.

    Cached deliberately. Each BackendAPIClient owns an httpx.AsyncClient with
    its own connection pool; building one per request would leak a pool per
    request and forfeit connection reuse entirely. The client holds no
    per-user state — user scoping is passed in explicitly on every call — so
    a single shared instance is safe. Released via aclose() on shutdown.

    Tests that change BACKEND_INTEGRATION_MODE must call
    get_backend_client.cache_clear().
    """
    from app.core.config import settings

    if settings.backend_integration_mode.value == "backend_api":
        from app.repositories.base import HttpRepository

        if not settings.backend_api_base_url:
            raise RuntimeError("backend_api_base_url must be set when backend_integration_mode=backend_api")
        repo = HttpRepository(
            base_url=settings.backend_api_base_url,
            timeout_seconds=settings.backend_api_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.backend_api_service_token}"},
        )
        return BackendAPIClient(repo)

    raise RuntimeError(
        "backend_integration_mode='read_only_db' requires a concrete database schema that was not "
        "provided — see docs/assumptions.md. Implement app/services/db_backend_client.py against the "
        "real schema before enabling this mode."
    )
