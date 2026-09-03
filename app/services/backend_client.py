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

from abc import ABC, abstractmethod
from datetime import date
from functools import lru_cache

from app.core.security import UserContext
from app.schemas.financial import (
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
from app.utils.transactions import expenses, income


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

    async def aclose(self) -> None:
        """
        Release any long-lived resources (connection pools, sockets).

        Default is a no-op so in-memory implementations need not care; the
        HTTP-backed client overrides it. Called once on application shutdown.
        """
        return None


class BackendAPIClient(BackendClient):
    """
    PRIMARY RECOMMENDED integration mode: calls the client's existing,
    already-authenticated backend over its internal API.

    Every request is scoped by passing context.user_id as a query parameter
    the backend is expected to cross-check against its own service-token
    identity — this client never lets a caller override that. The exact
    endpoint paths are an assumption documented in docs/assumptions.md,
    since no existing API contract was provided; they're centralized here so
    updating them to match the real backend is a one-file change.
    """

    def __init__(self, repository) -> None:  # repository: HttpRepository
        self._repo = repository

    async def get_user_profile(self, context: UserContext) -> dict:
        return await self._repo.get("/internal/users/profile", params={"user_id": context.user_id})

    async def get_accounts(self, context: UserContext) -> list[Account]:
        data = await self._repo.get("/internal/accounts", params={"user_id": context.user_id})
        return [Account.model_validate(item) for item in data]

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
            "category": category,
            "merchant": merchant,
            "min_amount": min_amount,
            "max_amount": max_amount,
        }
        data = await self._repo.get("/internal/transactions", params={k: v for k, v in params.items() if v is not None})
        return [Transaction.model_validate(item) for item in data]

    async def get_income(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        txns = await self.get_transactions(context, start=start, end=end)
        return income(txns)

    async def get_expenses(self, context: UserContext, *, start: date, end: date) -> list[Transaction]:
        txns = await self.get_transactions(context, start=start, end=end)
        return expenses(txns)

    async def get_bills(
        self, context: UserContext, *, start: date | None = None, end: date | None = None
    ) -> list[Bill]:
        params = {
            "user_id": context.user_id,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        }
        data = await self._repo.get("/internal/bills", params={k: v for k, v in params.items() if v is not None})
        return [Bill.model_validate(item) for item in data]

    async def get_budgets(self, context: UserContext, *, period_start: date, period_end: date) -> list[BudgetCategory]:
        data = await self._repo.get(
            "/internal/budgets",
            params={"user_id": context.user_id, "start": period_start.isoformat(), "end": period_end.isoformat()},
        )
        return [BudgetCategory.model_validate(item) for item in data]

    async def get_subscriptions(self, context: UserContext) -> list[Subscription]:
        data = await self._repo.get("/internal/subscriptions", params={"user_id": context.user_id})
        return [Subscription.model_validate(item) for item in data]

    async def get_recurring_transactions(self, context: UserContext) -> list[RecurringTransaction]:
        data = await self._repo.get("/internal/recurring-transactions", params={"user_id": context.user_id})
        return [RecurringTransaction.model_validate(item) for item in data]

    async def get_investments(self, context: UserContext) -> list[InvestmentHolding]:
        data = await self._repo.get("/internal/investments", params={"user_id": context.user_id})
        return [InvestmentHolding.model_validate(item) for item in data]

    async def get_liabilities(self, context: UserContext) -> list[Liability]:
        data = await self._repo.get("/internal/liabilities", params={"user_id": context.user_id})
        return [Liability.model_validate(item) for item in data]

    async def get_financial_summary(self, context: UserContext, *, start: date, end: date) -> FinancialSummary:
        data = await self._repo.get(
            "/internal/financial-summary",
            params={"user_id": context.user_id, "start": start.isoformat(), "end": end.isoformat()},
        )
        return FinancialSummary.model_validate(data)

    async def get_financial_score(self, context: UserContext) -> FinancialScore | None:
        data = await self._repo.get("/internal/financial-score", params={"user_id": context.user_id})
        return FinancialScore.model_validate(data) if data else None

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

    if settings.backend_integration_mode.value == "mock":
        from app.services.mock_backend_client import MockBackendClient

        return MockBackendClient()

    if settings.backend_integration_mode.value == "sqlite":
        from app.services.sqlite_backend_client import SqliteBackendClient

        return SqliteBackendClient(settings.demo_db_path)

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
