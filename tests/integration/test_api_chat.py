from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_orchestrator
from app.main import app
from app.orchestrator.orchestrator import Orchestrator
from app.schemas.planner import Intent, RawExecutionPlan
from app.services.conversation_service import ConversationService, InMemoryConversationStore
from app.services.memory_service import MemoryService
from app.services.mock_backend_client import MockBackendClient
from tests.conftest import FakeLLMProvider, make_service_token


def _build_test_orchestrator(plans, response_text="You spent $295.00 this month."):
    llm = FakeLLMProvider(plans=plans, response_text=response_text)
    backend = MockBackendClient()
    conversations = ConversationService(InMemoryConversationStore())
    memory = MemoryService(conversations, llm)
    return Orchestrator(llm_provider=llm, backend_client=backend, conversation_service=conversations, memory_service=memory)


@pytest.fixture
def make_client():
    """Factory fixture: make_client(plans, response_text) -> TestClient wired to a fake orchestrator."""
    created_clients = []

    def _make(plans, response_text="You spent $295.00 this month."):
        orchestrator = _build_test_orchestrator(plans, response_text)
        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
        client = TestClient(app)
        created_clients.append(client)
        return client

    yield _make
    app.dependency_overrides.clear()
    for c in created_clients:
        c.close()


def test_chat_requires_authentication():
    client = TestClient(app)
    response = client.post("/api/v1/chat", json={"session_id": "s1", "message": "hi"})
    assert response.status_code == 401


def test_chat_happy_path(make_client):
    plans = [
        RawExecutionPlan(
            intent=Intent.SPENDING_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            required_tools=["get_expenses"],
            date_phrase="this month",
        )
    ]
    client = make_client(plans)

    token = make_service_token("user-1", "session-1")
    response = client.post(
        "/api/v1/chat",
        json={"session_id": "session-1", "message": "How much did I spend this month?"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "session-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "session-1"
    assert body["message"]["content"] == "You spent $295.00 this month."
    assert body["metadata"]["intent"] == "SPENDING_ANALYSIS"
    assert body["metadata"]["data_used"] is True
    assert "get_expenses" in body["metadata"]["tools_used"]


def test_chat_rejects_oversized_message():
    client = TestClient(app)
    token = make_service_token("user-1", "session-1")
    response = client.post(
        "/api/v1/chat",
        json={"session_id": "session-1", "message": "a" * 3000},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "session-1"},
    )
    assert response.status_code == 422  # caught by ChatRequest max_length before orchestrator runs


def test_chat_general_conversation_does_not_touch_financial_data(make_client):
    plans = [
        RawExecutionPlan(
            intent=Intent.GENERAL_CONVERSATION, requires_financial_data=False, requires_calculation=False
        )
    ]
    client = make_client(plans, response_text="Hey there! How can I help with your finances today?")

    token = make_service_token("user-2", "session-2")
    response = client.post(
        "/api/v1/chat",
        json={"session_id": "session-2", "message": "hey!"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "session-2"},
    )
    assert response.status_code == 200
    assert response.json()["metadata"]["data_used"] is False


def test_body_session_id_must_match_the_authenticated_session():
    """
    The body's session_id is not used for scoping, but a mismatch means the
    client believes it is writing to a different conversation than it is.
    Silent divergence is how conversations end up in the wrong place.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import make_service_token

    client = TestClient(app)
    token = make_service_token("mismatch-user", "real-session")

    resp = client.post(
        "/api/v1/chat",
        json={"session_id": "a-different-session", "message": "What did I spend?"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "real-session"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_failed"


def test_rate_limit_returns_429_through_the_api():
    """The limiter must actually sit on the request path, not just exist."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_llm_provider
    from app.core.rate_limit import get_rate_limiter
    from app.main import app
    from tests.conftest import FakeLLMProvider, make_service_token

    # Throttling is what's under test — don't let this touch the real
    # provider, which would also bind its singleton client to this loop.
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider()

    limiter = get_rate_limiter()
    limiter.reset()
    original = limiter._limit
    limiter._limit = 2
    try:
        client = TestClient(app)
        token = make_service_token("rate-user", "rate-session")
        headers = {"Authorization": f"Bearer {token}", "X-Session-Id": "rate-session"}
        body = {"session_id": "rate-session", "message": "What did I spend?"}

        statuses = [client.post("/api/v1/chat", json=body, headers=headers).status_code for _ in range(4)]
        assert 429 in statuses, statuses

        throttled = client.post("/api/v1/chat", json=body, headers=headers)
        assert throttled.status_code == 429
        assert throttled.json()["code"] == "rate_limited"
    finally:
        limiter._limit = original
        limiter.reset()
        app.dependency_overrides.clear()


def test_the_response_names_the_period_it_covers(make_client):
    """
    A financial figure is only meaningful next to its period. The caller must
    not have to parse the prose to learn which window was used -- or to notice
    that the wrong one was: a comparison silently resolved to the earlier of
    two named periods once, and the answer read perfectly.
    """
    client = make_client(
        [
            RawExecutionPlan(
                intent=Intent.CATEGORY_ANALYSIS,
                requires_financial_data=True,
                requires_calculation=True,
                category_filter="Groceries",
                date_phrase="last month",
            )
        ]
    )
    token = make_service_token(user_id="42", session_id="s-period")
    body = {"session_id": "s-period", "message": "How much did I spend on groceries last month?"}
    r = client.post(
        "/api/v1/chat",
        json=body,
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "s-period"},
    )
    assert r.status_code == 200, r.text
    meta = r.json()["metadata"]
    assert meta["period_phrase"] == "last month"
    assert meta["period_start"] is not None and meta["period_end"] is not None
    assert meta["period_start"] < meta["period_end"]


def test_a_reply_with_no_period_reports_none(make_client):
    client = make_client(
        [
            RawExecutionPlan(
                intent=Intent.GENERAL_CONVERSATION,
                requires_financial_data=False,
                requires_calculation=False,
            )
        ],
        response_text="Hello! How can I help with your finances?",
    )
    token = make_service_token(user_id="42", session_id="s-none")
    r = client.post(
        "/api/v1/chat",
        json={"session_id": "s-none", "message": "hi"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "s-none"},
    )
    assert r.status_code == 200, r.text
    meta = r.json()["metadata"]
    assert meta["period_start"] is None
    assert meta["period_end"] is None
    assert meta["period_phrase"] is None


def test_a_tool_that_cannot_compute_answers_instead_of_erroring(make_client):
    """
    A tool saying "I cannot compute this from what I have" is a
    conversational outcome, not an HTTP failure.

    Observed live: asking "should I buy a 4K OLED LG TV?" and then confirming
    produced a red "422 validation_failed: An affordability question requires
    a specific amount." box in the middle of the chat. The user had asked a
    perfectly reasonable question.
    """
    client = make_client(
        [
            RawExecutionPlan(
                intent=Intent.MERCHANT_ANALYSIS,
                requires_financial_data=True,
                requires_calculation=True,
                date_phrase="last month",
                # MERCHANT_ANALYSIS needs a merchant; without one the tool
                # raises ValidationFailedError.
                merchant_filter=None,
            )
        ],
        response_text="I could not work that one out from what I have.",
    )
    token = make_service_token(user_id="42", session_id="s-toolfail")
    r = client.post(
        "/api/v1/chat",
        json={"session_id": "s-toolfail", "message": "how much did I spend there?"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "s-toolfail"},
    )
    assert r.status_code == 200, f"a tool failure must not become an HTTP error: {r.text}"
    body = r.json()
    assert body["message"]["content"], "the user must get an answer, not an error body"
    assert body["metadata"]["data_used"] is True, "the reason is handed to the model as data"


def test_request_level_validation_is_still_rejected(make_client):
    """The 422 that should survive: a body that contradicts the token."""
    client = make_client(
        [RawExecutionPlan(intent=Intent.GENERAL_CONVERSATION, requires_financial_data=False, requires_calculation=False)]
    )
    token = make_service_token(user_id="42", session_id="s-real")
    r = client.post(
        "/api/v1/chat",
        json={"session_id": "s-different", "message": "hello"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "s-real"},
    )
    assert r.status_code == 422
