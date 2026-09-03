"""
The persistent conversation store.

In-memory history is lost on restart and is not shared between instances, so
behind a load balancer a user's second turn can land on a process that has
never heard of them. Nothing errors when that happens — the assistant just
answers as though the conversation had begun, re-asks a question it was
already answered, and reads "yes" as a message with no subject. Every
response is still a 200, which is why this needs tests rather than trust.

Covers the contract in docs/handover-backend.md section 8.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.exceptions import BackendServiceError, ConversationError
from app.schemas.conversation import ConversationMessage, ConversationSummary, MessageRole
from app.services.api_conversation_store import BackendAPIConversationStore
from tests.conftest import make_user_context


class FakeRepo:
    """Records requests and replays canned responses, like the real endpoints would."""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[tuple[str, str, dict]] = []

    async def _reply(self, verb: str, path: str, payload: dict):
        self.requests.append((verb, path, payload))
        value = self.responses.get((verb, path), [])
        if isinstance(value, Exception):
            raise value
        return value

    async def get(self, path, *, params=None):
        return await self._reply("GET", path, params or {})

    async def post(self, path, *, json=None):
        return await self._reply("POST", path, json or {})

    async def delete(self, path, *, params=None):
        return await self._reply("DELETE", path, params or {})


CONTEXT = make_user_context(user_id="42", session_id="sess-1")


def _not_found() -> BackendServiceError:
    return BackendServiceError("not found", details={"status_code": 404})


@pytest.mark.asyncio
async def test_every_call_is_scoped_by_user_AND_session():
    """
    A session id alone is guessable across users. Both must travel on every
    request, or the store becomes the hole the rest of the service exists to
    prevent.
    """
    repo = FakeRepo()
    store = BackendAPIConversationStore(repo)

    await store.create_session(CONTEXT)
    await store.get_messages(CONTEXT)
    await store.get_summary(CONTEXT)
    await store.delete_session(CONTEXT)
    await store.append_message(CONTEXT, ConversationMessage(role=MessageRole.USER, content="hi"))

    assert repo.requests, "nothing was sent"
    for verb, path, payload in repo.requests:
        assert payload.get("user_id") == "42", (verb, path)
        assert payload.get("session_id") == "sess-1", (verb, path)


@pytest.mark.asyncio
async def test_the_three_memory_columns_are_always_sent():
    """
    is_clarification, date_phrase and intent are what let a later turn read a
    bare "yes" against the question it answers. Omitting them when falsy would
    make the backend store a default that means something different.
    """
    repo = FakeRepo()
    store = BackendAPIConversationStore(repo)

    await store.append_message(
        CONTEXT,
        ConversationMessage(
            role=MessageRole.ASSISTANT, content="Which month did you mean?",
            intent="SPENDING_ANALYSIS", is_clarification=True, date_phrase="last month",
        ),
    )
    _, path, payload = repo.requests[-1]
    assert path == "/internal/conversations/messages"
    assert payload["is_clarification"] is True
    assert payload["date_phrase"] == "last month"
    assert payload["intent"] == "SPENDING_ANALYSIS"

    # And present-but-empty on an ordinary answer, rather than absent.
    await store.append_message(CONTEXT, ConversationMessage(role=MessageRole.ASSISTANT, content="You spent $10."))
    _, _, payload = repo.requests[-1]
    assert payload["is_clarification"] is False
    assert "date_phrase" in payload and payload["date_phrase"] is None


@pytest.mark.asyncio
async def test_a_date_phrase_is_stored_as_the_user_said_it():
    """
    Resolving it at write time would freeze "this month" to whichever month
    the row was inserted in. It is re-resolved on every read instead.
    """
    repo = FakeRepo()
    await BackendAPIConversationStore(repo).append_message(
        CONTEXT,
        ConversationMessage(role=MessageRole.ASSISTANT, content="ok", date_phrase="this month"),
    )
    assert repo.requests[-1][2]["date_phrase"] == "this month"


@pytest.mark.asyncio
async def test_a_missing_session_reports_absent_not_broken():
    repo = FakeRepo({("GET", "/internal/conversations"): _not_found()})
    assert await BackendAPIConversationStore(repo).session_exists(CONTEXT) is False


@pytest.mark.asyncio
async def test_a_backend_fault_is_never_read_as_no_session():
    """
    Treating a 500 as "no session yet" would start a fresh conversation on top
    of an existing one -- silently discarding a user's history because the
    backend was briefly unwell.
    """
    repo = FakeRepo({("GET", "/internal/conversations"): BackendServiceError("boom", details={"status_code": 500})})
    with pytest.raises(BackendServiceError):
        await BackendAPIConversationStore(repo).session_exists(CONTEXT)


@pytest.mark.asyncio
async def test_messages_round_trip_with_their_memory_fields():
    stored = [
        {"role": "user", "content": "how much on food?"},
        {"role": "assistant", "content": "Which month?", "is_clarification": True, "date_phrase": "last month"},
    ]
    repo = FakeRepo({("GET", "/internal/conversations/messages"): stored})
    messages = await BackendAPIConversationStore(repo).get_messages(CONTEXT)

    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[1].is_clarification is True
    assert messages[1].date_phrase == "last month"


@pytest.mark.asyncio
async def test_no_summary_yet_is_none_not_an_error():
    repo = FakeRepo({("GET", "/internal/conversations/summary"): {}})
    assert await BackendAPIConversationStore(repo).get_summary(CONTEXT) is None


@pytest.mark.asyncio
async def test_a_summary_round_trips():
    summary = ConversationSummary(
        session_id="sess-1", summary_text="They asked about food spending.",
        covers_message_count=8, updated_at=datetime.now(UTC),
    )
    repo = FakeRepo()
    await BackendAPIConversationStore(repo).set_summary(CONTEXT, summary)
    _, path, payload = repo.requests[-1]
    assert path == "/internal/conversations/summary"
    assert payload["summary_text"] == "They asked about food spending."
    assert payload["user_id"] == "42" and payload["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_a_failed_create_surfaces_as_a_conversation_error():
    repo = FakeRepo({("POST", "/internal/conversations"): BackendServiceError("boom", details={"status_code": 500})})
    with pytest.raises(ConversationError):
        await BackendAPIConversationStore(repo).create_session(CONTEXT)


@pytest.mark.asyncio
async def test_it_satisfies_the_store_interface_the_service_depends_on():
    """A store the service cannot use is not a store."""
    from app.services.conversation_service import ConversationService, ConversationStore

    assert issubclass(BackendAPIConversationStore, ConversationStore)
    service = ConversationService(BackendAPIConversationStore(FakeRepo()))
    await service.add_user_message(CONTEXT, "hello")
