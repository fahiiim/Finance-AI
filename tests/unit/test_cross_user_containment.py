"""
CRITICAL TEST SUITE — cross-user containment barrier.

tests/unit/test_user_isolation.py proves this service ASKS for the right
user's data. These tests prove it also CHECKS what came back, so a backend
bug (a dropped WHERE clause, a tenant-mixing cache) cannot be formatted into
an answer and handed to the wrong person.

The distinction matters: scoping a request is a statement of intent, not
proof about the response.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.exceptions import CrossUserDataError
from app.schemas.financial import Account, Money, Transaction
from app.services.backend_client import BackendClient
from app.services.financial_service import FinancialService
from tests.conftest import make_user_context


def _txn(txn_id: str, owner: str | None) -> Transaction:
    return Transaction(
        transaction_id=txn_id,
        account_id="acct-1",
        amount=Decimal("-10.00"),
        date=date(2026, 1, 15),
        merchant_name="Cafe",
        owner_user_id=owner,
    )


class LeakyBackend(BackendClient):
    """A backend that ignores scoping and returns another user's records."""

    def __init__(self, records) -> None:
        self._records = records

    async def get_transactions(self, context, **kwargs):
        return self._records

    async def get_accounts(self, context):
        return self._records

    # Unused abstract methods — the barrier is exercised via the two above.
    async def get_user_profile(self, context): return {}
    async def get_income(self, context, *, start, end): return []
    async def get_expenses(self, context, *, start, end): return []
    async def get_bills(self, context, *, start=None, end=None): return []
    async def get_budgets(self, context, *, period_start, period_end): return []
    async def get_subscriptions(self, context): return []
    async def get_recurring_transactions(self, context): return []
    async def get_investments(self, context): return []
    async def get_liabilities(self, context): return []
    async def get_financial_summary(self, context, *, start, end): return None
    async def get_financial_score(self, context): return None


@pytest.mark.asyncio
async def test_foreign_transactions_abort_the_request():
    """The whole point: user A must never be shown user B's transactions."""
    service = FinancialService(LeakyBackend([_txn("t-1", "user-B")]))

    with pytest.raises(CrossUserDataError):
        await service.get_transactions(make_user_context(user_id="user-A"))


@pytest.mark.asyncio
async def test_a_single_foreign_record_rejects_the_whole_response():
    """
    Fail closed, not fail quiet. We do NOT filter out the foreign record and
    answer with the rest — if the source is leaking, what remains cannot be
    assumed trustworthy either.
    """
    service = FinancialService(
        LeakyBackend([_txn("t-1", "user-A"), _txn("t-2", "user-A"), _txn("t-3", "user-B")])
    )

    with pytest.raises(CrossUserDataError):
        await service.get_transactions(make_user_context(user_id="user-A"))


@pytest.mark.asyncio
async def test_correctly_owned_data_passes_through_untouched():
    records = [_txn("t-1", "user-A"), _txn("t-2", "user-A")]
    service = FinancialService(LeakyBackend(records))

    result = await service.get_transactions(make_user_context(user_id="user-A"))
    assert result == records


@pytest.mark.asyncio
async def test_barrier_also_covers_methods_reached_by_delegation():
    """
    Tools reach most backend methods through FinancialService.__getattr__.
    Those must be verified too — otherwise the barrier only guards the one
    method that happens to be written out explicitly.
    """
    leaked_account = Account(
        account_id="acct-B",
        name="Someone else's checking",
        type="checking",
        balance=Money(amount=Decimal("999")),
        owner_user_id="user-B",
    )
    service = FinancialService(LeakyBackend([leaked_account]))

    with pytest.raises(CrossUserDataError):
        await service.get_accounts(make_user_context(user_id="user-A"))


@pytest.mark.asyncio
async def test_unlabeled_data_is_allowed_by_default():
    """
    Backends that do not emit `owner_user_id` yet must keep working — the
    barrier verifies what it can rather than breaking every deployment.
    """
    service = FinancialService(LeakyBackend([_txn("t-1", None)]))
    result = await service.get_transactions(make_user_context(user_id="user-A"))
    assert len(result) == 1


@pytest.mark.asyncio
async def test_strict_mode_rejects_unverifiable_data(monkeypatch):
    """
    With STRICT_DATA_OWNERSHIP=true, unlabeled data becomes an error instead
    of a blind spot. This is the setting to enable once the backend emits
    ownership on every record.
    """
    from app.core import data_isolation

    monkeypatch.setattr(data_isolation.settings, "strict_data_ownership", True)
    get_settings.cache_clear()

    service = FinancialService(LeakyBackend([_txn("t-1", None)]))
    with pytest.raises(CrossUserDataError):
        await service.get_transactions(make_user_context(user_id="user-A"))


@pytest.mark.asyncio
async def test_breach_error_does_not_leak_the_other_users_identity():
    """
    The error surfaces to the client and to logs. Neither should carry the
    foreign user_id or any record content — logs are typically less protected
    than the data itself.
    """
    service = FinancialService(LeakyBackend([_txn("t-1", "victim-user-12345")]))

    with pytest.raises(CrossUserDataError) as exc_info:
        await service.get_transactions(make_user_context(user_id="user-A"))

    assert "victim-user-12345" not in str(exc_info.value)
    assert "t-1" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_strict_mode_rejects_a_PARTIALLY_labelled_response(monkeypatch):
    """
    One labelled record used to vouch for every unlabelled one beside it.

    The check was `unlabeled and checked == 0`, so a response had to be
    ENTIRELY unlabelled to be rejected. A mixed response — the realistic shape
    of a partial backend rollout, where some endpoints emit `owner_user_id`
    and some do not — passed silently, and the unlabelled rows went into the
    answer unverified. Strict mode has to mean strict, or it means nothing.
    """
    from app.core import data_isolation

    monkeypatch.setattr(data_isolation.settings, "strict_data_ownership", True)
    get_settings.cache_clear()

    service = FinancialService(LeakyBackend([_txn("t-1", "user-A"), _txn("t-2", None)]))
    with pytest.raises(CrossUserDataError):
        await service.get_transactions(make_user_context(user_id="user-A"))


@pytest.mark.asyncio
async def test_strict_mode_tolerates_nested_objects_that_have_no_owner(monkeypatch):
    """
    A Money has no `owner_user_id` field and never will. Counting it as
    unlabelled would make strict mode reject every response that contains one
    — which is all of them — so the setting could never be turned on.
    """
    from datetime import date
    from decimal import Decimal

    from app.core import data_isolation
    from app.schemas.financial import FinancialSummary, Money

    monkeypatch.setattr(data_isolation.settings, "strict_data_ownership", True)
    get_settings.cache_clear()

    summary = FinancialSummary(
        owner_user_id="user-A",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        total_income=Money(amount=Decimal("100"), currency="USD"),
        total_expenses=Money(amount=Decimal("40"), currency="USD"),
    )
    verified = data_isolation.verify_ownership(summary, make_user_context(user_id="user-A"), source="probe")
    assert verified is summary


@pytest.mark.asyncio
async def test_unlabelled_data_is_reported_once_so_the_flag_can_be_flipped_safely(monkeypatch, caplog):
    """
    Whether the backend labels ownership has to be answerable BEFORE turning
    strict mode on, or the flag can only be flipped as a gamble in production.
    Reported once per source, because it is a standing property of the
    backend, not a per-request event.
    """
    import logging

    from app.core import data_isolation

    monkeypatch.setattr(data_isolation.settings, "strict_data_ownership", False)
    monkeypatch.setattr(data_isolation, "_UNLABELED_SOURCES_SEEN", set())
    get_settings.cache_clear()

    service = FinancialService(LeakyBackend([_txn("t-1", None)]))
    context = make_user_context(user_id="user-A")

    with caplog.at_level(logging.WARNING):
        await service.get_transactions(context)
    assert "records_without_owner_user_id" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await FinancialService(LeakyBackend([_txn("t-2", None)])).get_transactions(context)
    assert "records_without_owner_user_id" not in caplog.text, "reported once per source, not per request"
