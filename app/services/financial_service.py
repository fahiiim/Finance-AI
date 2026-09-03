"""
Per-request cache + cross-user containment barrier over BackendClient.

TWO responsibilities, both of which depend on this being the ONE path tools
use to reach financial data:

1. CACHING. A single chat turn can need several tools that overlap in the
   data they fetch (both calculate_total_spending and detect_spending_spike
   want the same transaction list). This avoids redundant backend calls
   within one request — see spec section 39 — without introducing any
   cross-request or cross-user caching, which would risk leaking data
   between users. A new FinancialService is constructed fresh per request.

2. OWNERSHIP VERIFICATION. Every result is passed through
   verify_ownership() before any tool can see it, so a backend that returns
   the wrong user's records aborts the request instead of being formatted
   into an answer. This is enforced HERE rather than in each tool because
   there are 21 intent handlers and dozens of tool functions: a rule that
   every one of them must remember to apply is a rule that will eventually
   be forgotten. Routing every call through one wrapper makes the check
   structural instead of conventional.
"""

from __future__ import annotations

import inspect
from datetime import date
from functools import wraps

from app.core.data_isolation import verify_ownership
from app.core.security import UserContext
from app.services.backend_client import BackendClient
from app.utils.transactions import expenses, income


class FinancialService:
    def __init__(self, backend_client: BackendClient) -> None:
        self._client = backend_client
        self._transaction_cache: dict[tuple, list] = {}
        self._call_cache: dict[tuple, object] = {}

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
    ):
        # user_id is part of the cache key even though the cache is already
        # per-request. Defence in depth: if this object is ever made
        # longer-lived by a future change, the key still cannot collide
        # across users.
        cache_key = (context.user_id, start, end, category, merchant, min_amount, max_amount)
        if cache_key in self._transaction_cache:
            return self._transaction_cache[cache_key]

        result = await self._client.get_transactions(
            context,
            start=start,
            end=end,
            category=category,
            merchant=merchant,
            min_amount=min_amount,
            max_amount=max_amount,
        )
        verify_ownership(result, context, source="get_transactions")
        self._transaction_cache[cache_key] = result
        return result

    async def get_income(self, context: UserContext, *, start: date, end: date):
        """
        Income for a window, derived from the cached transaction list.

        Every backend implements get_income as a filter over get_transactions
        using the one sign convention in app/utils/transactions.py, so
        deriving it here is equivalent -- and it means a turn that needs
        transactions AND income costs one backend round trip instead of two.
        Over HTTP that is the difference between one request and several
        hundred kilobytes fetched twice.
        """
        return income(await self.get_transactions(context, start=start, end=end))

    async def get_expenses(self, context: UserContext, *, start: date, end: date):
        """Expenses for a window, derived from the cached transaction list. See get_income."""
        return expenses(await self.get_transactions(context, start=start, end=end))

    def __getattr__(self, name: str):
        """
        Delegate anything not explicitly cached to the backend client, with
        the ownership barrier applied on the way back.

        Wrapping the delegation (rather than returning the bound method
        directly) is what guarantees a tool cannot obtain unverified data by
        calling a method this class never anticipated.

        The result is memoised for the life of this request. A single turn can
        ask the same question twice -- an affordability check reads accounts
        and bills, an overall-health check reads a summary the cash-flow path
        may already have fetched -- and against the HTTP backend each repeat
        is a real round trip. The cache is per-request and per-user by
        construction (a new FinancialService is built for every request, and
        user_id is part of every key), so it can never serve one user data
        fetched for another.
        """
        attribute = getattr(self._client, name)

        if not (callable(attribute) and inspect.iscoroutinefunction(attribute)):
            return attribute

        @wraps(attribute)
        async def _verified(context: UserContext, *args, **kwargs):
            try:
                key = (name, context.user_id, args, tuple(sorted(kwargs.items())))
            except TypeError:
                # An unhashable argument: skip the cache rather than fail.
                key = None

            if key is not None and key in self._call_cache:
                return self._call_cache[key]

            result = await attribute(context, *args, **kwargs)
            verified = verify_ownership(result, context, source=name)
            if key is not None:
                self._call_cache[key] = verified
            return verified

        return _verified
