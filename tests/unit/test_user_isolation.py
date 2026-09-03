"""
CRITICAL TEST SUITE.

Per spec section 36: "User A must NEVER receive User B data." These tests
exist specifically to catch any regression that would let one user's
context leak into another user's response.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_user_context


@pytest.mark.asyncio
async def test_mock_backend_gives_different_data_per_user(mock_backend):
    ctx_a = make_user_context(user_id="user-A")
    ctx_b = make_user_context(user_id="user-B")

    accounts_a = await mock_backend.get_accounts(ctx_a)
    accounts_b = await mock_backend.get_accounts(ctx_b)

    ids_a = {a.account_id for a in accounts_a}
    ids_b = {a.account_id for a in accounts_b}
    assert ids_a.isdisjoint(ids_b)
    assert all(a.account_id.startswith("user-A") for a in accounts_a)
    assert all(a.account_id.startswith("user-B") for a in accounts_b)


@pytest.mark.asyncio
async def test_mock_backend_transactions_are_scoped_per_user(mock_backend):
    ctx_a = make_user_context(user_id="user-A")
    ctx_b = make_user_context(user_id="user-B")

    txns_a = await mock_backend.get_transactions(ctx_a)
    txns_b = await mock_backend.get_transactions(ctx_b)

    txn_ids_a = {t.transaction_id for t in txns_a}
    txn_ids_b = {t.transaction_id for t in txns_b}
    assert txn_ids_a.isdisjoint(txn_ids_b)


@pytest.mark.asyncio
async def test_conversation_store_is_scoped_by_user_and_session():
    from app.services.conversation_service import InMemoryConversationStore

    store = InMemoryConversationStore()
    ctx_a = make_user_context(user_id="user-A", session_id="shared-session-id")
    ctx_b = make_user_context(user_id="user-B", session_id="shared-session-id")

    await store.create_session(ctx_a)
    await store.append_message(ctx_a, _msg("user-A's private message"))

    # Same session_id, different user — must be a completely separate conversation.
    assert await store.session_exists(ctx_b) is False
    await store.create_session(ctx_b)
    messages_b = await store.get_messages(ctx_b)
    assert messages_b == []

    messages_a = await store.get_messages(ctx_a)
    assert len(messages_a) == 1
    assert "user-A" in messages_a[0].content


def _msg(content: str):
    from app.schemas.conversation import ConversationMessage, MessageRole

    return ConversationMessage(role=MessageRole.USER, content=content)


@pytest.mark.asyncio
async def test_chat_request_schema_has_no_user_id_field():
    """
    The ChatRequest schema must never accept a user_id — identity comes only
    from the authenticated UserContext dependency. This test guards against
    someone "helpfully" adding a user_id field back to the schema later.
    """
    from app.schemas.chat import ChatRequest

    assert "user_id" not in ChatRequest.model_fields
